"""V2 Agent 核心：多轮工具调用循环（不使用任何 Agent 框架）。

循环逻辑固定为：拼上下文 → 调 LLM → 若返回工具调用则执行并回填
tool 消息后进入下一轮，否则把最终回复存库并返回。最多 5 轮，
防止模型反复调用工具导致请求失控。
"""

import db
import llm
import memory
import tools


# 单次对话最多允许的工具调用轮数。
# 限制原因：模型可能陷入“反复检索/存库”的自我循环，
# 5 轮后强制收尾比无限重试更符合个人助手的交互体验
MAX_TOOL_ROUNDS = 5


def _message_to_jsonable(message):
    """把单条 LLM 消息转成可被 json.dumps 直接处理的纯 dict。

    参数：
        message：dict（system/user/tool）或 OpenAI SDK 返回的
            assistant message（pydantic 模型）。
    返回：纯 JSON 兼容的 dict，保留 role/content/tool_calls/
        tool_call_id 等全部字段；content 为 None 时保持 None。
    """
    # 自己拼的 dict 已经是 JSON 兼容结构，直接原样返回，
    # 避免多一层复制时意外丢失字段
    if isinstance(message, dict):
        return message

    # SDK 的 assistant message 为什么不能直接 json.dumps：
    # 它是 pydantic 模型对象，直接序列化会报“不是可 JSON 序列化对象”，
    # 必须先转成 dict；pydantic v2 提供 model_dump()，
    # 旧版 SDK/环境用 .dict()，这里按可用方法兼容
    if hasattr(message, "model_dump"):
        try:
            # mode="json" 让所有值（含 None）都输出为 JSON 原生类型
            return message.model_dump(mode="json")
        except TypeError:
            # 某些 pydantic 版本不支持 mode 参数，退回默认转 dict
            return message.model_dump()
    if hasattr(message, "dict"):
        return message.dict()
    raise TypeError("无法序列化的 LLM 消息类型：" + type(message).__name__)


def run_chat(session_id, user_message):
    """执行一次完整的 V2 Agent 对话，返回回复与工具调用轨迹。

    参数：
        session_id：会话 ID；
        user_message：用户本次输入。
    返回：{"reply": 最终回复文本,
          "tool_calls": 工具轨迹列表（旧字段，保持兼容）,
          "messages": 本轮最后一次发给模型的完整消息数组,
          "trace": 按时间顺序的模型/工具混合时间线,
          "user_message_id"/"assistant_message_id": 本轮落库的消息主键,
          前端拿它给气泡挂 id 锚点、给新记的笔记溯源}。
        工具轨迹元素为
        {"tool": 工具名, "args": 原始参数字符串, "result": 结果前 200 字}。
    """
    # 1) 先按“system + 历史 + 本轮问题”拼上下文；
    #    历史在 memory.build_messages 中已过滤为 user/assistant
    messages = memory.build_messages(session_id, user_message)

    # 2) 用户消息先落库；若后续 LLM 调用失败，问题也已在库里可追溯。
    #    记下返回的 id：本轮若调用 create_note 记计划，计划要指向
    #    「这条用户消息」，前端点击卡片才能跳回当前这轮对话
    user_message_id = db.save_message(session_id, "user", user_message)

    # tool_calls：只记录“执行过的工具”，兼容旧前端；
    # trace：记录“模型每轮思考 + 工具执行”的完整先后关系，
    # 两者都保留，前端新老字段都能渲染
    tool_trace = []
    trace = []
    reply = ""  # 默认空回复，循环内每轮都可能更新

    # 3) 工具调用循环：for-else 的 else 只在“5 轮用满仍未 break”时执行
    for step in range(MAX_TOOL_ROUNDS):
        # 每轮都把完整上下文（含上一轮 assistant 工具调用与 tool 结果）
        # 重新发给模型，模型才能基于结果决定继续调工具还是直接回答
        message = llm.chat_raw(messages, tools=tools.TOOLS)

        # 模型步必须“就地”追加而非循环结束后补记：
        # trace 的语义是逐轮发生的时间线，万一中途接入日志/异常处理，
        # 已发生的轮次也能被完整看到
        requested_names = [
            tool_call.function.name
            for tool_call in (message.tool_calls or [])
        ]
        trace.append(
            {
                "round": step,  # 轮次从 0 开始，前端展示时再加 1
                "kind": "model",
                # 截断 200 字：正文预览只为教学演示，
                # 不需要把整段最终回答复制进轨迹
                "content": (message.content or "")[:200],
                "tools_requested": requested_names,
            }
        )

        # 模型没有再要求调工具：本轮即为最终回答
        if not message.tool_calls:
            reply = message.content or ""
            break

        # 模型要求调工具：原始 message 自带 tool_calls 与 role=assistant，
        # 必须原样 append 进 messages（OpenAI API 要求 tool 结果紧跟它）
        messages.append(message)

        # 同一条 assistant 消息可能带多个工具调用，逐个执行并回填
        for tool_call in message.tool_calls:
            function = tool_call.function
            name = function.name
            args = function.arguments or ""
            print(
                "[agent] step{} 调用 {} args={}".format(step, name, args)
            )

            # dispatch 内部不抛异常：参数解析失败会返回错误文本，
            # 模型读到 tool 消息里的错误后可在下一轮自我纠正。
            # source_message_id 由这里注入（模型拿不到也猜不准主键）
            result = tools.dispatch(
                name, args, source_message_id=user_message_id
            )

            # 轨迹截断到 200 字：既保留前端可视化需要的要点，
            # 又避免把整段日记原文塞进 HTTP 响应
            tool_trace.append(
                {
                    "tool": name,
                    "args": args,
                    "result": result[:200],
                }
            )
            # 工具步紧跟对应模型步记录，round 复用当前轮次，
            # 前端据此把“模型说要调”和“实际执行结果”画在同一时间线节点
            trace.append(
                {
                    "round": step,
                    "kind": "tool",
                    "tool": name,
                    "args": args,
                    "result": result[:200],
                }
            )

            # tool 结果必须用 tool_call_id 关联刚追加的 assistant 消息，
            # 顺序与数量都要一一对应，否则 API 会拒绝请求
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": result,
                }
            )
            db.save_message(session_id, "tool", result, tool_name=name)
    else:
        # 5 轮用完仍在调工具：不再继续请求，给用户一个可操作的兜底文案
        reply = "我来回查了好几步还没组织好答案，换个问法再试试？"

    # 4) 只把最终 assistant 回复落库；
    #    中间轮 assistant 消息 content 为 None 且带 tool_calls，
    #    不落库是避免历史里出现无法独立回放的残缺消息
    # 回复也要 id：前端给它挂锚点，同一次会话里「继续聊」后的消息
    # 不必等刷新就能被计划卡片定位到
    assistant_message_id = db.save_message(session_id, "assistant", reply)

    # 5) 把“实际发给模型”的消息列表转成纯 JSON 结构：
    #    这里处于循环出口，messages 正好是最后一次 chat_raw 的入参，
    #    能完整展示 system/user/assistant/tool 四类消息如何逐轮累积
    jsonable_messages = [_message_to_jsonable(message) for message in messages]

    # 6) 返回给 main.py 的最终结构
    return {
        "reply": reply,
        "tool_calls": tool_trace,
        "messages": jsonable_messages,
        "trace": trace,
        "user_message_id": user_message_id,
        "assistant_message_id": assistant_message_id,
    }
