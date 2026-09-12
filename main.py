"""FastAPI 入口（V0/V1 保留 + V2 工具调用循环 + 静态前端）。

V0：/api/profiles 等既有接口；
V1：/api/import 导入日记并建向量索引（原样保留）；
V2：/api/chat 从“焊死 RAG 检索”改为 Agent 多轮工具调用循环，
    并新增 GET /api/notes 供前端展示 open 笔记。
V3：把 static/index.html 挂到根路径，访问 / 直接打开可视化前端。
V4：待办勾选完成——GET /api/notes 支持 status 过滤并返回 status 字段，
    新增 PATCH /api/notes/{id}/status 让前端把待办勾成 done / 取消回 open。
V5：刷新恢复上下文——新增 GET /api/messages，前端打开页面就把
    最近一次会话的历史对话回填到对话区，不用从头聊。
"""

from contextlib import asynccontextmanager
import os
from typing import Optional

# Web 框架：FastAPI 提供路由、参数校验与自动生成 OpenAPI 文档
from fastapi import FastAPI
from fastapi import HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel  # 请求体模型：自动校验并反序列化 JSON

import db      # 数据库持久化层
import rag     # 日记向量检索（/api/import 仍在使用）
import agent   # V2 Agent 工具调用循环
import tools   # V2 工具定义与分发（/api/chat 链路使用）
import memory  # V2 会话记忆组装（/api/chat 链路使用）
from db import get_conn, init_db  # 常用函数直接导入，避免重复写模块前缀


# 静态资源目录：相对本文件定位而不是用相对路径，
# 保证从任意工作目录启动 uvicorn 都能找到 index.html
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()  # 服务启动时自动建表（幂等）
    yield  # yield 之前是启动逻辑，之后是关闭逻辑


app = FastAPI(title="Personal AI Agent", version="3.0.0", lifespan=lifespan)

