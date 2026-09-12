"""冷启动档案灌库【公开示例版】。

本文件是 scripts/seed_profiles.py 的公开示例：目录定位、幂等设计、
执行方式与真实脚本完全一致，仅档案内容替换为虚构数据。

它演示的是"冷启动档案"这一设计：新用户首次使用前，把简历、近期
关键经历等一次性提炼成一批 profile 灌入档案层（profiles 表），
Agent 从第一轮对话起就带着用户背景工作；之后档案由 save_profile
工具在对话中持续生长。

真实档案含个人隐私，不入库 git（scripts/seed_profiles.py 已在
.gitignore 中排除）。你可以复制本文件为 scripts/seed_profiles.py，
把 PROFILES 换成自己的档案后运行。

用法：
    source venv/bin/activate
    python scripts/seed_profiles.example.py
"""

import os
import sys

# 直接执行时 Python 只把 scripts/ 加入搜索路径；
# 必须把项目根目录也加进去，才能 import 到同仓库的 db.py
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db


# source 固定标识本批次，DELETE 时只删"本脚本写过的数据"，
# 不误伤手动档案或 Agent 运行时通过 save_profile 写入的画像；
# 同一个标记也会写进 profile_history.source，审计时一眼能区分
# 「冷启动灌进来的种子」和「Agent 在对话里新加的画像」
SEED_SOURCE = "seed_example"


# 虚构示例档案：覆盖 profiles 表约定的 8 个 category
# （goal/pain_point/relationship/preference/skill/wish/status/decision）
PROFILES = [
    # ===== status 现状 =====
    ("status", "示例用户是一名 2026 届计算机专业本科毕业生，正在找后端开发方向的工作"),
    ("status", "示例用户住在广州，和两位大学同学合租"),
    # ===== goal 目标 =====
    ("goal", "示例用户希望 2026 年内拿到后端开发工程师 offer，期望月薪 10-15K"),
    ("goal", "示例用户计划一年内独立维护一个开源项目并积累 100 star"),
    # ===== pain_point 卡点 =====
    ("pain_point", "示例用户面试做算法题容易紧张，简单题也会写得磕磕绊绊"),
    ("pain_point", "示例用户觉得自己系统设计经验不足，没做过真正高并发的线上系统"),
    # ===== relationship 关系 =====
    ("relationship", "示例用户和父母关系融洽，父母支持他在大城市先闯两年"),
    # ===== preference 偏好 =====
    ("preference", "示例用户偏好远程友好的团队，习惯先写设计文档再动手写代码"),
    # ===== skill 技能 =====
    ("skill", "示例用户熟悉 Python 和 FastAPI，做过两个 RAG 方向的课程项目"),
    ("skill", "示例用户英语读写流利，能直接阅读英文技术文档和 GitHub issue"),
    # ===== wish 愿望 =====
    ("wish", "示例用户想养一只橘猫，计划工作稳定后领养"),
    # ===== decision 决定 =====
    ("decision", "示例用户决定 2026 年秋招只投后端和 AI 应用两个方向，不投测试岗"),
]


def seed_profiles():
    """幂等灌库：先删除本 source 的旧档案，再在同一事务内批量写入。

    返回：{"deleted": 删除条数, "inserted": [{"id", "category", "preview"}, ...]}。

    写库动作整体下沉到 db.replace_profiles_by_source()：
    项目约定「业务写操作只能经 db.py」，脚本自己拼 DELETE/INSERT 会绕过
    画像修订历史（profiles 与 profile_history 必须成对写入）。
    这里只负责把本批档案打包交给数据层，并在返回值里补上给人看的预览串。
    """
    result = db.replace_profiles_by_source(SEED_SOURCE, PROFILES)
    result["inserted"] = [
        {
            "id": item["id"],
            "category": item["category"],
            # 只保留前 20 字用于日志回显，避免整段画像刷屏
            "preview": item["content"][:20]
            + ("…" if len(item["content"]) > 20 else ""),
        }
        for item in result["inserted"]
    ]
    return result


def main():
    """执行灌库并打印统计与逐条结果。"""
    result = seed_profiles()
    inserted = result["inserted"]

    print(
        "示例档案灌库完成：删除旧种子 {} 条，新写入 {} 条（source={}）".format(
            result["deleted"],
            len(inserted),
            SEED_SOURCE,
        )
    )
    for item in inserted:
        print(
            "id={} category={} content={}".format(
                item["id"],
                item["category"],
                item["preview"],
            )
        )


if __name__ == "__main__":
    main()
