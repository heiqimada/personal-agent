"""把印象笔记导出的 .enex 转成本地 txt（标准库实现，零第三方依赖）。

设计要点：
    1. .enex 本质是 XML：每条 <note> 含 <title>/<created>/<content>；
       <content> 里是 ENML/XHTML，正文转纯文本规则与备忘录导出共用
       scripts/export_apple_notes.py 里的 html_to_text/clean_component，
       避免同一套 HTML 清洗逻辑在两份脚本里各写一份产生漂移；
    2. 输出到 imports/evernote/{yyyy-mm-dd}_{标题清洗}.txt，
       子目录 evernote 正好满足 import_txt.py 的 source 推断
       （父目录路径 = evernote）；
    3. 日期取 <created> 前 8 位 yyyyMMdd，转成 yyyy-mm-dd；
       图片/附件 en-media 替换为 [图片] 占位（文字管道只收文字）。

⚠️ 真实数据由人手动导出：
    印象笔记桌面端选中笔记/笔记本 → 文件 → 导出 → 格式选 .enex，
    导出后把文件路径传给本脚本。本脚本不碰印象笔记 App，只读本地文件。

用法：
    python scripts/convert_enex.py 路径/xxx.enex
    python scripts/convert_enex.py 路径/xxx.enex /tmp/enex_out   # 指定输出目录（自测用）
"""

import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

# scripts/ 下的兄弟模块导出函数复用（本文件被当作脚本直接运行）
sys.path.insert(0, str(Path(__file__).resolve().parent))
from export_apple_notes import clean_component, html_to_text  # noqa: E402


ROOT_DIR = Path(__file__).resolve().parent.parent  # 项目根目录
OUTPUT_ROOT = ROOT_DIR / "imports" / "evernote"  # 印象笔记 txt 输出根目录


def parse_created(created):
    """把 .enex 的 <created>（如 20260801T120000Z）转成 yyyy-mm-dd。

    只取前 8 位并按格式重排；不是合法日期形态时返回空串，
    由调用方决定用「无日期」前缀还是跳过。
    """
    match = re.match(r"^(\d{4})(\d{2})(\d{2})", created or "")
    if not match:
        return ""
    year, month, day = match.groups()
    return "{}-{}-{}".format(year, month, day)


def _claim_output_path(output_dir, date_str, title, claimed):
    """处理同日期/同标题重名：追加 -2、-3……并登记本次占用。

    幂等策略与备忘录导出一致：磁盘已有但本轮未认领的同名文件视为
    上次导出，直接覆盖；本轮已认领才追加序号。
    """
    if date_str and title:
        base = "{}_{}".format(date_str, title)
    elif date_str:
        base = date_str
    else:
        base = title or "无标题"
    candidate = output_dir / (base + ".txt")
    suffix = 2
    while candidate in claimed:
        candidate = output_dir / "{}-{}.txt".format(base, suffix)
        suffix += 1
    claimed.add(candidate)
    return candidate


def convert_enex(enex_path, output_dir=None):
    """解析单个 .enex，输出 txt；返回 (转换条数, 跳过空笔记数, 路径列表)。

    参数：
        enex_path：.enex 文件路径；
        output_dir：输出目录；不传时默认 imports/evernote，
            自测可传临时目录避免污染 imports/。
    """
    # 文件不存在时报清晰错误；真实数据由人导出，路径写错要立刻可发现
    if not enex_path.exists():
        raise FileNotFoundError("找不到 .enex 文件：{}".format(enex_path))
    if not enex_path.is_file():
        raise ValueError("路径不是文件：{}".format(enex_path))

    # ET 只负责解析 XML 骨架；<content> 里的 HTML 交给 html_to_text
    tree = ET.parse(str(enex_path))
    notes = tree.findall(".//note")
    if not notes:
        raise ValueError(".enex 中没有找到任何 <note>，请检查导出文件")

    claimed = set()  # 本轮已写过的输出路径，防同轮互相覆盖
    converted = 0
    skipped = 0
    paths = []
    for note in notes:
        title = (note.findtext("title") or "").strip()
        date_str = parse_created(note.findtext("created") or "")
        content_html = note.findtext("content") or ""
        text = html_to_text(content_html)
        if not text:
            # 只有标题没有正文的笔记跳过，语义与 import_txt.py 空文件一致
            skipped += 1
            print("  ⚠️  跳过空笔记：{}".format(title or date_str or "无标题"))
            continue

        title_clean = clean_component(title) or "无标题"
        output_dir = output_dir or OUTPUT_ROOT
        output_dir.mkdir(parents=True, exist_ok=True)  # 幂等建目录
        output_path = _claim_output_path(
            output_dir, date_str, title_clean, claimed
        )
        output_path.write_text(text + "\n", encoding="utf-8")
        converted += 1
        paths.append(str(output_path))
        print("  ✅ {}".format(output_path))

    return converted, skipped, paths


def main():
    """命令行入口：python scripts/convert_enex.py 路径/xxx.enex。"""
    if len(sys.argv) < 2:
        # 用法提示放在 stderr，避免和正常导出日志混在一起
        print(
            "用法：python scripts/convert_enex.py 路径/xxx.enex [输出目录]",
            file=sys.stderr,
        )
        sys.exit(2)

    enex_path = Path(sys.argv[1])
    output_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else None
    try:
        converted, skipped, _paths = convert_enex(enex_path, output_dir)
    except (FileNotFoundError, ValueError) as exc:
        # 文件缺失/格式错误属于用户输入问题，给清晰提示后非零退出
        print("❌ {}".format(exc), file=sys.stderr)
        sys.exit(1)

    print("\n完成：共转换 {} 条，跳过空笔记 {} 条".format(converted, skipped))


if __name__ == "__main__":
    main()
