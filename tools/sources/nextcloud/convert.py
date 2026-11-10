#!/usr/bin/env python3
"""docx/pptx/xlsx → md 文本转换(源私有,零 pip 依赖;概要设计 2026-10-16 §3/§14.2)。

docx 认真转(段落/标题/列表/表格);pptx/xlsx 尽力抽文本;pdf 不经本工具
(Read 原生支持,fetch 不为其产 .md)。外部 pandoc 存在时 docx 优先走 pandoc
(排版保真),否则标准库兜底(zip+XML)。输出写到 <输入>.md 同路径。

用法:python3 convert.py <file.docx|file.pptx|file.xlsx> [...]
退出码:0 全部成功 / 1 有失败(缺参数=2)。
"""
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
A_NS = "{http://schemas.openxmlformats.org/drawingml/2006/main}"


def _para_text(p):
    """docx 段落:合并 w:t,保留 w:tab/w:br。"""
    out = []
    for node in p.iter():
        if node.tag == W_NS + "t":
            out.append(node.text or "")
        elif node.tag == W_NS + "tab":
            out.append("\t")
        elif node.tag == W_NS + "br":
            out.append("\n")
    return "".join(out)


def docx_to_md(raw):
    doc = ET.fromstring(raw)
    body = doc.find(W_NS + "body")
    lines = []
    if body is None:
        return ""
    for el in body:
        if el.tag == W_NS + "p":
            style = ""
            ppr = el.find(W_NS + "pPr")
            if ppr is not None:
                s = ppr.find(W_NS + "pStyle")
                if s is not None:
                    style = s.get(W_NS + "val", "") or ""
            text = _para_text(el).strip()
            m = re.match(r"^[Hh]eading(\d)$", style)
            if m and text:
                lines.append("#" * min(int(m.group(1)), 6) + " " + text)
            elif re.match(r"^[Ll]ist", style) and text:
                lines.append("- " + text)
            elif text:
                lines.append(text)
        elif el.tag == W_NS + "tbl":
            rows = el.findall(W_NS + "tr")
            for i, tr in enumerate(rows):
                cells = ["".join(t.itertext()).strip() for t in tr.findall(W_NS + "tc")]
                lines.append("| " + " | ".join(cells) + " |")
                if i == 0:
                    lines.append("|" + "---|" * len(cells))
    return "\n".join(lines).strip() + "\n"


def pptx_to_md(raw):
    """逐段落抽 a:t 文本。"""
    texts = re.findall(rb"<a:p>(.*?)</a:p>", raw, re.S)
    lines = []
    for blob in texts:
        parts = re.findall(rb"<a:t>(.*?)</a:t>", blob, re.S)
        line = "".join(p.decode("utf-8", "replace") for p in parts).strip()
        if line:
            lines.append(line)
    return "\n".join(lines).strip() + "\n"


def xlsx_to_md(raw):
    """sharedStrings + sheet 顺序号:每个字符串一行(尽力而为的文本面)。"""
    strings = re.findall(rb"<t[^>]*>(.*?)</t>", raw, re.S)
    lines = [re.sub(rb"<[^>]+>", b"", s).decode("utf-8", "replace").strip()
             for s in strings]
    lines = [l for l in lines if l]
    return "\n".join(lines).strip() + "\n"


def convert(path):
    """转换单个文件 → 同路径 .md。返回 True/False。"""
    src = Path(path)
    if src.suffix.lower() not in (".docx", ".pptx", ".xlsx"):
        print(f"[convert] 跳过(类型不转): {src}", file=sys.stderr)
        return False
    dest = src.with_suffix(src.suffix + ".md")

    if src.suffix.lower() == ".docx" and shutil.which("pandoc"):
        r = subprocess.run(["pandoc", "--to=gfm", "--", str(src)],
                           capture_output=True, text=True, timeout=120)
        if r.returncode == 0:
            dest.write_text(r.stdout, encoding="utf-8")
            print(f"[convert] pandoc: {dest.name}")
            return True
        print(f"[convert] pandoc 失败,回退标准库: {r.stderr[:120]}", file=sys.stderr)

    try:
        with zipfile.ZipFile(src) as z:
            if src.suffix.lower() == ".docx":
                raw = z.read("word/document.xml")
                md = docx_to_md(raw)
            elif src.suffix.lower() == ".pptx":
                parts = [n for n in z.namelist()
                         if re.match(r"ppt/slides/slide\d+\.xml$", n)]
                md = "\n\n".join(
                    f"## {n.split('/')[-1]}\n" + pptx_to_md(z.read(n))
                    for n in sorted(parts, key=lambda x: int(re.search(r"(\d+)", x).group(1))))
            else:  # xlsx
                md = xlsx_to_md(z.read("xl/sharedStrings.xml"))
        dest.write_text(md, encoding="utf-8")
        print(f"[convert] {dest.name}({len(md)} 字符)")
        return True
    except (KeyError, zipfile.BadZipFile, ET.ParseError, OSError) as e:
        print(f"[convert] 失败 {src}: {e}", file=sys.stderr)
        return False


def main():
    if len(sys.argv) < 2:
        print("用法: python3 convert.py <file.docx|.pptx|.xlsx> [...]", file=sys.stderr)
        return 2
    rc = 0
    for p in sys.argv[1:]:
        if not convert(p):
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
