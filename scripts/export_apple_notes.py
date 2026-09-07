"""从 Apple「备忘录」导出全部/指定文件夹为 txt（标准库实现，零第三方依赖）。

设计要点：
    1. 输出目录 imports/apple_notes/{文件夹名}/{yyyy-mm-dd}_{标题清洗}.txt，
       与 import_txt.py 的「子目录名 = source」推断规则对应——导入后来源
       会显示成 apple_notes/自我思考记 这类可读路径；
    2. 日期一律取「创建日期」而不是修改日期：日记语义是「哪天落笔」；
    3. 正文是 Notes 返回的 HTML，转纯文本时图片留 [图片] 占位、
       br/div/p 等换行语义转成 \n；图片本身导不进 txt，占位让阅读时
       知道这里有附件；
    4. 脚本只负责把备忘录写成本地 txt，绝不触发导入、绝不改动笔记。

⚠️ 首次运行授权（必须在 Mac 终端由主人手动执行）：
    osascript 访问备忘录会触发系统「自动化」权限弹窗；第一次请在本机
    终端跑 `python scripts/export_apple_notes.py 自我思考记` 试水，然后到
    「系统设置 → 隐私与安全性 → 自动化」允许终端控制 Notes。授权后
    本脚本才能被 agent/定时任务安全调用。

用法：
    python scripts/export_apple_notes.py               # 导全部账户全部文件夹
    python scripts/export_apple_notes.py 自我思考记     # 只导同名文件夹
"""

from html.parser import HTMLParser
import re
import subprocess
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent  # 项目根目录
OUTPUT_ROOT = ROOT_DIR / "imports" / "apple_notes"  # 备忘录 txt 输出根目录

# 文件名非法字符：跨平台保留字符 + 换行；文件夹/标题清洗共用同一份
INVALID_FS_CHARS = re.compile(r'[/\\:*?"<>|\r\n]+')

# 块级标签：结束后补换行，避免 div/p 等内容粘连成一大段
BLOCK_TAGS = {
    "div", "p", "li", "tr", "table",
    "h1", "h2", "h3", "h4", "h5", "h6",
}


