#!/usr/bin/env python3
"""数据库备份脚本：给 data/agent.db 做在线备份，滚动保留最近 10 份。

用法（在项目根目录执行）：
    source venv/bin/activate
    python scripts/backup_db.py
    python scripts/backup_db.py --keep 20          # 想多留几份
    python scripts/backup_db.py --db 其他库.db     # 备份别的库（默认 data/agent.db）
    python scripts/backup_db.py --out-dir /tmp/bak # 换备份目录

产出：data/backups/agent_YYYYMMDD_HHMMSS.db，备份完成后打印文件路径、
大小和 PRAGMA integrity_check 结果（不是 ok 就以非 0 退出，避免把坏备份
当成功留在盘上）。

为什么用 sqlite3 的在线备份 API（conn.backup）而不是 cp / rsync：
    cp 是「按字节拷贝」——如果拷贝时正好有连接在写（Agent 正在回复、
    前端在勾待办），拷到的是撕裂的中间状态，文件可能直接打不开或丢页。
    SQLite 的 backup API 会在源库上加读锁、按页把一致快照写进目标库，
    能安全地在服务运行中执行；这也是本项目「随时可备份」的前提。

为什么保留最近 N 份而不是只留一份：
    只留一份的话，如果坏数据是在上一次备份之前写进去的（比如今天才发现
    三天前的画像被改歪），唯一那份备份里同样是坏的。留一个滚动窗口，
    才有多几个时间点可选。

为什么不按文件修改时间排序清理：备份文件名里就带时间戳，
    按名字从大到小排就是时间顺序；mtime 会被复制/同步工具改写，
    用它排序反而可能删错对象。
"""

import argparse
import os
import re
import sqlite3
import sys
from datetime import datetime

# 直接运行时 Python 只把 scripts/ 加进搜索路径，这里手动补项目根，
# 保证从任意工作目录执行都能定位到 data/agent.db
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB_PATH = os.path.join(BASE_DIR, "data", "agent.db")
DEFAULT_BACKUP_DIR = os.path.join(BASE_DIR, "data", "backups")

# 备份文件名前缀与保留份数：前缀用于清理时只认自己的产物，
# 不会误删用户手动放进同目录的其他文件
BACKUP_PREFIX = "agent_"
KEEP_DEFAULT = 10

# 只有本脚本自己生成的备份名才参与清理：agent_20260913_000830.db
# （同秒连跑时会有 agent_..._1.db 后缀）。
# 为什么用正则卡死格式，而不是只判断前缀+后缀：用户可能手工把备份
# 另存成 agent_03迁移前.db 这种带意义的名字，泛匹配会把它算进
# 「最近 N 份」里，滚动清理时反而先把人工留的档删掉
BACKUP_NAME_RE = re.compile(r"^agent_\d{8}_\d{6}(_\d+)?\.db$")


def make_backup(db_path=DEFAULT_DB_PATH, backup_dir=DEFAULT_BACKUP_DIR, keep=KEEP_DEFAULT):
    """用在线备份 API 备份数据库，并滚动清理超过 keep 份的旧备份。

    参数：
        db_path：源数据库路径（默认 data/agent.db）；
        backup_dir：备份目录，不存在时自动创建；
        keep：保留最近多少份（含本次新建的这份）。
    返回：本次备份文件的绝对路径。
    副作用：在 backup_dir 下新建备份文件；删除超出保留数的旧备份文件。
    异常：源库不存在、备份失败或备份文件完整性检查不过关时抛异常。
    """
    if not os.path.exists(db_path):
        # 明确报错而不是「建一个空备份」：空备份比没有备份更危险，
        # 将来恢复时才发现里面什么都没有
        raise FileNotFoundError("源数据库不存在：" + db_path)

    os.makedirs(backup_dir, exist_ok=True)
    target_path = _unique_backup_path(backup_dir)

    # 源库连接只用于读出快照；目标库连接负责承接写入。
    # 两个连接都必须正常关闭，否则 Windows 下会留下文件占用
    source_conn = sqlite3.connect(db_path)
    target_conn = sqlite3.connect(target_path)
    try:
        source_conn.backup(target_conn)  # 一致快照，不怕源库正在被写
        target_conn.commit()
    finally:
        target_conn.close()
        source_conn.close()

    # 备份立刻做一次完整性检查：宁可现在失败，也不要留一个坏文件
    _assert_integrity(target_path)
    _prune_old_backups(backup_dir, keep)
    return target_path


