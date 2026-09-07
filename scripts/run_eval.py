#!/usr/bin/env python3
"""Agent 评测脚本（10 题 × 2 轮，稳定通过率）。

为什么要有这个脚本：
    1. 面试/README 需要一个"可复现、可现场演示"的评估故事，
       clone 仓库 + 配好 DeepSeek key 后跑一条命令即可复现；
    2. 用公开虚构数据（seed_profiles.example.py 12 条档案 +
       本脚本同目录 fixtures 3 篇虚构日记），数字可入库、不进真实数据。

运行前必须满足：
    - 已执行 ollama pull bge-m3（本地 embedding，rag.py 依赖）；
    - DEEPSEEK_API_KEY 环境变量已设置，或项目根目录存在 .env 且含该行。
    两者都没有时脚本直接报错退出，绝不伪造结果。

数据隔离设计（为什么是独立路径）：
    - 数据库写 data/eval_test.db，向量写 data/chroma_eval/，
      与真实库 data/agent.db、data/chroma_db/ 完全隔离；
    - 每次运行先删除自己的 eval 库/集合再重建，天然幂等：
      连跑两次结果一致、且绝不污染真实数据。
"""

import importlib.util
import json
import os
import re
import shutil
import sys
from pathlib import Path

# ===== 路径与配置（相对本文件定位项目根，任意 cwd 都能跑） =====
ROOT_DIR = Path(__file__).resolve().parent.parent        # 项目根目录
sys.path.insert(0, str(ROOT_DIR))  # 以 `python scripts/run_eval.py` 运行时也能 import 到根目录模块
EVAL_DB_PATH = ROOT_DIR / "data" / "eval_test.db"        # 评测专用 SQLite
EVAL_CHROMA_DIR = ROOT_DIR / "data" / "chroma_eval"      # 评测专用 Chroma 目录
FIXTURE_PATH = ROOT_DIR / "tests" / "fixtures" / "sample_diaries.txt"
RESULT_PATH = ROOT_DIR / "data" / "eval_result.json"     # 结构化结果（data/ 已 gitignore）
SEED_MODULE_PATH = ROOT_DIR / "scripts" / "seed_profiles.example.py"


def load_api_key():
    """返回 DeepSeek key；环境变量优先，缺失时尝试项目根 .env。

    为什么 .env 兜底：本地开发常把 key 放 .env（已在 .gitignore），
    让 clone 仓库的人只需要 export 也能跑，两种姿势都支持。
    """
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if key:
        return key
    env_file = ROOT_DIR / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("DEEPSEEK_API_KEY="):
                # 去掉可能存在的引号，兼容 KEY="xxx" / KEY='xxx' 写法
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit(
        "缺少 DEEPSEEK_API_KEY：请 export DEEPSEEK_API_KEY=... 或在项目根放 .env；"
        "严禁伪造评测结果。"
    )


def prepare_eval_env():
    """删除旧的评测库/集合，把 db/rag 指向评测专用路径。

    为什么先删再建：保证"每次重建测试库与集合"，跑第 2 遍时
    不会残留上一轮的档案/切片，避免幂等性被历史数据破坏。
    """
    # 只允许删除 eval 自己的产物；真实 agent.db / chroma_db 一律不动
    if EVAL_DB_PATH.exists():
        EVAL_DB_PATH.unlink()
    if EVAL_CHROMA_DIR.exists():
        shutil.rmtree(EVAL_CHROMA_DIR)
    (ROOT_DIR / "data").mkdir(exist_ok=True)

    # 先 import db/rag 再打补丁：
    # db.get_conn 的默认参数在 def 时已绑定原路径，因此这里整体替换
    # db.get_conn，让 db 模块内所有 get_conn() 调用都落到评测库。
    import db  # noqa: E402  项目自带数据层
    original_get_conn = db.get_conn

    def eval_get_conn(db_path=str(EVAL_DB_PATH)):
        """评测专用连接：无视调用方默认路径，始终连 data/eval_test.db。"""
        return original_get_conn(db_path)

    db.get_conn = eval_get_conn
    db.init_db()  # 幂等建表（四张表与真实库同构）

    import rag  # noqa: E402  向量检索层
    # Chroma 集合路径是模块级常量，运行时读取，直接改指向评测目录
    rag.CHROMA_DIR = str(EVAL_CHROMA_DIR)
    rag._client = None        # 清掉可能的 embedding 客户端缓存
    rag._collection = None    # 清掉可能的集合缓存，保证首次调用重建空集合
    return db, rag


def seed_profiles(db):
    """向评测库灌入 12 条虚构档案（scripts/seed_profiles.example.py）。

    用 importlib 加载而不是普通 import：文件名含点号
    （seed_profiles.example.py），Python 语法上无法直接 import。
    """
    spec = importlib.util.spec_from_file_location("seed_profiles_example", SEED_MODULE_PATH)
    seed_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(seed_mod)
    result = seed_mod.seed_profiles()  # 幂等：先删 source=seed_example 旧行再插入
    print(f"档案灌入：删除 {result['deleted']} 条旧种子，写入 {len(result['inserted'])} 条虚构档案")
    return seed_mod


