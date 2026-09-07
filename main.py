"""FastAPI 入口（V0/V1 保留 + V2 工具调用循环 + 静态前端）。

V0：/api/profiles 等既有接口；
V1：/api/import 导入日记并建向量索引（原样保留）；
V2：/api/chat 从“焊死 RAG 检索”改为 Agent 多轮工具调用循环，
    并新增 GET /api/notes 供前端展示 open 笔记。
V3：把 static/index.html 挂到根路径，访问 / 直接打开可视化前端。
"""

from contextlib import asynccontextmanager
import os

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


# ===== V2 新增：GET /api/notes（前端待办区数据源） =====
@app.get("/api/notes")
def list_open_notes():
    """返回全部 open 状态的笔记，前端待办/记录区展示用。

    只暴露前端需要的四个字段；按 id 倒序让最新记录排在最前。
    """
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, type, content, created_at
            FROM notes
            WHERE status = 'open'
            ORDER BY id DESC
            """
        ).fetchall()
    return [dict(row) for row in rows]


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
