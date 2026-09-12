"""个人 AI Agent 的 SQLite 持久化层——全项目唯一的业务写入口。

- 只使用标准库 sqlite3，不引入第三方数据库依赖；
- 数据库文件位于 data/agent.db；
- 时间统一存成 ISO-8601 字符串；
- 所有 SQL 一律使用 ? 占位符，避免拼接注入。

项目约定（写操作收口）：
    所有业务写操作（INSERT / UPDATE / DELETE）只能经由本模块的函数进行，
    main.py / tools.py / agent.py / memory.py 等上层模块一律不得直接
    conn.execute("INSERT/UPDATE/DELETE")。

    为什么定这条规矩：写操作散落在各层时，审计（改了哪张表、要不要记历史、
    时间戳怎么打）就得在每个调用点各写一遍，迟早出现「有的路径记了 updated_at、
    有的没记」这种不一致。收口到 db.py 后，新增一条业务规则只需要改一个文件，
    也保证每次写入都走同一套时间戳/事务/历史记录逻辑。

    注意这是代码层面的约定，不是技术上的强制：本地单用户，数据库文件本来
    就可读写（用 SQLite Viewer 直接改也是允许的）。本模块不引入权限系统、
    文件锁之类的机制——那是过度设计。
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
    created_at TEXT NOT NULL,            -- 首次入库时间
    updated_at TEXT NOT NULL             -- 最近一次内容改动时间（应用层维护，见 add_profile/update_profile）
);

-- profile_history：画像修订历史（审计用，只增不删）
-- 为什么要有这张表：profiles 是「持续修订」的表，画像被改过之后
-- 表里只剩新值，改歪了无从回滚、也无从知道谁在什么时候改的；
-- 每次创建/修改都留一条记录，历史才可回溯。
CREATE TABLE IF NOT EXISTS profile_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    -- profile_id 是任务书列清单之外我加的一列：同名 category 在 profiles 里
    -- 会有多行（实测 goal/pain_point 各 6 行），只按 category 记历史
    -- 无法回答「改的是哪一行」，回滚时会改错行；加上主键引用才真正可审计
    profile_id INTEGER,                  -- 对应 profiles.id
    user_id TEXT DEFAULT 'local',        -- 预留多用户字段
    category TEXT,                       -- 对应 profiles.category
    old_content TEXT,                    -- 修订前内容；首次创建为 NULL
    new_content TEXT,                    -- 修订后内容
    source TEXT,                         -- 写入来源：agent（save_profile 工具）/ seed（灌库脚本）
    changed_at TEXT                      -- 本次变更时间（ISO-8601）
);

-- 按画像行查历史是最常用的审计姿势（改歪了看这一行被谁改成什么）
CREATE INDEX IF NOT EXISTS idx_profile_history_profile
    ON profile_history(profile_id, id);

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
    created_at TEXT NOT NULL,
    -- 溯源字段：这条笔记由哪条 message 触发（用户当轮提问，= messages.id）。
    -- 前端点计划卡片要能跳回「当时那轮对话」，没有它就只能干瞪眼；
    -- 允许 NULL：V1 时代入库的老笔记没有来源，迁移后这些行保持 NULL
    source_message_id INTEGER
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
    """创建 data/agent.db（若不存在）、建表并补齐历史库缺失的列。"""
    # 说明：with 写法会提交事务但不会主动关闭连接；
    # SQLite 下短连接开销很低，本项目沿用这种简单写法
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


def _migrate(conn):
    """给已存在的老库补列（SQLite 的 CREATE TABLE IF NOT EXISTS 不会改旧表）。

    参数：
        conn：已打开的连接（由 init_db 传入，共用同一事务）。
    返回：无。
    副作用：必要时执行 ALTER TABLE ADD COLUMN 与回填 UPDATE（均幂等）。

    为什么必须显式迁移：老用户库里的 notes 表在 V5 之前就建好了，
    重跑 SCHEMA 只会发现表已存在、直接跳过，source_message_id 永远不会出现，
    接口一查就报 no such column。所以按「先查 PRAGMA 再决定加不加」的方式
    做增量迁移，而且必须幂等——服务每次启动都会调 init_db()。

    SQLite 的 ALTER TABLE ADD COLUMN 不需要重建表、不锁数据，
    老行自动填 NULL，正好符合「历史计划无法溯源」的预期。
    """
    note_cols = {
        row["name"] for row in conn.execute("PRAGMA table_info(notes)").fetchall()
    }
    if "source_message_id" not in note_cols:
        conn.execute("ALTER TABLE notes ADD COLUMN source_message_id INTEGER")

    # ---- V7：profiles.updated_at ----
    # 极老的库（V0 初版）只有 created_at，缺这一列
    profile_cols = {
        row["name"] for row in conn.execute("PRAGMA table_info(profiles)").fetchall()
    }
    if "updated_at" not in profile_cols:
        # 注意不能带 NOT NULL：SQLite 的 ADD COLUMN 对已有行没有值可填，
        # 只允许带非空默认值的 NOT NULL；这里先按可空加上，再用下面的
        # 回填把语义补成「永远有值」（新库走 SCHEMA，本来就是 NOT NULL）
        conn.execute("ALTER TABLE profiles ADD COLUMN updated_at TEXT")

    # 回填既有行的 updated_at：迁移这一刻没有比 created_at 更可信的信息，
    # 用创建时间兜底比留 NULL 更安全——上层（列表/导出）可以放心地把它
    # 当普通字符串用，不必到处判空。这条 UPDATE 对没有 NULL 的库是空操作，
    # 所以每次启动跑一遍也无害（幂等）
    conn.execute(
        "UPDATE profiles SET updated_at = created_at WHERE updated_at IS NULL"
    )


# ===== V2 起新增的 CRUD 辅助函数（上层模块只调这里，不自己拼写 SQL） =====
# 说明：V7 出于审计需要新增了 profile_history 表，并给 profiles 补了
# updated_at 语义，因此这里不再声称「不改表结构」——表结构由上面的
# SCHEMA + _migrate() 统一维护，本区块只负责读写逻辑


def save_message(session_id, role, content, tool_name=None):
    """向 messages 表插入一条消息。

    参数：
        session_id：会话 ID，字符串；
        role：消息角色，user/assistant/tool 之一；
        content：消息正文；tool 消息存放工具返回文本；
        tool_name：仅 tool 消息使用，记录调用的是哪个工具。
    返回：新插入消息的自增主键 id（插入即提交）。

    为什么要返回 id：Agent 在同一轮里可能调 create_note 记计划，
    这条计划要回填 source_message_id 指向「当轮用户消息」，
    拿不到 id 就没法建立溯源关系；返回 lastrowid 是最省事的做法，
    不改表结构也不多查一次库。
    """
    # 时间戳统一走 now_iso()，保证与历史消息格式一致；
    # tool_name 传 None 时写入 NULL，不污染普通消息
    with get_conn() as conn:
        cursor = conn.execute(
            """
            INSERT INTO messages (session_id, role, content, tool_name, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (session_id, role, content, tool_name, now_iso()),
        )
        return cursor.lastrowid


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


