# Personal AI Agent

一个越用越懂你的个人 Agent：**日记为记忆、profile 为档案、工具为手脚、LLM 为大脑**。

它不是通用聊天机器人——通用能力交给大模型，个人维度做到任何通用助手都做不到：
能回答「我到底是谁」「我最近在为什么事焦虑」「我接下来该干什么」这类只有懂你的人才答得了的问题。

- **不依赖任何 Agent 框架**：工具调用循环、记忆装配、失败恢复全部手写，每个环节可控、可讲清
- **分层记忆**：档案常驻 system prompt / 最近对话滑窗 / 日记向量库按需检索
- **RAG 防幻觉**：相似度阈值过滤弱相关片段，日记里没有依据就明说，不编造
- **双模型可切换**：DeepSeek 云端开发保工具调用稳定性，Ollama 本地运行支持无网/私有化演示（按 llm.py 顶部注释改三处常量即可互切）

## 技术栈

| 层 | 选型 |
|---|---|
| Web 框架 | FastAPI + 单页前端（static/index.html） |
| 结构化存储 | SQLite（仅标准库 sqlite3），四张表 |
| 向量检索 | ChromaDB 持久化 + bge-m3 embedding（本地 Ollama） |
| LLM | DeepSeek API / Ollama 本地模型，均走 OpenAI 兼容接口 |
| Agent | 手写多轮 function-calling 循环，无 LangChain 等框架 |

## 架构

```
用户提问（浏览器 / Swagger）
        │
        ▼
FastAPI 接收 → Pydantic 校验
        │
        ▼
装配 messages（memory.py）
  · system：人设（prompts/system.txt）
  · system：profiles 表全部 active 档案   ← 档案层，精华常驻
  · 当前 session 最近 6 轮 messages        ← 短期记忆，session 间隔离
  · user：本次问题 + tools 工具说明书
        │
        ▼
Agent 循环（agent.py，最多 5 轮防失控）
  ① LLM 决策 ── 无 tool_calls ──→ 最终回答，跳出
  ② 有 tool_calls → 代码分发执行（tools.py，LLM 自己不执行）：
       search_journal → ChromaDB 语义检索日记（距离阈值 0.7 过滤）
       create_note    → notes 表写入待办/愿望/想法/情绪/计划
       save_profile   → profiles 表写入长期档案
  ③ 工具结果以 role="tool" 回灌消息（失败也转成文本，让模型自我纠正）
  ④ 回到 ①
        │
        ▼
本轮 user/assistant 消息落库 messages 表 → JSON 返回前端
```

**三层记忆 = 工作记忆 / 长期记忆 / 档案柜：**

| 层 | 存储 | 进 prompt 方式 |
|---|---|---|
| 档案层 | `profiles` 表 | 每次请求全量常驻 system |
| 短期层 | `messages` 表（当前 session 近 6 轮） | 自动截取，新 session 自动清空 |
| 素材层 | `journal` 表 + ChromaDB | Agent 调 `search_journal` 按需检索 |

话题断层靠 session 隔离解决：感情、工作、学习各开各的 session，短期记忆互不污染；
档案跨 session 常驻；旧事细节靠语义检索捞回。**话题跳跃是默认场景，不是边界情况。**

## 四张表

```sql
profiles   -- 档案层：一句话长期事实（goal/pain_point/relationship/preference/
           --         skill/wish/status/decision），每次对话全量进 system prompt
journal    -- 素材层：日记原文，切片向量化进 ChromaDB，metadata 带 journal_id 可回溯
notes      -- 待办/愿望/想法/情绪/计划，Agent 通过 create_note 随时写
messages   -- 对话流水，按 session_id 隔离；tool 消息记录工具名
```

所有表 V0 就带 `user_id` 字段，为多租户外拓提前埋点。

## 快速开始

```bash
# 1. 环境（Python 3.9+）
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# 2. Embedding 模型（RAG 检索依赖，本地 Ollama）
ollama pull bge-m3

# 3. 对话模型：二选一
export DEEPSEEK_API_KEY=sk-xxx          # 方案 A：DeepSeek 云端（默认）
# 方案 B：本地 Ollama——按 llm.py 顶部注释改三行常量，ollama pull qwen2.5:3b

# 4. 启动
uvicorn main:app --reload
# 打开 http://127.0.0.1:8000 （前端）或 /docs （Swagger）
```