class _HTMLTextExtractor(HTMLParser):
    """把 Notes/enex 的 HTML 正文抽成纯文本。

    规则：
        - img / en-media 标签替换成 [图片]；
        - br 与块级结束标签补换行；
        - 其余标签只剥掉标签本身，保留文字；
        - HTMLParser 默认 convert_charrefs=True，&nbsp;/&amp; 等实体自动解码。
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []  # 逐段收集纯文本，最后一次性拼接

    def handle_starttag(self, tag, attrs):
        """开始标签：img/en-media 记占位，br 记换行。"""
        if tag == "img":
            self.parts.append("[图片]")
        elif tag == "en-media":
            # 印象笔记附件（含图片）统一留 [图片] 占位；
            # 文字管道只收文字，附件本身无法进 txt
            self.parts.append("[图片]")
        elif tag == "br":
            self.parts.append("\n")

    def handle_endtag(self, tag):
        """结束标签：块级标签补换行，保证 div/p 分段不粘连。"""
        if tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        """正文文本直接收集（HTML 实体已在解析层解码）。"""
        self.parts.append(data)


def html_to_text(html):
    """把 HTML 正文转纯文本并做空白归一化，供两个导出脚本共用。"""
    if not html:
        return ""
    parser = _HTMLTextExtractor()
    # Notes 偶发不闭合标签，HTMLParser.feed 能容错继续解析
    parser.feed(html or "")
    parser.close()
    text = "".join(parser.parts)
    # 归一化：去掉行尾空白，把 3 个以上连续换行压成 2 个，便于人读
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def clean_component(name, max_len=30):
    """清洗文件夹/标题中的文件系统非法字符并按字数截断。

    只保留 30 字内：避免标题把路径撑得不可读、超长文件名触发系统限制；
    截断前先清非法字符，保证不会产出 `/` 之类的伪子目录。
    """
    cleaned = INVALID_FS_CHARS.sub("", name or "")
    # 首尾空格和点去掉：路径组件以点结尾在某些文件系统有隐藏语义
    cleaned = cleaned.strip(" .")
    return cleaned[:max_len]


def _osascript(script):
    """执行一段 AppleScript，返回 stdout；失败抛清晰错误。"""
    # 统一走系统 osascript；自动化授权弹窗只在首次手动运行时出现
    proc = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
        timeout=180,
    )
    if proc.returncode != 0:
        # 报错原因多为权限未授权或 Notes 不可用，上抛让人处理
        raise RuntimeError("osascript 失败: {}".format(proc.stderr.strip()))
    return proc.stdout


def _apple_quote(value):
    """把 Python 字符串转义成 AppleScript 字符串字面量，用于内嵌脚本。"""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _notes_meta_script():
    """生成「枚举全部账户/文件夹下笔记元信息」的 AppleScript。

    每行四列（账户<TAB>文件夹<TAB>标题<TAB>yyyy-mm-dd）。标题等字段里的
    换行/TAB 先转义成字面 `\n`/`\t`，避免破坏按行解析；body 不在这一步取，
    因为正文含任意换行/制表符，混进同一行会无法可靠切分，所以单独按
    (账户, 文件夹, 文件夹内序号) 二次取正文。
    """
    # 注意 raw string 里的 "\n"/"\t" 是给 AppleScript 的字面两字符
    return r'''
on cleanField(theText)
    set AppleScript's text item delimiters to linefeed
    set pieces to every text item of theText
    set AppleScript's text item delimiters to "\n"
    set theText to pieces as text
    set AppleScript's text item delimiters to return
    set pieces to every text item of theText
    set AppleScript's text item delimiters to "\n"
    set theText to pieces as text
    set AppleScript's text item delimiters to tab
    set pieces to every text item of theText
    set AppleScript's text item delimiters to "\t"
    set theText to pieces as text
    set AppleScript's text item delimiters to ""
    return theText
end cleanField

on formatDate(d)
    set y to year of d
    set mo to (month of d) as integer
    set da to day of d
    if mo < 10 then set mo to "0" & (mo as text)
    if da < 10 then set da to "0" & (da as text)
    return (y as text) & "-" & (mo as text) & "-" & (da as text)
end formatDate

on run
    set linesOut to {}
    tell application "Notes"
        repeat with acc in every account
            tell acc
                set accName to name
                repeat with f in every folder
                    tell f
                        set folderName to name
                        repeat with n in every note
                            set end of linesOut to (my cleanField(accName)) & tab & (my cleanField(folderName)) & tab & (my cleanField(name of n)) & tab & (my formatDate(creation date of n))
                        end repeat
                    end tell
                end repeat
            end tell
        end repeat
    end tell
    set AppleScript's text item delimiters to linefeed
    set outText to linesOut as text
    set AppleScript's text item delimiters to ""
    return outText
end run
'''.strip()


def _list_notes_meta():
    """跑 AppleScript 拿回全部笔记的 (账户, 文件夹, 标题, 创建日期)。"""
    output = _osascript(_notes_meta_script()).strip()
    if not output:
        return []
    meta = []
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) < 4:
            continue  # 异常行直接跳过，不让单条脏数据中断整轮导出
        # 上游已把真实换行/TAB 转义成两字符，这里还原
        account = parts[0].replace("\\t", "\t").replace("\\n", "\n")
        folder = parts[1].replace("\\t", "\t").replace("\\n", "\n")
        title = parts[2].replace("\\t", "\t").replace("\\n", "\n")
        meta.append(
            {"account": account, "folder": folder, "title": title, "date": parts[3]}
        )
    return meta


def _body_script(account, folder, note_index):
    """生成「取某账户某文件夹第 note_index 条笔记 body」的 AppleScript。

    为什么按文件夹内序号取而不是按标题取：同文件夹下标题可能重复，
    序号与元信息枚举顺序一致，能唯一定位。脚本只输出 body，因此正文里
    无论有多少换行都不会破坏 Python 侧的解析。
    """
    return (
        'on run\n'
        '    set accName to "{}"\n'
        '    set folderName to "{}"\n'
        '    set targetIndex to {}\n'
        '    tell application "Notes"\n'
        '        repeat with acc in every account\n'
        '            if name of acc is accName then\n'
        '                tell acc\n'
        '                    repeat with f in every folder\n'
        '                        if name of f is folderName then\n'
        '                            tell f\n'
        '                                set i to 0\n'
        '                                repeat with n in every note\n'
        '                                    set i to i + 1\n'
        '                                    if i is targetIndex then\n'
        '                                        return body of n\n'
        '                                    end if\n'
        '                                end repeat\n'
        '                            end tell\n'
        '                        end if\n'
        '                    end repeat\n'
        '                end tell\n'
        '            end if\n'
        '        end repeat\n'
        '    end tell\n'
        '    return ""\n'
        'end run\n'
    ).format(_apple_quote(account), _apple_quote(folder), int(note_index))


def _fetch_body(meta_row):
    """按元信息行取正文 HTML 并转纯文本；任何一步失败抛清晰错误。"""
    script = _body_script(
        meta_row["account"],
        meta_row["folder"],
        meta_row["folder_index"],
    )
    html = _osascript(script)
    return html_to_text(html)


def _claim_output_path(output_dir, date_str, title, claimed):
    """处理同文件夹/同日期/同标题重名：追加 -2、-3……并登记本次占用。

    幂等策略：磁盘上已存在但本次还没认领的同名文件，视为上一次导出，
    直接覆盖（重跑不会越导越多）；本次已认领过才追加序号，
    保证一轮导出内不同笔记互不覆盖。
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