def latest_session_id(user_id=USER_ID):
    """取最近有过消息的会话 ID（按 messages.id 倒序取第一条）。

    参数：
        user_id：数据归属用户，默认本地单用户。
    返回：session_id 字符串；messages 表里一条消息都没有时返回 None。

    为什么要这个函数：前端只把 session_id 存在浏览器 localStorage 里，
    换浏览器/清缓存后它不知道自己上次聊的是哪个会话；接口不传
    session_id 时就靠这里兜底，保证「刷新后至少能看到最近一次对话」。
    """
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT session_id
            FROM messages
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (user_id,),
        ).fetchone()
    return row["session_id"] if row is not None else None


def list_messages(session_id, user_id=USER_ID):
    """按时间正序取某会话的全部消息行（含 role='tool' 的过程行）。

    参数：
        session_id：会话 ID；
        user_id：数据归属用户，默认本地单用户。
    返回：元素为 {"id","role","content","tool_name","created_at"} 的列表，
        按 id 正序（旧→新，也就是对话发生的真实顺序）。

    为什么把 role='tool' 的行也取出来：这些行不会进 LLM 上下文
    （见 memory.py 的红线），但它们是「本轮到底调了什么工具、
    工具返回了什么」的唯一持久化证据；刷新页面回填时丢掉了就没法回放，
    所以 db 层照取，由上层决定怎么归并展示。
    """
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, role, content, tool_name, created_at
            FROM messages
            WHERE session_id = ? AND user_id = ?
            ORDER BY id
            """,
            (session_id, user_id),
        ).fetchall()
    return [dict(row) for row in rows]


def get_message(message_id, user_id=USER_ID):
    """按主键取单条消息（含会话 ID），供溯源跳转接口使用。

    参数：
        message_id：messages 表主键；
        user_id：数据归属用户，默认本地单用户。
    返回：{"id","session_id","role","content","tool_name","created_at"}；
        不存在或不属于该用户时返回 None，由上层翻译成 404。

    为什么要带 session_id 返回：前端点计划卡片时，如果目标消息不在
    当前会话里，需要靠这个字段判断「是历史还没渲染完，还是跨会话了」，
    否则只能瞎猜，会把用户引到错误的对话上。
    """
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT id, session_id, role, content, tool_name, created_at
            FROM messages
            WHERE id = ? AND user_id = ?
            """,
            (message_id, user_id),
        ).fetchone()
    return dict(row) if row is not None else None


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


