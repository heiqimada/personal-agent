"""个人 AI Agent 的 SQLite 持久化层。

- 只使用标准库 sqlite3，不引入第三方数据库依赖；
- 数据库文件位于 data/agent.db；
- 时间统一存成 ISO-8601 字符串；
- 所有 SQL 一律使用 ? 占位符，避免拼接注入。
"""

import os
import sqlite3
from datetime import datetime, timezone


# 项目根目录与数据库文件路径（相对本文件定位，保证从任意目录运行都正确）
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "data", "agent.db")

# 本地单用户模式的固定用户标识。
# 四张业务表都预留了 user_id 字段（默认 'local'），当前只跑本地单用户；
# 集中定义成常量而不是各处硬编码字符串，将来接多用户时只需改这里
# 或由调用方显式传参，不会出现「某处写 local 某处写 default」的脏数据。
USER_ID = "local"


# V0 建表语句：启动时执行 CREATE TABLE IF NOT EXISTS，可重复运行
SCHEMA = """
-- profiles：用户画像 / 长期记忆条目
CREATE TABLE IF NOT EXISTS profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT DEFAULT 'local',        -- 预留多用户字段，默认本地单用户
    category TEXT NOT NULL,              -- 分类：目标/痛点/关系/偏好/技能/愿望/状态/决策
    content TEXT NOT NULL,
    source TEXT,                         -- 来源：手动录入 / 反思生成
    status TEXT DEFAULT 'active',        -- 启用状态
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- journal：用户日记原文
CREATE TABLE IF NOT EXISTS journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT DEFAULT 'local',        -- 预留多用户字段
    source TEXT,                         -- 来源：apple_notes/onenote/evernote/xhs/manual
    title TEXT,
    content TEXT NOT NULL,
    written_at TEXT,                     -- 日记落笔时间（导入方提供）
    created_at TEXT NOT NULL             -- 本系统入库时间
);

-- notes：任务 / 愿望 / 想法 / 情绪 / 计划等零散笔记
CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT DEFAULT 'local',        -- 预留多用户字段
    type TEXT NOT NULL,                  -- 类型：任务/愿望/想法/情绪/计划
    content TEXT NOT NULL,
    status TEXT DEFAULT 'open',
    created_at TEXT NOT NULL
);

-- messages：聊天记录（user / assistant / tool 三种角色）
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT DEFAULT 'local',        -- 预留多用户字段
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,                  -- 角色：user/assistant/tool
    content TEXT NOT NULL,
    tool_name TEXT,
    created_at TEXT NOT NULL
);

-- 按会话查询消息时常用的联合索引
CREATE INDEX IF NOT EXISTS idx_msg_session ON messages(session_id, id);
"""


def get_conn(db_path=DB_PATH):
    """打开 SQLite 连接，并把每行结果设置为字典风格（可按列名访问）。"""
    os.makedirs(os.path.dirname(db_path), exist_ok=True)  # 确保 data/ 目录存在
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row  # 让 fetch 结果支持 row["列名"] 访问
    return conn


