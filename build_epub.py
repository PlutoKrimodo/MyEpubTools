# -*- coding: utf-8 -*-
"""Convert a plain-text book into an EPUB, reusing the CSS from a template EPUB.

Usage:
    python build_epub.py --input book.txt --template test.epub \
        --title "书名" --author "作者" --output book.epub

Only --input and --template are required. Title defaults to the txt file's
stem; author defaults to "未知"; output defaults to "outputs/<title>.epub"
next to this script.

Chapter detection:
    Any line matching one of these patterns is treated as a new chapter:
      * 第X章 / 第X回 / 第X节 / 第X部 / 第X卷  (X = Chinese or Arabic digits)
      * 引言 / 前言 / 序言 / 序 / 楔子 / 后记 / 结语 / 尾声 / 附录 / 译后记
      * Chapter N / CHAPTER N   (English)
      * Arabic-numeric-only line (e.g. "1", "12") — treated as a chapter marker
    Provide --chapter-regex to add or replace with a custom pattern.

The first line matching 《书名》 or 作者：xxx is treated as metadata and
skipped from the body.
"""
import argparse
import re
import shutil
import uuid
import zipfile
from pathlib import Path


DEFAULT_CHAPTER_PATTERNS = [
    r"^第[零〇一二三四五六七八九十百千万两0-9]+[章回节部卷篇](?:\s*.*)?$",
    r"^(?:引言|前言|序言|序|楔子|后记|结语|尾声|附录|译后记|导言|导读|译序|译者序|作者序|自序|绪论|绪言)(?:\s*.*)?$",
    r"^(?:Chapter|CHAPTER|Prologue|Epilogue|Preface|Introduction|Foreword)\s+[0-9IVXLCDM]+.*$",
    r"^[0-9]{1,4}$",
]


class EpubBuildError(Exception):
    pass


META_TITLE_RE = re.compile(r"^《.+》$")
META_AUTHOR_RE = re.compile(r"^(?:作者|著者|作\s*者|译\s*者|译者)\s*[:：]\s*.*$")


def build_chapter_re(extra_pattern=None, mode="append"):
    if mode not in {"append", "custom"}:
        raise EpubBuildError(f"Invalid chapter mode: {mode}")

    parts = []
    if mode == "append":
        parts.extend(DEFAULT_CHAPTER_PATTERNS)
        if extra_pattern:
            parts.insert(0, extra_pattern)
    elif extra_pattern:
        parts.append(extra_pattern)

    if not parts:
        return re.compile(r"a^")
    return re.compile("|".join(f"(?:{p})" for p in parts))


# --------------------------------------------------------------------------- #
# 从示例文本生成自定义章节正则
# --------------------------------------------------------------------------- #
#: 生成时最多使用的示例行数（去空后）。
MAX_GENERATE_SAMPLES = 50

_ARABIC_DIGITS = "0-9０-９"
_CHINESE_NUMERALS = "零〇一二三四五六七八九十百千万亿两"
_NUMERIC_CLASS = f"[{_ARABIC_DIGITS}{_CHINESE_NUMERALS}]"

#: 按「具体 → 宽泛」排序的候选模板；**全部**示例都能 match 才采用。
#: 首项直接复用内置规则，保证与默认章节识别行为一致。
_GENERATE_CANDIDATES = [
    DEFAULT_CHAPTER_PATTERNS[0],
    DEFAULT_CHAPTER_PATTERNS[1],
    DEFAULT_CHAPTER_PATTERNS[2],
    DEFAULT_CHAPTER_PATTERNS[3],
    (
        r"^(?:Chapter|CHAPTER|Section|SECTION|Part|PART|Episode|EPISODE)"
        r"\s+[0-9IVXLCDM]+(?:\s*.*)?$"
    ),
    (
        rf"^第{_NUMERIC_CLASS}+[章回节部卷篇话折序][：:、]?\s*.*$"
    ),
    rf"^[{_ARABIC_DIGITS}]{{1,4}}[.、．)]\s*.*$",
    r"^Vol(?:ume)?\.?\s+[0-9]+(?:\s*.*)?$",
]

_ARABIC_RUN_RE = re.compile(rf"[{_ARABIC_DIGITS}]+")
_CN_NUM_RUN_RE = re.compile(rf"[{_CHINESE_NUMERALS}]+")
_LETTER_RUN_RE = re.compile(r"[A-Za-z]+")
_WS_RUN_RE = re.compile(r"\s+")
_CHINESE_NUMERAL_CHARS = frozenset(_CHINESE_NUMERALS)