def add_note(note_type, content, source_message_id=None):
    """写入一条 open 状态的笔记（待办/愿望/想法/情绪/计划等）。

    参数：
        note_type：笔记类型；
        content：笔记内容；
        source_message_id：触发这条笔记的消息 id（通常是当轮用户消息），
            默认 None 表示来源不明（非对话路径写入的笔记）。
    返回：新插入笔记的自增主键 id。

    为什么来源由参数传入而不是在 SQL 里反查「最近一条消息」：
    同一轮里模型可能连续调多次 create_note，反查只能拿到同一条最新消息；
    而且并发会话下「最近一条」不可靠。由调用链上游（Agent 循环）把
    明确的 id 传下来，来源才是确定的。
    """
    with get_conn() as conn:
        cursor = conn.execute(
            """
            INSERT INTO notes
                (user_id, type, content, status, created_at, source_message_id)
            VALUES (?, ?, ?, 'open', ?, ?)
            """,
            (USER_ID, note_type, content, now_iso(), source_message_id),
        )
        return cursor.lastrowid  # 返回自增 id 便于前端/工具确认记录位置


def list_notes(status=None, user_id=USER_ID):
    """列出某用户的笔记，可按状态过滤（前端待办区数据源）。

    参数：
        status：'open' / 'done'；传 None 表示不过滤（全部都要）；
        user_id：数据归属用户，默认本地单用户。
    返回：元素为
        {"id","type","content","status","created_at","source_message_id"}
        的列表，按 id 倒序（最新记录在最前）。

    为什么要 status 过滤能力：前端需要在「全部 / 待办 / 已完成」之间切换，
    而下拉全量再在浏览器里筛会把无用的历史数据全塞进网络与内存；
    过滤下推到 SQL 里，配合 user_id 条件能直接走库内的行过滤。

    为什么要把 source_message_id 带出去：前端靠它实现「点计划卡片
    跳回那轮对话」，这个字段只在 notes 表里，接口不返回就断了链路。
    """
    # 状态与用户值一律走 ? 占位符；这里只按条件拼「结构」不拼「值」，
    # 因此不存在注入面（值永远由驱动转义后绑定）
    sql = (
        "SELECT id, type, content, status, created_at, source_message_id "
        "FROM notes WHERE user_id = ?"
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
    返回：更新后的 note dict（含 status 与 source_message_id 字段）；
        笔记不存在或不属于该用户时返回 None，由上层翻译成 404。
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
            SELECT id, type, content, status, created_at, source_message_id
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
    副作用：写 profiles 一行 + profile_history 一条（old_content 为 NULL）。

    为什么插入也要记历史：只记「修改」的话，档案里会出现没有来历的行——
    审计时无法区分「这条是冷启动灌进来的」和「Agent 在对话里新加的」。
    首次创建的 old_content 显式留 NULL，正好一眼区分新建与修订。
    """
    with get_conn() as conn:
        return _insert_profile(conn, category, content, source)


def update_profile(profile_id, content, source="agent", user_id=USER_ID):
    """修改一条画像的内容，刷新 updated_at 并留一条修订历史。

    参数：
        profile_id：目标画像主键；
        content：新的画像内容（调用方负责 strip）；
        source：本次修改的来源，默认 "agent"；
        user_id：数据归属用户，默认本地单用户。
    返回：更新后的整行 dict（含 category/content/created_at/updated_at）；
        画像不存在或不属于该用户时返回 None，由上层翻译成 404。
    副作用：写 profiles（content + updated_at）+ profile_history 一条旧→新记录。

    关于 updated_at 为什么要应用层写：SQLite 没有 MySQL 那种
    ON UPDATE CURRENT_TIMESTAMP 自动时间戳，只能显式赋值；
    也不用触发器——触发器藏在数据库里，读代码的人看不见，
    和本项目「逻辑全在代码里、注释能讲清」的目标相冲。

    为什么先查旧值再改：修订历史必须记「改之前是什么」，
    UPDATE 执行完就查不到旧值了；而且查不到行时直接返回 None，
    不会误报成修改成功。
    """
    with get_conn() as conn:
        old = conn.execute(
            """
            SELECT id, category, content
            FROM profiles
            WHERE id = ? AND user_id = ?
            """,
            (profile_id, user_id),
        ).fetchone()
        if old is None:
            return None

        conn.execute(
            """
            UPDATE profiles
            SET content = ?, updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (content, now_iso(), profile_id, user_id),
        )
        # 历史与 UPDATE 共用同一个连接/事务：要么两笔都落，要么都不落，
        # 不会出现「内容改了但历史没记上」的审计断点
        _record_profile_history(
            conn,
            profile_id=profile_id,
            category=old["category"],
            old_content=old["content"],
            new_content=content,
            source=source,
            user_id=user_id,
        )
        updated = conn.execute(
            """
            SELECT id, category, content, source, status, created_at, updated_at
            FROM profiles
            WHERE id = ? AND user_id = ?
            """,
            (profile_id, user_id),
        ).fetchone()
    return dict(updated)


