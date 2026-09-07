"""V2 工具集：检索日记 / 记笔记 / 存画像 + OpenAI function-calling schema + 分发。

工具函数只做一件事并返回文本给模型；dispatch 负责把模型请求的
JSON 参数安全地分发到对应工具，任何失败都转成文本错误而不是抛异常，
这样 Agent 循环才能看到错误并自我纠正。
"""

import json

import db
import rag


# 余弦距离阈值：distance > 0.7 视为与问题不相关。
# 这是待调音参数：阈值调高会召回更多但可能引入噪声，
# 调低更严格但可能漏掉模糊相关的日记，后续按实测效果调整即可。
DISTANCE_THRESHOLD = 0.7


def search_journal(query):
    """按用户问题检索日记，返回整理成文本的命中片段。

    参数：
        query：要检索的用户问题/主题。
    返回：多行文本；无相关命中时返回固定提示，供模型如实转述。
    """
    hits = rag.search(query, top_k=3)
    # 控制台留痕便于调试检索质量；打印原始距离，过滤前就能看到全貌
    print(
        "[tool] search_journal "
        + query
        + " distances="
        + str([round(hit["distance"], 3) for hit in hits])
    )
    # 阈值过滤：超过阈值的片段余弦相似度过低，宁可让模型说“没依据”
    # 也不把弱相关片段当事实塞给模型，避免编造
    relevant = [hit for hit in hits if hit["distance"] <= DISTANCE_THRESHOLD]
    if not relevant:
        return "没有找到相关日记"

    lines = []
    for hit in relevant:
        # 每条命中抬头带 [日记#id · 日期 · 来源]（距离0.xx），
        # 让模型回答时能直接引用日期和来源；距离保留两位小数，
        # 既够人读也不会把浮点误差暴露给模型
        journal_id = hit["journal_id"]
        written_at = str(hit.get("written_at") or "").strip()
        source = str(hit.get("source") or "").strip()
        # 日期/来源为空时对应片段整体省略（不许拼出“·  ·”空架子），
        # 因此只把非空片段用“ · ”连接进方括号
        meta_parts = [part for part in (written_at, source) if part]
        if meta_parts:
            head = "[日记#{id} · {meta}]（距离{distance:.2f}）".format(
                id=journal_id,
                meta=" · ".join(meta_parts),
                distance=hit["distance"],
            )
        else:
            head = "[日记#{id}]（距离{distance:.2f}）".format(
                id=journal_id,
                distance=hit["distance"],
            )
        lines.append(
            head + "\n" + hit["content"]
        )
    return "\n\n".join(lines)


def create_note(note_type, content):
    """把用户要记住的内容写入笔记表，返回确认文本。

    参数：
        note_type：类型，限 task/wish/idea/emotion/plan；
        content：笔记正文。
    返回：确认文本（含新 id）；类型不合法时返回错误说明。
    """
    allowed = {"task", "wish", "idea", "emotion", "plan"}
    if note_type not in allowed:
        # 类型交给模型判断，但必须在边界处校验：
        # 脏类型会让 notes.type 失去统计价值，前端也无法归类
        return (
            "无效的笔记类型："
            + str(note_type)
            + "，可选："
            + "/".join(sorted(allowed))
        )
    if not content or not str(content).strip():
        return "笔记内容不能为空"
    new_id = db.add_note(note_type, str(content).strip())
    # 控制台打印落库结果，方便人工核对 Agent 行为
    print("[tool] create_note id={} type={}".format(new_id, note_type))
    return "已记录（id={}，类型={}）：{}".format(new_id, note_type, content)