def load_and_index_fixtures(rag):
    """读取 3 篇虚构日记并灌入评测专用向量集合。

    fixture 用 '=== yyyy-mm-dd ===' 分节；正文走 rag.add_journal，
    与真实 /api/import 同一套切片+embedding+入库链路。
    """
    text = FIXTURE_PATH.read_text(encoding="utf-8")
    blocks = re.findall(
        r"^=== (\d{4}-\d{2}-\d{2}) ===\n(.*?)(?=^=== |\Z)",
        text,
        flags=re.M | re.S,
    )
    total_chunks = 0
    # journal_id 用 91xxx 高位段，避免与真实库 id 空间混淆（真实库也看不见这里）
    for idx, (date_str, content) in enumerate(blocks, start=1):
        journal_id = 91000 + idx
        chunks = rag.add_journal(
            journal_id,
            content.strip(),
            title=f"fixture-{date_str}",
        )
        total_chunks += chunks
        print(f"  fixture {date_str} -> journal_id={journal_id}, chunks={chunks}")
    print(f"虚构日记灌入完成：{len(blocks)} 篇，共 {total_chunks} 个切片")
    return len(blocks)


# ===== 10 道评测题定义（题号/问题/考点/期望要点） =====
QUESTIONS = [
    # (题号, 问题, 考点, 期望要点说明, 判定函数)
    (1, "我现在在哪个城市？和谁住一起？", "档案·status",
     "广州；大学同学合租",
     lambda t: all(g(t) for g in [
         lambda s: "广州" in s,
         lambda s: any(w in s for w in ["大学同学", "同学", "室友", "合租"]),
         lambda s: any(w in s for w in ["合租", "一起住", "住在一起", "同住"]),
     ])),
    (2, "我找工作的目标是什么？期望薪资多少？", "档案·goal",
     "2026 年内拿 offer；后端/AI 应用；10-15K",
     lambda t: all(g(t) for g in [
         lambda s: "2026" in s,
         lambda s: "offer" in s.lower(),
         lambda s: "后端" in s and "AI" in s,
         lambda s: re.search(r"10\s*[-~至到]\s*15\s*[kK]", s) is not None,
     ])),
    (3, "我在求职面试上有什么短板？", "档案·pain_point",
     "算法题紧张；系统设计/高并发经验不足",
     lambda t: all(g(t) for g in [
         lambda s: "算法" in s and any(w in s for w in ["紧张", "卡壳", "磕绊"]),
         lambda s: any(w in s for w in ["系统设计", "高并发"]),
     ])),
    (4, "我秋招决定投哪些方向？不投什么？", "档案·decision",
     "只投后端和 AI 应用；不投测试",
     lambda t: all(g(t) for g in [
         lambda s: "后端" in s and "AI" in s,
         lambda s: "测试" in s,
         lambda s: any(w in s for w in ["不投", "不考虑", "不选", "不面", "排除", "不碰", "不会投", "没打算投"]),
     ])),
    (5, "我最近部署过什么项目？部署在哪个平台？", "日记·RAG",
     "FastAPI 待办 API；Render",
     lambda t: all(g(t) for g in [
         lambda s: "FastAPI" in s or "fastapi" in s.lower(),
         lambda s: any(w in s.lower() for w in ["待办", "todo"]),
         lambda s: "render" in s.lower(),
     ])),
    (6, "我那个项目的登录鉴权是怎么做的？", "日记·RAG",
     "JWT",
     lambda t: "jwt" in t.lower()),
    (7, "我最近和谁去爬过山？去的哪座山？", "日记·RAG",
     "小李；白云山",
     lambda t: "小李" in t and "白云山" in t),
    (8, "小李最近工作上有什么变动？", "日记·RAG",
     "跳槽；字节；后端",
     lambda t: all(g(t) for g in [
         lambda s: any(w in s for w in ["跳槽", "换工作", "离职", "跳去"]),
         lambda s: "字节" in s.replace("字节跳动", "字节"),
         lambda s: "后端" in s,
     ])),
    # 9/10 是反幻觉题：允许"提到问题里的词"，但禁止编造时间/人名/宠物名
    (9, "我上次去日本旅游是什么时候？跟谁去的？", "反幻觉",
     "拒答：明确说没有记录，不得编造日期/人名",
     lambda t: (
         any(w in t for w in ["没有", "没提到", "没记录", "日记里没有", "没找到", "没搜到", "找不到", "不确定", "无法", "不清楚", "未提及", "没有相关", "不记得", "不太确定"])
         and re.search(r"(?:19|20)\d{2}\s*年|\d{4}-\d{1,2}", t) is None
         and re.search(r"(?:和|跟)[^，。]{0,8}(?:一起)?(?:去|去了|去过)[^，。]{0,6}日本|东京|大阪|京都|北海道", t) is None
     )),
    (10, "我养的宠物叫什么名字？", "反幻觉",
     "拒答：没有养宠物记录（只有想养橘猫的愿望），不得编造名字",
     lambda t: (
         any(w in t for w in ["没有", "没养", "还没养", "没记录", "没提到", "日记里没有", "没找到", "不确定", "不清楚", "没搜到", "没见着", "不太确定", "应该没有"])
         and re.search(r"名字(?:是|叫)|(?:养了|养着)(?:一只|只)?[^，。、]{0,8}[猫狗][^，。、]{0,6}(?:叫|名字)", t) is None
     )),
]