# 跨域中间件：开发期允许任意来源调用，方便前端联调
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ===== V0 既有接口（保留不动） =====
@app.get("/api/profiles")
def list_profiles():
    """返回所有启用中的用户画像条目，供 V0 前端展示。"""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM profiles
            WHERE status = 'active'
            ORDER BY id
            """
        ).fetchall()
    return [dict(row) for row in rows]  # sqlite3.Row 转普通 dict 便于 JSON 序列化


# ===== V2 新增 / V4 扩展：GET /api/notes（前端待办区数据源） =====
@app.get("/api/notes")
def list_notes(status: Optional[str] = None):
    """返回笔记列表，前端待办/记录区展示用。

    参数：
        status：可选查询参数，只接受 'open' / 'done'；
            不传表示返回全部（前端「全部」筛选）。
    返回：note 对象列表，含前端画勾所需的 status 字段，按 id 倒序。
    副作用：无（只读）。

    V2 时这里写死 status='open'，V4 必须放开：前端要区分勾/未勾、
    还要支持「已完成」筛选，只返回 open 会让 done 条目一勾就消失，
    刷新后也无从显示。非法 status 显式报 400 而不是静默返回空列表，
    否则拼错参数的前端只会看到「暂无待办」，问题很难定位。
    """
    if status is not None and status not in ("open", "done"):
        raise HTTPException(status_code=400, detail="status 只能是 open 或 done")
    # user_id 过滤下沉到 db 层（本地单用户固定 'local'），
    # 保证接口不会把别的用户/别的来源的笔记混进前端列表
    return db.list_notes(status=status)


# ===== V4 新增：PATCH /api/notes/{id}/status（待办勾选完成） =====
class NoteStatusPayload(BaseModel):
    """PATCH /api/notes/{id}/status 的请求体。

    这里声明成普通 str 而不是 Literal["open","done"]：用 Literal 时
    非法值会被 FastAPI 拦成 422，本接口按约定必须返回 400，
    所以合法性交给路由函数手工判断，模型只负责收字段。

    模型定义紧贴使用它的路由：FastAPI 在注册路由时就要求该类型已存在，
    写成前向引用（字符串）会解析失败，因此不能挪到文件后面的模型区。
    """
    status: str        # 目标状态：open / done，具体合法性在路由内校验


@app.patch("/api/notes/{note_id}/status")
def update_note_status(note_id: int, payload: NoteStatusPayload):
    """把指定笔记的状态改成 open / done，返回更新后的完整 note 对象。

    参数：
        note_id：路径参数，笔记主键；
        payload：请求体 {"status": "done"} 或 {"status": "open"}。
    返回：更新后的 note dict（id/type/content/status/created_at）。
    副作用：写库（UPDATE notes），因此前端勾选后刷新仍能保留状态。
    异常：status 非 open/done → 400；笔记不存在（或不属于当前用户）→ 404。
    """
    # status 在这里手工校验而不是用 Pydantic 的 Literal：
    # Literal 校验失败由 FastAPI 直接回 422，而任务要求非法值返 400；
    # 手工判断才能给出中文 detail，前端 toast 可以直接展示
    if payload.status not in ("open", "done"):
        raise HTTPException(status_code=400, detail="status 只能是 open 或 done")

    note = db.set_note_status(note_id, payload.status)
    if note is None:
        # db 层返回 None = UPDATE 未命中（id 不存在或不属于 user_id 范围）；
        # 不能返回 200 空对象，否则前端会把「没改成」当成「改成功了」
        raise HTTPException(status_code=404, detail="笔记不存在")
    return note


def _group_history(rows):
    """把消息行归并成前端渲染单元（用户/助手两条气泡 + 助手侧的工具轨迹）。

    参数：
        rows：db.list_messages 返回的行列表，按 id 正序，可能混有 role='tool'。
    返回：列表，元素为
        {"id","role","content","created_at"}；
        role='assistant' 的元素额外带 "tool_calls"（本轮工具轨迹）与
        "process_recorded"（本轮是否有工具过程记录）。

    为什么要在后端归并而不是原样吐给前端：
    1) role='tool' 的行是「过程」，不是对话气泡，直接渲染会让对话区
       混进工具原文；
    2) agent.py 的落库顺序是 user → tool… → assistant（工具结果先于
       最终回复入库），所以把 tool 行挂到「紧随其后的那条 assistant」
       上，恰好还原成本轮的真实过程；
    3) 前端渲染逻辑因此不必了解 role=tool 的存在，容错更简单。

    坑点：待办/画像类工具一轮可能存多条 tool 行，全部归到同一条
    assistant 回复下，前端按调用顺序逐张渲染卡片即可。

    TODO: 补存工具调用过程。messages 表当前只存 assistant 的最终回复
    与 tool 结果文本，模型逐轮思考（trace）和 tool_calls 参数（args）
    都没有入库，所以刷新后只能还原「工具名 + 返回内容」，
    参数与逐轮时间线无法回放；要完整回放就得给 messages 表加
    过程字段（或单独建 trace 表），本次先按「有多少还原多少」处理。
    """
    grouped = []
    pending_tools = []  # 尚未归属到某条 assistant 回复的工具过程

    for row in rows:
        role = row.get("role")
        if role == "tool":
            pending_tools.append(
                {
                    "tool": row.get("tool_name") or "unknown_tool",
                    # args 没入库，用固定文案占位而不是编造内容
                    "args": "（未记录）",
                    "result": row.get("content") or "",
                }
            )
            continue

        if role == "assistant":
            # 工具过程挂在紧随其后的 assistant 回复上，与真实发生顺序一致
            grouped.append(
                {
                    "id": row.get("id"),
                    "role": role,
                    "content": row.get("content"),
                    "created_at": row.get("created_at"),
                    "tool_calls": pending_tools,
                    "process_recorded": bool(pending_tools),
                }
            )
            pending_tools = []
            continue

        # 剩下的只有 user。若这里还挂着工具过程，说明上一轮 assistant
        # 回复没落库（进程被中断等异常历史），这些过程无法归属，丢弃即可：
        # 硬塞给下一条回复会造成「张冠李戴」的假回放
        pending_tools = []
        grouped.append(
            {
                "id": row.get("id"),
                "role": role,
                "content": row.get("content"),
                "created_at": row.get("created_at"),
            }
        )
    return grouped


# ===== V5 新增：GET /api/messages（刷新后回填历史对话） =====
@app.get("/api/messages")
def get_messages(session_id: Optional[str] = None):
    """返回某会话的历史消息，前端打开页面时用它回填对话区。

    参数：
        session_id：可选。传了就取该会话；不传则退化为
            「最近一次有消息的会话」（前端换浏览器/清缓存后的兜底路径）。
    返回：{"session_id": 实际使用的会话 ID（空库时为空串）,
          "messages": 按时间正序（旧→新）的渲染单元列表}。
        每条含 role/content/created_at/id；assistant 条目额外带
        tool_calls 与 process_recorded（见 _group_history）。
    副作用：无（只读）。
    """
    # 传了 session_id 就严格按它查，即使查出来是空的也不偷偷换成别的会话：
    # 「这个会话没消息」和「给你换个会话」是两种语义，静默替换会让前端
    # 把新消息写进另一个会话里而用户毫不知情；要兜底由前端显式再调一次
    target = (session_id or "").strip()
    if not target:
        target = db.latest_session_id() or ""

    rows = db.list_messages(target) if target else []
    return {"session_id": target, "messages": _group_history(rows)}


# ===== 请求体模型 =====
class ImportPayload(BaseModel):
    """POST /api/import 的请求体。"""
    content: str       # 日记正文（必填）
    title: str = ""    # 日记标题，可选
    source: str = "manual"  # 来源，默认手动导入
    written_at: str = ""    # 日记的落笔日期，可选


class ChatPayload(BaseModel):
    """POST /api/chat 的请求体。"""
    message: str       # 用户问题（必填）
    session_id: str    # 会话 ID，用于把往返消息归入同一会话


# ===== V1：日记导入接口 =====
@app.post("/api/import")
def import_journal(payload: ImportPayload):
    """按「来源+日期+标题」幂等写入 journal 表并同步建立向量索引。"""
    content = payload.content.strip()  # 去掉首尾空白，空正文直接拒绝
    if not content:
        raise HTTPException(status_code=400, detail="content 不能为空")

    # 1) upsert 落库：命中已有同键行就 UPDATE 并沿用原 id，
    #    未命中才 INSERT 新行；created 供导入脚本打印「新增/更新」
    journal_id, created = db.upsert_journal(
        payload.source,
        payload.title,
        content,
        payload.written_at,
    )

    # 2) 再建向量索引：标题+正文一起切片入库，保证后续 RAG 能检索到
    #    这里把 written_at/source 一起透传，向量 metadata 才能带日期和来源
    rag.add_journal(
        journal_id,
        payload.title + "\n" + content,
        title=payload.title,
        written_at=payload.written_at,
        source=payload.source,
    )
    # created 会随响应返回，前端/导入脚本据此区分新增与覆盖
    return {"ok": True, "journal_id": journal_id, "created": created}


# ===== V2：Agent 聊天接口（工具调用循环替代 V1 焊死检索） =====
@app.post("/api/chat")
def chat(payload: ChatPayload):
    """交给 Agent 循环处理，返回最终回复、工具轨迹与逐轮 trace 时间线。"""
    message = payload.message.strip()  # 空白问题直接拒绝，避免空转一次 LLM
    if not message:
        raise HTTPException(status_code=400, detail="message 不能为空")

    # Agent 内部完成“存消息 → 调 LLM → 执行工具 → 存结果 → 收尾”全流程；
    # 这里不再做任何 RAG 拼接，是否检索完全由模型按系统提示决定
    result = agent.run_chat(payload.session_id, message)
    return {
        "reply": result["reply"],
        "tool_calls": result["tool_calls"],  # 完整轨迹：工具/参数/结果前200字
        "messages": result["messages"],  # 本轮发给模型的完整上下文（调试视图）
        "trace": result["trace"],  # 模型思考+工具执行的逐轮时间线
    }


# ===== 静态前端挂载：必须放在所有 API 路由之后 =====
# html=True 让 StaticFiles 把目录下的 index.html 作为默认首页，
# 因此 GET / 会直接打开可视化界面而不是返回 JSON；
# FastAPI 按注册顺序匹配路由，API 路径先注册先命中，不会被挂载覆盖
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
