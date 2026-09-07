"""会话记忆组装：把“系统人设 + 用户档案 + 近期对话”拼成发给 LLM 的消息。

历史消息只允许 user/assistant 两类角色进入对话上下文；
tool 消息必须紧跟带 tool_call_id 的 assistant 消息才能被 API 接受，
因此旧的 tool 消息一律不从这里回放（V2 每轮对话重新生成 tool 上下文）。
"""

import os

import db


# 历史消息滑动窗口大小 = 最近 6 轮对话（1 问 1 答算 1 轮）。
# 为什么放在 memory.py 而不是 db.py：窗口大小是“组装上下文”的业务策略，
# 未来若按 token 预算截断，就会用 token 数替代这里的条数上限，
# 到时只需改 memory 层，不必动纯取数的 db 层。
MAX_HISTORY_MESSAGES = 12


# 相对本文件定位 prompts 目录，保证从任意工作目录启动都能读到
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SYSTEM_PROMPT_PATH = os.path.join(BASE_DIR, "prompts", "system.txt")


def load_system_prompt():
    """读取 prompts/system.txt，并拼上当前用户档案作为长期记忆。

    返回：人设文本 + “## 用户档案（长期记忆）”分节的完整 system 内容。
    """
    # 人设规则放 prompts/system.txt，方便不改代码就能调整 Agent 行为
    with open(SYSTEM_PROMPT_PATH, "r", encoding="utf-8") as file:
        persona = file.read().strip()

    profiles = db.list_active_profiles()
    if profiles:
        # 每行一种档案：带 category 前缀便于模型理解归类依据
        archive_lines = [
            "- [{}] {}".format(profile["category"], profile["content"])
            for profile in profiles
        ]
        archive_text = "\n".join(archive_lines)
    else:
        # 空档案也要给模型明确信号，避免它误以为没有数据支撑而拒绝作答
        archive_text = "（暂无档案）"

    # 人设与档案之间必须留空行分隔，模型才容易把档案当成“数据库”而非人设正文
    return persona + "\n\n## 用户档案（长期记忆）\n" + archive_text


def build_messages(session_id, user_message):
    """构造一次对话的完整消息列表。

    参数：
        session_id：会话 ID，用于取历史；
        user_message：本条用户输入。
    返回：OpenAI 风格消息列表
        [{"role": "system", ...}, ...历史 user/assistant..., {"role": "user", ...}]。
    """
    messages = [{"role": "system", "content": load_system_prompt()}]
    # recent_messages 内部已把 role 限定为 user/assistant 并转成时间正序；
    # 红线：绝不能把 role=tool 的历史消息直接拼进来——tool 消息必须跟在
    # 带 tool_call_id 的 assistant 消息后面，脱离上下文回放会触发 API 校验错误
    messages.extend(
        db.recent_messages(session_id, MAX_HISTORY_MESSAGES)  # 显式传窗口常量
    )
    messages.append({"role": "user", "content": user_message})
    return messages