def _is_cjk_char(ch):
    code = ord(ch)
    return (
        0x3400 <= code <= 0x4DBF
        or 0x4E00 <= code <= 0x9FFF
        or 0xF900 <= code <= 0xFAFF
    )


def _match_cjk_run(text, start):
    """匹配汉字连串；遇到中文数字则停，避免「卷之一 / 第一卷」被吞成一整段。"""
    index = start
    length = len(text)
    while index < length:
        ch = text[index]
        if ch in _CHINESE_NUMERAL_CHARS:
            break
        if not _is_cjk_char(ch):
            break
        index += 1
    return index - start


def _normalize_sample_lines(raw):
    """拆成去重、保序的非空示例行；全空则报中文错误。"""
    lines = []
    seen = set()
    for line in str(raw or "").splitlines():
        text = line.strip()
        if not text or text in seen:
            continue
        seen.add(text)
        lines.append(text)
        if len(lines) >= MAX_GENERATE_SAMPLES:
            break
    if not lines:
        raise EpubBuildError("请至少提供一行章节标题示例")
    return lines


def _all_match(pattern, samples):
    try:
        compiled = re.compile(pattern)
    except re.error:
        return False
    return all(compiled.match(sample) for sample in samples)


def _tokenize_chapter_line(text):
    """把一行拆成 ``(kind, value)``。

    kind：``NUM`` / ``WORD`` / ``CJK`` / ``SPACE`` / ``LIT``。
    中文数字与阿拉伯数字都归为 ``NUM``，便于结构对齐时合并。
    """
    tokens = []
    index = 0
    length = len(text)
    while index < length:
        rest = text[index:]

        match = _ARABIC_RUN_RE.match(rest)
        if match:
            tokens.append(("NUM", match.group()))
            index += match.end()
            continue

        match = _CN_NUM_RUN_RE.match(rest)
        if match:
            tokens.append(("NUM", match.group()))
            index += match.end()
            continue

        match = _LETTER_RUN_RE.match(rest)
        if match:
            tokens.append(("WORD", match.group()))
            index += match.end()
            continue

        cjk_len = _match_cjk_run(text, index)
        if cjk_len:
            tokens.append(("CJK", text[index : index + cjk_len]))
            index += cjk_len
            continue

        match = _WS_RUN_RE.match(rest)
        if match:
            tokens.append(("SPACE", " "))
            index += match.end()
            continue

        tokens.append(("LIT", text[index]))
        index += 1
    return tokens


def _structural_tokens(tokens):
    """去掉空白后的结构序列；空白是否出现单独记录，便于生成 ``\\s*``。"""
    structural = []
    space_after = []
    for kind, value in tokens:
        if kind == "SPACE":
            if structural:
                space_after[-1] = True
            continue
        structural.append((kind, value))
        space_after.append(False)
    return structural, space_after


def _merge_numeric_token(values):
    """数字槽：始终允许阿拉伯数字与中文数字，单行示例也能泛化到其它章号。"""
    return rf"(?:[{_ARABIC_DIGITS}]+|[{_CHINESE_NUMERALS}]+)"


def _common_prefix_length(values):
    if not values:
        return 0
    limit = min(len(value) for value in values)
    index = 0
    while index < limit and len({value[index] for value in values}) == 1:
        index += 1
    return index


def _merge_variable_text(values, is_last):
    """同一槽位文本不一致：末位用 ``.+`` 收标题；中间保留公共前缀再放宽。"""
    if is_last:
        return r".+"
    prefix_len = _common_prefix_length(values)
    if prefix_len:
        return re.escape(values[0][:prefix_len]) + r".+"
    return r".+"