def now_iso():
    """返回当前 UTC 时间的 ISO-8601 字符串（精确到秒）。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init_db():
    """创建 data/agent.db（若不存在）并应用 V0 的建表语句。"""
    # 说明：with 写法会提交事务但不会主动关闭连接；
    # SQLite 下短连接开销很低，本项目沿用这种简单写法
    with get_conn() as conn:
        conn.executescript(SCHEMA)


# ===== V2：新增 CRUD 辅助函数（不改四张表 DDL，全部走既有表结构） =====


def save_message(session_id, role, content, tool_name=None):
    """向 messages 表插入一条消息。

    参数：
        session_id：会话 ID，字符串；
        role：消息角色，user/assistant/tool 之一；
        content：消息正文；tool 消息存放工具返回文本；
        tool_name：仅 tool 消息使用，记录调用的是哪个工具。
    返回：无（插入即提交）。
    """
    # 时间戳统一走 now_iso()，保证与历史消息格式一致；
    # tool_name 传 None 时写入 NULL，不污染普通消息
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO messages (session_id, role, content, tool_name, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (session_id, role, content, tool_name, now_iso()),
        )


def recent_messages(session_id, limit):
    """取某会话最近 limit 条“用户/助手”消息，按时间正序返回。

    参数：
        session_id：会话 ID；
        limit：最多取多少条，必传。
    返回：元素为 {"role": ..., "content": ...} 的列表，时间从旧到新。

    为什么 limit 不在这里放默认值：取多少条历史属于“对话策略”，
    应由上层（memory.py）根据 token 预算/轮次显式决定；
    db 层只负责“按调用方要求取数据”，不内置业务默认值，
    避免将来业务改窗口大小时还要在数据层做无意义修改。
    """
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT role, content
            FROM messages
            WHERE session_id = ? AND role IN ('user', 'assistant')
            ORDER BY id DESC
            LIMIT ?
            """,
            (session_id, limit),
        ).fetchall()
    # 倒序取回后再翻转成时间正序，LLM 才能按自然时序理解对话；
    # 必须显式过滤 role，绝不能把 tool 消息混进历史（原因见 memory.py）
    rows.reverse()
    return [{"role": row["role"], "content": row["content"]} for row in rows]


def list_active_profiles():
    """读取所有启用中的用户画像。

    返回：元素为 {"category": ..., "content": ...} 的列表，按 id 升序。
    """
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT category, content
            FROM profiles
            WHERE status = 'active'
            ORDER BY id
            """
        ).fetchall()
    return [dict(row) for row in rows]


def add_note(note_type, content):
    """写入一条 open 状态的笔记（待办/愿望/想法/情绪/计划等）。

    参数：
        note_type：笔记类型；
        content：笔记内容。
    返回：新插入笔记的自增主键 id。
    """
    with get_conn() as conn:
        cursor = conn.execute(
            """
            INSERT INTO notes (user_id, type, content, status, created_at)
            VALUES (?, ?, ?, 'open', ?)
            """,
            (USER_ID, note_type, content, now_iso()),
        )
        return cursor.lastrowid  # 返回自增 id 便于前端/工具确认记录位置


def list_notes(status=None, user_id=USER_ID):
    """列出某用户的笔记，可按状态过滤（前端待办区数据源）。

    参数：
        status：'open' / 'done'；传 None 表示不过滤（全部都要）；
        user_id：数据归属用户，默认本地单用户。
    返回：元素为 {"id","type","content","status","created_at"} 的列表，
        按 id 倒序（最新记录在最前）。

    为什么要 status 过滤能力：前端需要在「全部 / 待办 / 已完成」之间切换，
    而下拉全量再在浏览器里筛会把无用的历史数据全塞进网络与内存；
    过滤下推到 SQL 里，配合 user_id 条件能直接走库内的行过滤。
    """
    # 状态与用户值一律走 ? 占位符；这里只按条件拼「结构」不拼「值」，
    # 因此不存在注入面（值永远由驱动转义后绑定）
    sql = (
        "SELECT id, type, content, status, created_at FROM notes WHERE user_id = ?"
    )
    params = [user_id]
    if status:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY id DESC"

    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(row) for row in rows]


def set_note_status(note_id, status, user_id=USER_ID):
    """把某条笔记的状态改成 open/done，并返回更新后的整行数据。

    参数：
        note_id：笔记主键 id；
        status：目标状态，只接受 'open' / 'done'（合法性由上层校验）；
        user_id：数据归属用户，默认本地单用户。
    返回：更新后的 note dict（含 status 字段）；笔记不存在或不属于
        该用户时返回 None，由上层翻译成 404。
    副作用：写库（UPDATE notes）。

    为什么 UPDATE 的 WHERE 必须带 user_id：notes 表是多用户预留结构，
    只按 id 改会在将来多用户时变成「拿到别人的 id 就能改别人的数据」；
    代价只是多一个恒真条件，收益是把越权改数的可能性从根上掐掉。

    为什么改完再查一次而不是直接 return 传入值：UPDATE 影响 0 行时
    （id 不存在 / 不属于该用户）不能假装成功；同时以库里真实数据
    为准返回，前端拿到的 status 一定是持久化后的权威值。
    """
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE notes
            SET status = ?
            WHERE id = ? AND user_id = ?
            """,
            (status, note_id, user_id),
        )
        row = conn.execute(
            """
            SELECT id, type, content, status, created_at
            FROM notes
            WHERE id = ? AND user_id = ?
            """,
            (note_id, user_id),
        ).fetchone()
    # fetchone 无命中时返回 None，转成 None 让上层报 404 而不是编造一条假数据
    return dict(row) if row is not None else None