**导入日记**：把 txt 放进 `imports/`（文件名以 `yyyy-mm-dd` 开头会自动记为日记日期），
服务启动状态下运行：

```bash
python scripts/import_txt.py
```

**冷启动档案**（可选，让 Agent 第一轮就懂你）：

```bash
python scripts/seed_profiles.example.py   # 公开示例：12 条虚构档案，可直接跑通看效果
# 正式使用：复制为 scripts/seed_profiles.py，换成自己的档案再运行（幂等，可重复执行）
```

## 关键设计决策

- **手写 Agent 循环而非上框架**：工具调用结果放哪、为什么会死循环、连续选错工具怎么办——
  每个环节都是自己代码里的显式逻辑（轮次上限、参数白名单校验、错误文本回灌、trace 时间线）。
- **RAG 防幻觉优先于召回率**：余弦距离 > 0.7 的片段直接丢弃，宁可回答「日记里没提到」，
  也不把弱相关内容当事实塞给模型。
- **tool 消息绝不回放进历史**：tool 消息必须跟在带 tool_call_id 的 assistant 消息后，
  脱离上下文回放会触发 API 校验错误——短期记忆只回放 user/assistant。
- **双模型可切换**：云端 API 保证开发效率和工具调用格式稳定性；
  本地 Ollama 支持无网环境和私有化演示（数据不出内网），OpenAI 兼容格式，按 llm.py 顶部注释改三处常量即可切换。
- **prompt 外置 + 错题本迭代**：人设规则放 `prompts/system.txt`，改行为不改代码；
  效果问题按「档案层 / 提示词层 / 工具层 / 模型层」四层定位，测试集回归防修好 A 弄坏 B。

## 目录结构

```
├── main.py        # FastAPI 入口：路由、CORS、启动建表、静态前端挂载
├── db.py          # SQLite 连接 + 四张表 DDL 与 CRUD
├── llm.py         # LLM 客户端（DeepSeek / Ollama 切换点）
├── agent.py       # Agent 核心：多轮 tool_calls 循环
├── tools.py       # 3 个工具实现 + function-calling schema
├── memory.py      # 记忆装配：档案常驻 + 最近 6 轮滑窗
├── rag.py         # 切片（500/50）+ bge-m3 向量化 + ChromaDB 检索
├── prompts/system.txt   # 人设与工具使用规则
├── scripts/
│   ├── import_txt.py            # imports/ 下 txt 批量导入
│   └── seed_profiles.example.py # 冷启动档案灌库（公开示例，真实档案不入库）
├── static/index.html     # 单页前端（对话 + 笔记/待办）
├── imports/              # 待导入日记 txt（git 忽略，含隐私）
└── data/                 # agent.db + chroma_db/（git 忽略）
```

## Roadmap

- [x] **V0 地基**：四张表 + FastAPI 骨架
- [x] **V1 RAG 问答**：日记导入 → 切片 → bge-m3 向量化 → ChromaDB 检索 → 基于日记回答
- [x] **V2 Agent 闭环**：手写工具调用循环 + 3 个工具 + session 隔离 + 冷启动档案
- [x] **V2.1 过程可视化**：前端 trace 时间线 + messages 调试视图，逐轮展示工具调用与结果
- [ ] **V3 反思生长**：每 N 轮对话提炼档案（add/update/archive）、`list_notes` / `make_plan` 工具、20 题测试集与错题本、公网穿透手机可访问
- [ ] **V4 外拓**：多源同步 adapter（苹果备忘录 AppleScript / 印象笔记 .enex / OneNote Graph API）、外接工具（web_search / 飞书 / 推送）、多租户与新用户冷启动引导、Docker 部署

## 隐私说明

本项目为个人工具，真实日记（`imports/`）、数据库（`data/`）和真实冷启动档案
（`scripts/seed_profiles.py`）均已在 `.gitignore` 中排除，仓库内只含代码与虚构示例数据。