def _merge_samples(samples):
    """结构对齐合并多行示例，失败抛 ``EpubBuildError``。"""
    token_lists = [_tokenize_chapter_line(sample) for sample in samples]
    structure_keys = []
    all_structural = []
    all_space_after = []
    for tokens in token_lists:
        structural, space_after = _structural_tokens(tokens)
        all_structural.append(structural)
        all_space_after.append(space_after)
        structure_keys.append(tuple(kind for kind, _ in structural))

    if not structure_keys or len(set(structure_keys)) != 1:
        raise EpubBuildError(
            "示例格式不一致：请粘贴风格相同的章节标题"
            "（例如都是「第X章 标题」或都是「Chapter N …」）"
        )

    count = len(all_structural[0])
    parts = ["^"]
    for position in range(count):
        kinds = [struct[position][0] for struct in all_structural]
        values = [struct[position][1] for struct in all_structural]
        kind = kinds[0]
        is_last = position == count - 1

        if kind == "NUM":
            parts.append(_merge_numeric_token(values))
        elif kind in ("WORD", "CJK"):
            unique = list(dict.fromkeys(values))
            if len(unique) == 1 and not is_last:
                parts.append(re.escape(unique[0]))
            elif len(unique) == 1 and is_last:
                # 单行末尾汉字/单词：仍收成可变标题，避免只能匹配这一行
                parts.append(_merge_variable_text(values, True))
            else:
                parts.append(_merge_variable_text(values, is_last))
        elif kind == "LIT":
            unique = list(dict.fromkeys(values))
            if len(unique) == 1:
                parts.append(re.escape(unique[0]))
            else:
                class_body = "".join(dict.fromkeys(re.escape(ch) for ch in "".join(unique)))
                parts.append(f"[{class_body}]")
        else:
            parts.append(re.escape(values[0]))

        # 任一样例在本槽位后有空白 → 生成可选空白，兼容「第1章 题」与「第1章题」
        if position < count - 1 and any(
            space_after[position] for space_after in all_space_after
            if position < len(space_after)
        ):
            parts.append(r"\s*")

    parts.append("$")
    return "".join(parts)


def generate_chapter_regex(samples_text):
    """从多行章节标题示例生成自定义章节正则字符串。

    优先匹配内置候选模板（全部示例命中才采用）；否则按结构合并泛化。
    生成结果保证能 ``match`` 每一行去空后的示例。
    """
    samples = _normalize_sample_lines(samples_text)

    for candidate in _GENERATE_CANDIDATES:
        if _all_match(candidate, samples):
            return candidate

    pattern = _merge_samples(samples)
    if not _all_match(pattern, samples):
        # 合并结果异常时给出可读错误，而不是把坏正则塞进输入框
        raise EpubBuildError("无法从示例生成可用的章节正则，请调整示例后重试")
    try:
        re.compile(pattern)
    except re.error as exc:
        raise EpubBuildError(f"生成的正则无效：{exc}") from exc
    return pattern


def parse_chapters(text, chapter_re):
    """Split text into sections. Returns a list of dicts: {title, paragraphs}."""
    sections = []
    current = None
    seen_body = False

    def push():
        nonlocal current
        if current is not None:
            sections.append(current)
            current = None

    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped:
            continue

        if not seen_body and current is None:
            if META_TITLE_RE.match(stripped) or META_AUTHOR_RE.match(stripped):
                continue

        if chapter_re.match(stripped):
            push()
            current = {"title": stripped, "paragraphs": []}
            seen_body = True
            continue

        para = stripped.lstrip("　 \t")
        if not para:
            continue
        if current is None:
            current = {"title": "前言", "paragraphs": []}
        current["paragraphs"].append(para)
        seen_body = True

    push()
    return sections


def analyze_txt(src_txt, chapter_regex=None, chapter_mode="append", encoding="utf-8"):
    src_txt = Path(src_txt)
    if not src_txt.exists():
        raise EpubBuildError(f"Input txt not found: {src_txt}")

    text = src_txt.read_text(encoding=encoding)
    chapter_re = build_chapter_re(chapter_regex, chapter_mode)
    sections = parse_chapters(text, chapter_re)
    warnings = []
    if not sections:
        warnings.append("No content detected.")

    return {
        "section_count": len(sections),
        "sections": [
            {
                "index": idx,
                "title": section["title"],
                "paragraph_count": len(section["paragraphs"]),
            }
            for idx, section in enumerate(sections, start=1)
        ],
        "warnings": warnings,
    }


def escape_html(s):
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


CHAPTER_HTML_TEMPLATE = """<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Strict//EN" "http://www.w3.org/TR/xhtml1/DTD/xhtml1-strict.dtd"><html xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="{lang}" xmlns="http://www.w3.org/1999/xhtml" lang="{lang}">
<head>
<title>{title}</title>
{css_links}
</head>
<body>
<h1 id="{anchor}" class="chaptertitle-c"><b>{title_html}</b></h1>
{body}
</body>
</html>
"""

