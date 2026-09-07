"""把 imports/ 下所有 *.txt（含子目录）逐篇 POST 到 /api/import 导入。

职责（V2.6）：
    1. 递归遍历 imports/ 子目录（rglob），苹果备忘录/印象笔记导出文件
       都在子目录里，旧的只扫根目录 glob 会漏掉；
    2. 文件名（不含 .txt）作为 title；以 yyyy-mm-dd 开头时解析 written_at；
       无标题后缀的纯日期文件名（如 2026-08-01.txt）标题缺省回退为日期串；
    3. source 按相对 imports/ 的父目录推断：根目录散文件 = manual，
       子目录文件用父目录路径（如 apple_notes/自我思考记）作为来源；
    4. 依赖服务端 /api/import 的幂等 upsert 判重，响应 created 字段
       区分「新增」与「更新（已存在，覆盖）」。需要先启动 main.py 服务。

用法：
    source venv/bin/activate
    python scripts/import_txt.py
"""

import json
import re             # 从文件名解析 yyyy-mm-dd 日期
import sys            # 遇到导入失败时以非零码退出
import urllib.request # 用标准库发 HTTP POST，避免额外依赖 requests
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent  # 项目根目录
IMPORTS_DIR = ROOT_DIR / "imports"                 # 待导入 txt 所在目录
API_ENDPOINT = "http://127.0.0.1:8000/api/import"  # 本地服务地址（默认端口 8000）


def import_file(path: Path):
    """导入单个 txt，返回 (journal_id, created)；空文件返回 (None, False)。"""
    content = path.read_text(encoding="utf-8").strip()  # 读正文并去掉首尾空白
    if not content:
        print(f"  ⚠️  {path.name} 为空，跳过")
        return None, False  # 空文件不入库

    title = path.stem  # 文件名去掉 .txt 后作为标题
    # 若文件名以日期开头（如 2026-08-01 或 2026-08-15_复盘），
    # 把开头日期解析为 written_at；纯日期文件名时 title 本身就是日期串，
    # 天然满足「无标题后缀时标题缺省回退为日期串」
    date_match = re.match(r"^(\d{4}-\d{2}-\d{2})", title)
    written_at = date_match.group(1) if date_match else ""  # 不是日期名就留空

    # source 推断：根目录 = manual；子目录 = 相对 imports/ 的父目录路径
    # （如 apple_notes/自我思考记），正斜杠跨平台统一
    rel_parent = path.parent.relative_to(IMPORTS_DIR)
    source = "manual" if str(rel_parent) == "." else rel_parent.as_posix()

    # 组装 /api/import 请求体
    payload = {
        "content": content,
        "title": title,
        "source": source,
        "written_at": written_at,
    }
    # 构造 POST 请求：JSON 序列化时保留中文（ensure_ascii=False）
    request = urllib.request.Request(
        API_ENDPOINT,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    # 发送请求并读取 JSON 响应；导入或网络异常会抛出异常交给上层处理
    with urllib.request.urlopen(request, timeout=120) as response:
        result = json.loads(response.read().decode("utf-8"))

    # created=True 表示服务端新增；False 表示命中同键旧行并覆盖正文
    is_created = bool(result.get("created"))
    action = "新增" if is_created else "更新（已存在，覆盖）"
    # 相对路径打印便于和目录结构对应；journal_id 用于后续核验
    print(
        f"  ✅ {path.relative_to(IMPORTS_DIR)} → "
        f"journal_id={result.get('journal_id')}（{action}）"
    )
    return result.get("journal_id"), is_created


def main():
    # rglob 递归收集全部 txt（含子目录）；按相对路径排序保证顺序稳定
    txt_files = sorted(
        (path for path in IMPORTS_DIR.rglob("*.txt") if path.is_file()),
        key=lambda p: p.relative_to(IMPORTS_DIR).as_posix(),
    )
    if not txt_files:
        print(f"未在 {IMPORTS_DIR}（含子目录）找到任何 .txt 文件")
        return  # 没有文件时安静退出

    print(f"导入 {IMPORTS_DIR}（含子目录）下的 {len(txt_files)} 篇日记：")
    ids = []  # 收集所有成功的 journal_id，最后统一汇总
    created_count = 0  # 新增篇数；其余为幂等更新，用于日志快速判断
    for path in txt_files:
        try:
            journal_id, is_created = import_file(path)
            if journal_id is not None:
                ids.append(journal_id)
            if is_created:
                created_count += 1
        except Exception as exc:
            # 任一篇失败即中止，避免“半成功”状态难排查
            print(f"  ❌ {path.name} 导入失败：{exc}")
            sys.exit(1)

    print(
        f"\n完成：共 {len(ids)} 篇入库（新增 {created_count} 篇，"
        f"更新 {len(ids) - created_count} 篇）；journal_ids={ids}"
    )


if __name__ == "__main__":
    # 允许直接执行：python scripts/import_txt.py
    main()