def _unique_backup_path(backup_dir):
    """生成不冲突的备份文件路径（同一秒内连跑两次也不会互相覆盖）。

    参数：backup_dir——备份目录（调用方保证已存在）。
    返回：绝对路径，形如 data/backups/agent_20260913_101500.db。

    为什么允许重名时加后缀：秒级时间戳对人工操作足够，
    但本脚本可能被定时任务和手工操作同时触发；直接覆盖会把前一份
    备份悄悄替换掉，「保留最近 10 份」就名不副实了。
    """
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = os.path.join(backup_dir, BACKUP_PREFIX + stamp)
    candidate = base + ".db"
    suffix = 1
    while os.path.exists(candidate):
        candidate = "{}_{}.db".format(base, suffix)
        suffix += 1
    return candidate


def _assert_integrity(db_path):
    """对备份文件跑 PRAGMA integrity_check，不是 ok 就抛异常。

    参数：db_path——刚生成的备份文件路径。
    返回：无（校验通过）。
    异常：数据库打不开或校验结果不是 'ok' 时抛 sqlite3.DatabaseError。
    """
    conn = sqlite3.connect(db_path)
    try:
        # integrity_check 默认只返回一行 'ok'；返回多行说明有问题
        # （哪张表/哪个索引坏了），把这些行一起抛出去，方便定位
        result = conn.execute("PRAGMA integrity_check").fetchall()
    finally:
        conn.close()
    rows = [str(row[0]) for row in result]
    if rows != ["ok"]:
        raise sqlite3.DatabaseError(
            "备份完整性检查未通过：" + "；".join(rows)
        )


def _prune_old_backups(backup_dir, keep):
    """只保留最近 keep 份备份，更老的删除。

    参数：
        backup_dir：备份目录；
        keep：保留份数（<=0 视为不清理，用于调试）。
    返回：被删除的文件路径列表。
    副作用：删除磁盘上的旧备份文件（不可恢复，所以只删本脚本命名的文件）。

    删除是全局最危险的动作，这里用三重限制收窄打击面：
    只匹配本脚本生成的时间戳文件名（BACKUP_NAME_RE）、只看本目录、
    排序后从第 keep 份开始删——用户手工另存的备份文件不在此列。
    """
    if keep <= 0:
        return []

    names = [name for name in os.listdir(backup_dir) if BACKUP_NAME_RE.match(name)]
    # 文件名里就是 yyyyMMdd_HHmmss 时间戳，字典序倒排 = 新→旧
    names.sort(reverse=True)

    removed = []
    for name in names[keep:]:
        path = os.path.join(backup_dir, name)
        os.remove(path)
        removed.append(path)
    return removed


def main():
    """解析命令行参数、执行备份并打印结果。"""
    parser = argparse.ArgumentParser(
        description="备份 personal-agent 的 SQLite 数据库（在线备份 + 滚动保留）"
    )
    parser.add_argument("--db", default=DEFAULT_DB_PATH, help="源数据库路径")
    parser.add_argument("--out-dir", default=DEFAULT_BACKUP_DIR, help="备份目录")
    parser.add_argument(
        "--keep",
        type=int,
        default=KEEP_DEFAULT,
        help="保留最近多少份备份（默认 10）",
    )
    args = parser.parse_args()

    try:
        target_path = make_backup(args.db, args.out_dir, args.keep)
    except (OSError, sqlite3.Error) as exc:
        # 备份失败必须以非 0 退出：这样放进定时任务/cron 时能触发告警，
        # 而不是"看起来每天都有日志"却其实一直没备份成功
        print("备份失败：{}".format(exc), file=sys.stderr)
        return 1

    size_kb = os.path.getsize(target_path) / 1024
    remaining = [
        name for name in os.listdir(args.out_dir) if BACKUP_NAME_RE.match(name)
    ]
    print("备份完成：{}（{:.1f} KB，integrity_check=ok）".format(
        target_path, size_kb
    ))
    print("当前保留 {} 份备份（上限 {}）".format(len(remaining), args.keep))
    return 0


if __name__ == "__main__":
    sys.exit(main())