COVER_HTML_TEMPLATE = """<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Strict//EN" "http://www.w3.org/TR/xhtml1/DTD/xhtml1-strict.dtd"><html xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="{lang}" xmlns="http://www.w3.org/1999/xhtml" lang="{lang}">
<head>
<title>{title}</title>
{css_links}
</head>
<body>
<h1 class="parttitle-c" style="font-size:2.4em; margin-top:4em;"><b>{title}</b></h1>
{subtitle_block}
<p class="noindent-bodyContent-1-c-top" style="margin-top:3em; font-size:1.3em;">{author}</p>
</body>
</html>
"""

TOC_HTML_TEMPLATE = """<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Strict//EN" "http://www.w3.org/TR/xhtml1/DTD/xhtml1-strict.dtd"><html xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="{lang}" xmlns="http://www.w3.org/1999/xhtml" lang="{lang}">
<head>
<title>{title}</title>
{css_links}
</head>
<body>
<h1 class="parttitle-c"><b>目录</b></h1>
<ol class="toc-list">
{items}
</ol>
</body>
</html>
"""

CUSTOM_CSS = (
    "h1.chaptertitle-c, h1.chaptertitle, h1.chaptertitle-r,\n"
    "h1.parttitle, h1.parttitle-c, h1.parttitle-r,\n"
    "h1.prefacetitle, h1.prefacetitle-c, h1.prefacetitle-r {\n"
    "    font-weight: bold;\n"
    "}\n"
    "h1.chaptertitle-c b, h1.parttitle-c b { font-weight: bold; }\n"
    ".toc-list { margin: 1.5em 0 0 1.25em; }\n"
    ".toc-list li { margin: 0.4em 0; }\n"
)


def extract_template_css(template_epub, dest_dir):
    """Extract every .css file from the template epub into dest_dir.

    Returns a list of extracted css filenames (basename only, no folders).
    """
    css_names = []
    with zipfile.ZipFile(template_epub, "r") as zf:
        for name in zf.namelist():
            if name.lower().endswith(".css"):
                data = zf.read(name)
                base = Path(name).name
                out_path = dest_dir / base
                if out_path.exists():
                    base = Path(name).parent.name + "_" + base
                    out_path = dest_dir / base
                out_path.write_bytes(data)
                css_names.append(base)
    return css_names


def make_css_links(css_names):
    return "\n".join(
        f'<link href="{name}" rel="stylesheet" type="text/css" />' for name in css_names
    )


def make_chapter_html(section, idx, lang, css_links):
    title = section["title"]
    anchor = f"ch{idx:04d}"
    body = "\n".join(
        f'<p class="bodyContent-1">{escape_html(p)}</p>'
        for p in section["paragraphs"]
    )
    return CHAPTER_HTML_TEMPLATE.format(
        lang=lang,
        title=escape_html(title),
        title_html=escape_html(title),
        anchor=anchor,
        body=body,
        css_links=css_links,
    )


def make_cover_html(title, author, subtitle, lang, css_links):
    subtitle_block = ""
    if subtitle:
        subtitle_block = f'<p class="prefacetitle-c" style="margin-top:1.5em; font-size:1.1em;">{escape_html(subtitle)}</p>'
    return COVER_HTML_TEMPLATE.format(
        lang=lang,
        title=escape_html(title),
        author=escape_html(author),
        subtitle_block=subtitle_block,
        css_links=css_links,
    )


def make_toc_html(sections, title, lang, css_links):
    items = []
    for idx, section in enumerate(sections, start=1):
        items.append(
            f'<li><a href="text{idx:05d}.html#ch{idx:04d}">{escape_html(section["title"])}</a></li>'
        )
    return TOC_HTML_TEMPLATE.format(
        lang=lang,
        title=escape_html(title),
        css_links=css_links,
        items="\n".join(items),
    )