def add_profile(category, content, source="agent"):
    """写入一条启用中的用户画像（长期记忆）。

    参数：
        category：画像类别，如 goal/preference/status；
        content：提炼后的一句话画像；
        source：来源，Agent 写入时默认 "agent"。
    返回：新插入画像的自增主键 id。
    """
    with get_conn() as conn:
        cursor = conn.execute(
            """
            INSERT INTO profiles (category, content, source, status, created_at, updated_at)
            VALUES (?, ?, ?, 'active', ?, ?)
            """,
            (category, content, source, now_iso(), now_iso()),
        )
        # 新画像默认 active 且创建/更新时间一致，
        # 这样 V0 的 /api/profiles 和系统提示里的档案能立即看到
        return cursor.lastrowid


def upsert_journal(source, title, content, written_at):
    """按「来源+日期+标题」判重写入日记：有则 UPDATE、无则 INSERT。

    参数：
        source：日记来源（manual / apple_notes/自我思考记 等）；
        title：日记标题；
        content：日记正文（由调用方保证已 strip）；
        written_at：日记落笔日期（yyyy-mm-dd 或空字符串）。
    返回：(journal_id, created)；created=True 表示本次是新增，
        False 表示命中已有行并覆盖其 content。

    为什么用 SELECT+UPDATE/INSERT 而不是 INSERT OR REPLACE / 唯一索引：
    REPLACE 是「删旧行再插新行」，journal_id 会变；向量库切片 id
    形如 journal_{journal_id}_chunk_{i}，id 一变旧切片就成了查不到的
    孤儿。先按业务键 SELECT，命中就 UPDATE、未命中才 INSERT，
    才能保住 journal_id 稳定，重复导入只覆盖同 id 内容。

    为什么 written_at 为空时不判重、直接 INSERT：空日期说明来源/时间
    不可信，拿它和别人的 source+title 做匹配会误伤（比如把两篇同标题
    笔记错误合并）。该分支宁可每次新增，也不冒险合并。
    """
    written_at = written_at or ""  # None/空统一成空串，便于下方判空
    with get_conn() as conn:
        if written_at:
            # 倒序取最近一条：理论上同键不应重复，若历史脏数据有重复，
            # 覆盖最新的那一条即可，不扩大误伤面
            row = conn.execute(
                """
                SELECT id FROM journal
                WHERE source = ? AND written_at = ? AND title = ?
                ORDER BY id DESC LIMIT 1
                """,
                (source, written_at, title),
            ).fetchone()
            if row is not None:
                conn.execute(
                    "UPDATE journal SET content = ? WHERE id = ?",
                    (content, row["id"]),
                )
                return row["id"], False

        cursor = conn.execute(
            """
            INSERT INTO journal (source, title, content, written_at, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (source, title, content, written_at, now_iso()),
        )
        return cursor.lastrowid, True


if __name__ == "__main__":
    # 直接运行本文件时初始化数据库，便于手动建库
    init_db()
    print(f"Database ready: {DB_PATH}")
