"""对话与工具调用共用的 LLM 客户端（V1 保留，V2 扩展 chat_raw）。

默认走 DeepSeek 云端接口；模型名与密钥来自环境变量。

想切到本地 Ollama 时，把下方三处常量改成：
    LLM_BASE_URL = "http://localhost:11434/v1"
    LLM_MODEL    = "qwen2.5:3b"
    LLM_API_KEY  = "ollama"
并在启动前执行 ollama pull qwen2.5:3b。
"""

import os

from openai import OpenAI

# ===== 对话模型配置（默认 DeepSeek 云端） =====
LLM_BASE_URL = "https://api.deepseek.com/v1"
LLM_MODEL = "deepseek-chat"
LLM_API_KEY_ENV = "DEEPSEEK_API_KEY"  # API Key 从该环境变量读取，避免硬编码

# 本地 Ollama（切换示例，见文件顶部注释）
# LLM_BASE_URL = "http://localhost:11434/v1"
# LLM_MODEL = "qwen2.5:3b"
# LLM_API_KEY_ENV = None


# 进程级缓存：同一个 OpenAI 客户端复用于所有请求
_client = None


def _get_client() -> OpenAI:
    """惰性创建 OpenAI SDK 客户端（DeepSeek / Ollama 均兼容）。"""
    global _client
    if _client is None:
        # 只有在真正调用前才读取 Key：Key 缺失时给出明确报错
        api_key = os.environ.get(LLM_API_KEY_ENV, "")
        if not api_key:
            raise RuntimeError(
                f"缺少环境变量 {LLM_API_KEY_ENV}；"
                "或按文件顶部注释切换到本地 Ollama。"
            )
        # base_url 可同时兼容 DeepSeek 与 Ollama 的 /v1 开放接口
        _client = OpenAI(base_url=LLM_BASE_URL, api_key=api_key)
    return _client


def chat(messages, temperature: float = 0.7) -> str:
    """调用对话模型，返回文本回复。

    messages：OpenAI 风格的消息列表，例如
        [{"role": "system", "content": "系统提示"},
         {"role": "user", "content": "用户问题"}]
    temperature：采样温度，默认 0.7（数值越大回答越发散）。
    """
    api_key = os.environ.get(LLM_API_KEY_ENV, "")
    if not api_key:
        # 未配置 Key 时提前失败，避免把无意义请求发到远端
        raise RuntimeError(
            f"缺少环境变量 {LLM_API_KEY_ENV}；请在 shell 中 export 后重试，"
            "或按文件顶部注释切换到本地 Ollama。"
        )
    response = _get_client().chat.completions.create(
        model=LLM_MODEL,        # 使用当前配置的对话模型
        messages=messages,      # 按顺序传入完整对话上下文
        temperature=temperature,  # 应用调用方指定的采样温度
    )
    # 模型可能返回空内容，统一兜底为空字符串
    return response.choices[0].message.content or ""


def chat_raw(messages, tools=None, temperature: float = 0.7):
    """底层聊天调用：需要时把工具 schema 一并交给模型，返回原始消息对象。

    参数：
        messages：OpenAI 风格消息列表（含 system/user/assistant/tool）；
        tools：OpenAI function-calling 格式的工具列表；传 None 表示纯聊天；
        temperature：采样温度。
    返回：choices[0].message 原始对象，可通过 .content 取文本、
        .tool_calls 取工具调用（没有工具调用时为 None/空）。
    """
    # 关键设计：只有显式传入 tools 才附加 tools 参数，
    # 否则 V1 的普通 chat 语义会因多余的 tool_choice 被破坏
    kwargs = {
        "model": LLM_MODEL,
        "messages": messages,
        "temperature": temperature,
    }
    if tools is not None:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"  # 是否调用工具完全交给模型判断
    response = _get_client().chat.completions.create(**kwargs)
    # 返回 message 而非字符串：Agent 循环需要同时读取回复和工具调用
    return response.choices[0].message