def build_epub(
    src_txt,
    template_epub,
    out_epub,
    title,
    author,
    lang="zh-Hans",
    build_dir=None,
    keep_build=False,
    chapter_regex=None,
    chapter_mode="append",
    encoding="utf-8",
    toc_page=False,
    subtitle="",
    log_func=print,
):
    emit = log_func or (lambda *_args, **_kwargs: None)

    src_txt = Path(src_txt)
    template_epub = Path(template_epub)
    out_epub = Path(out_epub)
    out_epub.parent.mkdir(parents=True, exist_ok=True)
    build_dir = Path(build_dir) if build_dir else src_txt.parent / f".build_{src_txt.stem}"

    if not src_txt.exists():
        raise EpubBuildError(f"Input txt not found: {src_txt}")
    if not template_epub.exists():
        raise EpubBuildError(f"Template epub not found: {template_epub}")

    text = src_txt.read_text(encoding=encoding)
    chapter_re = build_chapter_re(chapter_regex, chapter_mode)
    sections = parse_chapters(text, chapter_re)
    if not sections:
        raise EpubBuildError("No chapters detected. Check your chapter markers or pass --chapter-regex.")

    emit(f"Parsed {len(sections)} sections from {src_txt.name}")
    for i, section in enumerate(sections):
        if i < 3 or i >= len(sections) - 2:
            emit(f"  {i + 1:>3}. {section['title']}  ({len(section['paragraphs'])} paragraphs)")
        elif i == 3 and len(sections) > 5:
            emit("  ...")

    if build_dir.exists():
        shutil.rmtree(build_dir)
    build_dir.mkdir(parents=True)
    oebps = build_dir / "OEBPS"
    oebps.mkdir()
    meta_inf = build_dir / "META-INF"
    meta_inf.mkdir()

    (build_dir / "mimetype").write_text("application/epub+zip", encoding="ascii")
    (meta_inf / "container.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
        "   <rootfiles>\n"
        '      <rootfile full-path="content.opf" media-type="application/oebps-package+xml"/>\n'
        "   </rootfiles>\n"
        "</container>\n",
        encoding="utf-8",
    )

    css_names = extract_template_css(template_epub, oebps)
    emit(f"Copied {len(css_names)} CSS file(s) from {template_epub.name}: {', '.join(css_names) or '(none)'}")
    (oebps / "custom.css").write_text(CUSTOM_CSS, encoding="utf-8")
    css_names.append("custom.css")
    css_links = make_css_links(css_names)

    (oebps / "cover.html").write_text(
        make_cover_html(title=title, author=author, subtitle=subtitle, lang=lang, css_links=css_links),
        encoding="utf-8",
    )

    if toc_page:
        (oebps / "toc.html").write_text(
            make_toc_html(sections=sections, title=title, lang=lang, css_links=css_links),
            encoding="utf-8",
        )

    html_entries = []
    for idx, section in enumerate(sections, start=1):
        fname = f"text{idx:05d}.html"
        (oebps / fname).write_text(
            make_chapter_html(section, idx, lang, css_links),
            encoding="utf-8",
        )
        html_entries.append((f"a{idx}", f"OEBPS/{fname}", section["title"], f"ch{idx:04d}"))

    uid = f"urn:uuid:{uuid.uuid4()}"
    manifest_items = ['<item id="cover" href="OEBPS/cover.html" media-type="application/xhtml+xml"/>']
    spine_items = ['<itemref idref="cover" linear="yes"/>']
    nav_points = [
        f'      <navPoint id="nav1" playOrder="1">\n'
        f'         <navLabel><text>封面</text></navLabel>\n'
        f'         <content src="OEBPS/cover.html"/>\n'
        f"      </navPoint>"
    ]

    if toc_page:
        manifest_items.append('<item id="tocpage" href="OEBPS/toc.html" media-type="application/xhtml+xml"/>')
        spine_items.append('<itemref idref="tocpage" linear="yes"/>')
        nav_points.append(
            '      <navPoint id="nav2" playOrder="2">\n'
            '         <navLabel><text>目录</text></navLabel>\n'
            '         <content src="OEBPS/toc.html"/>\n'
            '      </navPoint>'
        )

    for i, (iid, href, sec_title, anchor) in enumerate(html_entries, start=len(nav_points) + 1):
        manifest_items.append(f'<item id="{iid}" href="{href}" media-type="application/xhtml+xml"/>')
        spine_items.append(f'<itemref idref="{iid}" linear="yes"/>')
        nav_points.append(
            f'      <navPoint id="nav{i}" playOrder="{i}">\n'
            f'         <navLabel><text>{escape_html(sec_title)}</text></navLabel>\n'
            f'         <content src="{href}#{anchor}"/>\n'
            f"      </navPoint>"
        )

    manifest_items.append('<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>')
    for i, name in enumerate(css_names):
        manifest_items.append(f'<item id="css{i}" href="OEBPS/{name}" media-type="text/css"/>')

    manifest_xml = "\n    ".join(manifest_items)
    spine_xml = "\n    ".join(spine_items)
    opf = f"""<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="bookid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:opf="http://www.idpf.org/2007/opf">
    <dc:title>{escape_html(title)}</dc:title>
    <dc:creator opf:role="aut">{escape_html(author)}</dc:creator>
    <dc:identifier id="bookid">{uid}</dc:identifier>
    <dc:language>{escape_html(lang)}</dc:language>
  </metadata>
  <manifest>
    {manifest_xml}
  </manifest>
  <spine toc="ncx">
    {spine_xml}
  </spine>
  <guide>
    <reference type="cover" title="Cover" href="OEBPS/cover.html"/>
  </guide>
</package>
"""
    (build_dir / "content.opf").write_text(opf, encoding="utf-8")

    ncx = f"""<?xml version="1.0" encoding="utf-8"?>
<ncx version="2005-1" xmlns="http://www.daisy.org/z3986/2005/ncx/">
   <head>
      <meta name="dtb:uid" content="{uid}"/>
      <meta name="dtb:depth" content="1"/>
      <meta name="dtb:totalPageCount" content="0"/>
      <meta name="dtb:maxPageNumber" content="0"/>
   </head>
   <docTitle>
      <text>{escape_html(title)}</text>
   </docTitle>
   <navMap>
{chr(10).join(nav_points)}
   </navMap>
</ncx>
"""
    (build_dir / "toc.ncx").write_text(ncx, encoding="utf-8")

    if out_epub.exists():
        out_epub.unlink()
    with zipfile.ZipFile(out_epub, "w") as zf:
        zf.write(build_dir / "mimetype", "mimetype", compress_type=zipfile.ZIP_STORED)
        for path in sorted(build_dir.rglob("*")):
            if path.is_dir():
                continue
            rel = path.relative_to(build_dir).as_posix()
            if rel == "mimetype":
                continue
            zf.write(path, rel, compress_type=zipfile.ZIP_DEFLATED)

    if not keep_build:
        shutil.rmtree(build_dir)

    emit(f"Generated: {out_epub}  ({out_epub.stat().st_size} bytes)")
    return out_epub