def save_profile(category, content):
    """把用户长期事实提炼成画像入库，返回确认文本。

    参数：
        category：画像类别，限 goal/pain_point/relationship/preference/
            skill/wish/status/decision；
        content：一句话画像。
    返回：确认文本（含新 id）；类别不合法时返回错误说明。
    """
    allowed = {
        "goal",
        "pain_point",
        "relationship",
        "preference",
        "skill",
        "wish",
        "status",
        "decision",
    }
    if category not in allowed:
        # 画像类别决定系统提示里“长期记忆”的归类，
        # 非法类别会让档案混乱，所以宁可让模型重来一次
        return (
            "无效的画像类别："
            + str(category)
            + "，可选："
            + "/".join(sorted(allowed))
        )
    if not content or not str(content).strip():
        return "画像内容不能为空"
    new_id = db.add_profile(category, str(content).strip(), source="agent")
    # 控制台打印落库结果，方便人工核对 Agent 行为
    print("[tool] save_profile id={} category={}".format(new_id, category))
    return "已存档（id={}，类别={}）".format(new_id, category)


# OpenAI function-calling 的工具 schema。
# description 是写给 LLM 的说明书，决定模型何时该调用哪个工具，
# 因此每个都写清“什么时候用/什么时候不要用”。
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_journal",
            "description": (
                "当用户问及她自己的生活、经历、情绪、计划、学习进度、过去发生的事，"
                "或需要关于用户个人的事实依据时调用。闲聊、常识、通用知识问题不要调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "要检索的用户问题或主题，需完整表达问题语义",
                    }
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_note",
            "description": (
                "当用户要求记住待办、提醒、愿望、想法、情绪或计划时调用，"
                "如'提醒我…''记一下…''我希望…'。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "note_type": {
                        "type": "string",
                        "enum": ["task", "wish", "idea", "emotion", "plan"],
                        "description": "笔记类型：任务/愿望/想法/情绪/计划",
                    },
                    "content": {
                        "type": "string",
                        "description": "要记住的具体内容，保留用户原意",
                    },
                },
                "required": ["note_type", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_profile",
            "description": (
                "当对话中出现关于用户的长期稳定事实（目标/痛点/人际关系/偏好/技能/"
                "愿望/状态/重要决定）时调用，提炼成一句话存档。临时细节不要存。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "enum": [
                            "goal",
                            "pain_point",
                            "relationship",
                            "preference",
                            "skill",
                            "wish",
                            "status",
                            "decision",
                        ],
                        "description": "画像类别：目标/痛点/关系/偏好/技能/愿望/状态/决定",
                    },
                    "content": {
                        "type": "string",
                        "description": "提炼成一句话的长期事实，不存一次性临时细节",
                    },
                },
                "required": ["category", "content"],
            },
        },
    },
]


def dispatch(name, arguments_json):
    """按工具名把 JSON 参数分发给对应工具函数。

    参数：
        name：模型请求的工具名；
        arguments_json：OpenAI 返回的参数字符串（JSON 文本）。
    返回：工具结果的文本；解析失败或工具名未知时返回错误文本
        （不抛异常，错误会作为 tool 消息回传给模型自我纠正）。
    """
    try:
        # 模型偶尔会生成不完整 JSON，必须容错解析：
        # 解析失败不能打断 Agent 循环，要让它看到原因后重试
        args = json.loads(arguments_json) if arguments_json else {}
    except (json.JSONDecodeError, TypeError) as exc:
        return "工具参数解析失败：{}，请检查参数 JSON 格式".format(exc)

    # 显式取参而非 **args：即使模型漏传或传错类型，
    # 也能返回“缺哪个参数”的明确错误而不是 TypeError
    if name == "search_journal":
        query = args.get("query")
        if not query:
            return "缺少参数 query（应为你希望检索的问题）"
        return search_journal(str(query))

    if name == "create_note":
        note_type = args.get("note_type")
        content = args.get("content")
        if note_type is None or content is None:
            return "缺少参数 note_type 或 content"
        return create_note(str(note_type), str(content))

    if name == "save_profile":
        category = args.get("category")
        content = args.get("content")
        if category is None or content is None:
            return "缺少参数 category 或 content"
        return save_profile(str(category), str(content))

    # 未知工具名说明 schema 与调用不同步，让模型看到错误后再选择
    return "未知工具：{}，可用工具：search_journal / create_note / save_profile".format(name)