def export_notes(target_folder=None):
    """执行导出，返回 (导出条数, 跳过空笔记数, 输出路径列表)。

    参数：
        target_folder：只导该文件夹名；None 表示导全部账户的全部文件夹。
    """
    meta = _list_notes_meta()
    if not meta:
        print("备忘录中未找到任何笔记")
        return 0, 0, []

    if target_folder is not None:
        filtered = [row for row in meta if row["folder"] == target_folder]
        if not filtered:
            print("未找到文件夹：{}".format(target_folder))
            return 0, 0, []
        meta = filtered

    # 给每条笔记标 (账户, 文件夹) 内的顺序号，供 _fetch_body 二次定位
    position_counter = {}
    for row in meta:
        key = (row["account"], row["folder"])
        position_counter[key] = position_counter.get(key, 0) + 1
        row["folder_index"] = position_counter[key]

    claimed = set()  # 本轮已写过的输出路径，防同轮互相覆盖
    exported = 0
    skipped = 0
    paths = []
    for row in meta:
        body = _fetch_body(row)
        if not body:
            # 纯空笔记不入库；计数并跳过，保持和 import_txt.py 的空文件语义一致
            skipped += 1
            print("  ⚠️  跳过空笔记：{} / {}".format(
                row["folder"], row["title"] or row["date"]
            ))
            continue

        folder_clean = clean_component(row["folder"]) or "未分类"
        title_clean = clean_component(row["title"]) or "无标题"
        output_dir = OUTPUT_ROOT / folder_clean
        output_dir.mkdir(parents=True, exist_ok=True)  # 幂等建目录
        output_path = _claim_output_path(
            output_dir, row["date"], title_clean, claimed
        )
        # 写正文纯文本；不把标题重复写进正文（标题已体现在文件名）
        output_path.write_text(body + "\n", encoding="utf-8")
        exported += 1
        paths.append(str(output_path))
        print("  ✅ {}".format(output_path))

    return exported, skipped, paths


def main():
    """命令行入口：支持可选参数只导指定文件夹。"""
    # 不传参数导全部；传了则精确匹配文件夹名（同任务书试水方式）
    target_folder = sys.argv[1] if len(sys.argv) > 1 else None
    exported, skipped, _paths = export_notes(target_folder)
    print(
        "\n完成：共导出 {} 条，跳过空笔记 {} 条".format(exported, skipped)
    )


if __name__ == "__main__":
    main()