def main():
    p = argparse.ArgumentParser(description="Convert a txt book into an EPUB using a template epub's CSS.")
    p.add_argument("--input", "-i", required=True, help="Source .txt file")
    p.add_argument("--template", "-t", required=True, help="Template .epub file whose CSS will be reused")
    p.add_argument("--output", "-o", help="Output .epub file (default: outputs/<title>.epub)")
    p.add_argument("--title", help="Book title (default: source filename stem)")
    p.add_argument("--author", default="未知", help='Book author (default: "未知")')
    p.add_argument("--lang", default="zh-Hans", help="Language code (default: zh-Hans)")
    p.add_argument("--chapter-regex", help="Custom regex for chapter markers")
    p.add_argument("--chapter-mode", choices=["append", "custom"], default="append", help="How to combine the custom chapter regex with built-in rules")
    p.add_argument("--encoding", default="utf-8", help="Input txt encoding (default: utf-8)")
    p.add_argument("--preview", action="store_true", help="Preview chapter detection without building EPUB")
    p.add_argument("--toc-page", action="store_true", help="Generate an in-book TOC page")
    p.add_argument("--subtitle", default="", help="Subtitle shown on the cover page")
    p.add_argument("--keep-build", action="store_true", help="Keep the build directory for inspection")
    args = p.parse_args()

    src = Path(args.input)
    title = args.title or src.stem
    output = Path(args.output) if args.output else Path(__file__).resolve().parent / "outputs" / f"{title}.epub"

    try:
        if args.preview:
            result = analyze_txt(
                src_txt=src,
                chapter_regex=args.chapter_regex,
                chapter_mode=args.chapter_mode,
                encoding=args.encoding,
            )
            print(f"Parsed {result['section_count']} sections from {src.name}")
            for section in result["sections"][:10]:
                print(f"  {section['index']:>3}. {section['title']}  ({section['paragraph_count']} paragraphs)")
            if len(result["sections"]) > 10:
                print("  ...")
            for warning in result["warnings"]:
                print(f"Warning: {warning}")
            return

        build_epub(
            src_txt=src,
            template_epub=args.template,
            out_epub=output,
            title=title,
            author=args.author,
            lang=args.lang,
            keep_build=args.keep_build,
            chapter_regex=args.chapter_regex,
            chapter_mode=args.chapter_mode,
            encoding=args.encoding,
            toc_page=args.toc_page,
            subtitle=args.subtitle,
        )
    except EpubBuildError as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
