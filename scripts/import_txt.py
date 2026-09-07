"""把 imports/*.txt 逐篇 POST 到 /api/import 导入。

文件名（不含 .txt）作为 title；若文件名以 yyyy-mm-dd 开头则同时用作
written_at；source 固定为 "manual"。需要先启动 main.py 服务。

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
    """导入单个 txt，返回服务端响应中的 journal_id。"""
    content = path.read_text(encoding="utf-8").strip()  # 读正文并去掉首尾空白
    if not content:
        print(f"  ⚠️  {path.name} 为空，跳过")
        return None  # 空文件不入库

    title = path.stem  # 文件名去掉 .txt 后作为标题
    # 若文件名以日期开头（如 2026-08-01），顺手作为日记 written_at
    date_match = re.match(r"^(\d{4}-\d{2}-\d{2})", title)
    written_at = date_match.group(1) if date_match else ""  # 不是日期名就留空

    # 组装 /api/import 请求体
    payload = {
        "content": content,
        "title": title,
        "source": "manual",
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

    print(f"  ✅ {path.name} → journal_id={result.get('journal_id')}")
    return result.get("journal_id")  # 返回服务端分配的新日记 id


def main():
    # 按文件名排序遍历，保证导入顺序稳定、可重复
    txt_files = sorted(IMPORTS_DIR.glob("*.txt"))
    if not txt_files:
        print(f"未在 {IMPORTS_DIR} 找到任何 .txt 文件")
        return  # 没有文件时安静退出

    print(f"导入 {IMPORTS_DIR} 下的 {len(txt_files)} 篇日记：")
    ids = []  # 收集所有成功的 journal_id，最后统一汇总
    for path in txt_files:
        try:
            journal_id = import_file(path)
            if journal_id is not None:
                ids.append(journal_id)
        except Exception as exc:
            # 任一篇失败即中止，避免“半成功”状态难排查
            print(f"  ❌ {path.name} 导入失败：{exc}")
            sys.exit(1)

    print(f"\n完成，共导入 {len(ids)} 篇：journal_ids={ids}")


if __name__ == "__main__":
    # 允许直接执行：python scripts/import_txt.py
    main()