def replace_profiles_by_source(source, items, user_id=USER_ID):
    """整批替换某个来源的画像（冷启动灌库脚本专用，单事务）。

    参数：
        source：本批画像的来源标记，同时也用来界定删除范围；
        items：[(category, content), ...]，按给定顺序写入；
        user_id：数据归属用户，默认本地单用户。
    返回：{"deleted": 删除条数, "inserted": [{"id","category","content"}, ...]}。
    副作用：删掉本来源的旧画像，写入新画像，并为每条新画像记一条创建历史。

    为什么把这个操作放进 db.py（而不是让脚本自己 DELETE + INSERT）：
    1) 写操作收口——脚本直接拼 DELETE/INSERT 就绕过了历史记录，
       会出现「灌进来的画像没有历史行」的审计盲区；
    2) 幂等灌库必须整体成功或整体失败，放在一个事务里才不会出现
       「删了一半、插了一半」的长期记忆污染。

    为什么不给删除动作补历史：删除对象是「本脚本上一轮写入的种子」，
    这些内容在当初写入时就已各留了一条创建历史，内容本身没有丢；
    再为批量重置单记删除会让历史表被整批噪音淹没（任务范围内也不要求）。
    """
    with get_conn() as conn:
        cursor = conn.execute(
            "DELETE FROM profiles WHERE source = ? AND user_id = ?",
            (source, user_id),
        )
        deleted = cursor.rowcount

        inserted = []
        for category, content in items:
            profile_id = _insert_profile(conn, category, content, source, user_id)
            inserted.append(
                {"id": profile_id, "category": category, "content": content}
            )
        return {"deleted": deleted, "inserted": inserted}


def _insert_profile(conn, category, content, source, user_id=USER_ID):
    """在调用方给定的事务里插入一条画像，并同步记一条创建历史。

    参数：
        conn：已打开的连接（由 add_profile / replace_profiles_by_source 传入）；
        category：画像类别；
        content：画像内容；
        source：来源标记；
        user_id：数据归属用户。
    返回：新画像的自增主键 id。

    为什么做成私有函数而不是公开的 db.add_profile：插入画像必须连同
    历史行一起写，把「两笔写」封在同一个函数里，外部就不可能只写一半；
    同时让批量灌库能复用同一套逻辑却仍然共享一个事务。
    """
    # created_at / updated_at 同一次取值：新建的画像还没被改过，
    # 两个时间戳本来就该一致（/api/profiles 和系统提示立刻能看到）
    timestamp = now_iso()
    cursor = conn.execute(
        """
        INSERT INTO profiles (category, content, source, status, created_at, updated_at)
        VALUES (?, ?, ?, 'active', ?, ?)
        """,
        (category, content, source, timestamp, timestamp),
    )
    profile_id = cursor.lastrowid
    _record_profile_history(
        conn,
        profile_id=profile_id,
        category=category,
        old_content=None,      # 首次创建没有「旧值」，历史里留 NULL 表示新建
        new_content=content,
        source=source,
        user_id=user_id,
    )
    return profile_id


def _record_profile_history(
    conn, profile_id, category, old_content, new_content, source, user_id=USER_ID
):
    """写一条画像变更历史（内部函数，复用调用方的事务）。

    参数：
        conn：已打开的连接；
        profile_id：画像主键；
        category：画像类别；
        old_content：变更前内容，首次创建传 None；
        new_content：变更后内容；
        source：来源标记（agent / seed 等）；
        user_id：数据归属用户。
    返回：无。
    副作用：向 profile_history 插入一行。
    """
    conn.execute(
        """
        INSERT INTO profile_history
            (profile_id, user_id, category, old_content, new_content, source, changed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (profile_id, user_id, category, old_content, new_content, source, now_iso()),
    )


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