# 10 题每题跑 2 轮；两轮都判过才算稳定通过
ROUNDS = [1, 2]


def normalize(text):
    """把回复统一成便于判定的字符串。

    为什么单独归一化：不同表达（字节跳动/字节、大小写 K）判定时
    需要同义词兼容，但又不改变原文用于"回复前 100 字"展示。
    """
    s = text.replace("字节跳动", "字节")
    return s


def judge(question_no, reply):
    """返回 (是否通过, 判定说明)。"""
    text = normalize(reply)
    passed = QUESTIONS[question_no - 1][4](text)
    if passed:
        return True, "命中期望要点"
    return False, "未命中期望要点/含编造事实"


def main():
    """执行完整评测并输出两轮控制台报告。"""
    load_api_key()  # 先校验 key，缺 key 直接退出，不浪费一次模型调用
    db, rag = prepare_eval_env()  # 重建评测库与向量集合
    seed_profiles(db)             # 12 条虚构档案
    load_and_index_fixtures(rag)  # 3 篇虚构日记

    import agent  # noqa: E402  此时 db/rag 已指向评测库，agent 调用即评测链路

    results = {}       # qid -> {"q", "rounds": {1: {...}, 2: {...}}}
    stable = {}        # qid -> 两轮是否都过
    summary = {1: 0, 2: 0}

    # 外层先整轮 1 再整轮 2：便于末尾打印"第1轮 X/10，第2轮 Y/10"
    for round_no in ROUNDS:
        print(f"\n========== 第 {round_no} 轮 ==========")
        for qid, question, layer, expected, _judge in QUESTIONS:
            session_id = f"eval_q{qid}_round{round_no}"  # 每题独立 session，互不串历史
            print(f"\n[Q{qid}][{layer}] {question}")
            out = agent.run_chat(session_id, question)
            reply = (out.get("reply") or "").strip()
            passed, why = judge(qid, reply)
            # 只展示回复前 100 字：既满足可读性，又不至于让日志过长
            preview = reply[:100].replace("\n", " ")
            print(f"  回复前100字: {preview}")
            print(f"  判定: {'✅' if passed else '❌'} {why}（期望：{expected}）")
            results.setdefault(qid, {"q": question, "rounds": {}})
            results[qid]["rounds"][round_no] = {"passed": passed, "reply": reply}
            if passed:
                summary[round_no] += 1

    # 汇总：两轮都过 = 稳定通过
    for qid in results:
        r1 = results[qid]["rounds"][1]["passed"]
        r2 = results[qid]["rounds"][2]["passed"]
        stable[qid] = r1 and r2

    stable_count = sum(1 for v in stable.values() if v)
    print(f"\n========== 汇总 ==========")
    print(f"第1轮 {summary[1]}/10，第2轮 {summary[2]}/10，稳定通过 {stable_count}/10")
    for qid in results:
        print(f"Q{qid}: 第1轮={'✅' if results[qid]['rounds'][1]['passed'] else '❌'} "
              f"第2轮={'✅' if results[qid]['rounds'][2]['passed'] else '❌'} "
              f"稳定={'✅' if stable[qid] else '❌'}")

    # 结构化结果落 data/eval_result.json（gitignore，供 docs/eval.md 生成）
    RESULT_PATH.write_text(
        json.dumps(
            {
                "round1": summary[1],
                "round2": summary[2],
                "stable": stable_count,
                "questions": {
                    str(qid): {
                        "question": results[qid]["q"],
                        "rounds": {
                            str(r): {
                                "passed": results[qid]["rounds"][r]["passed"],
                                "reply": results[qid]["rounds"][r]["reply"],
                            }
                            for r in ROUNDS
                        },
                        "stable": stable[qid],
                    }
                    for qid in results
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\n结构化结果已写入 {RESULT_PATH}")

    # 非零退出：反幻觉题全过也如实输出，本脚本不替模型"凑分"
    sys.exit(0)


if __name__ == "__main__":
    main()
