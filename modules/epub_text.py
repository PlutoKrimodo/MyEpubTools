# -*- coding: utf-8 -*-
"""正文文本清洗公共层。

只处理 **spine 正文文档**（由 :func:`modules.epub_package.spine_documents` 定位），
封面 / 目录 / 导航文档一律不动。支持四类操作：

1. 空白与空段落规范——连续空白按「中文紧贴、西文留一格」合并，删除空 ``<p>``
2. 合并被硬换行拆断的段落——相邻 ``<p>`` 在边界不像句末时的合并
3. 清理冗余内联标签——去 ``<font>``、去内联 ``style``、拆无意义 ``<span>``、删空内联标签
4. 自定义查找替换——默认作用于**文本节点**，不会破坏标签结构

本模块**没有** ``TOOL`` 字典，属于公共模块，``registry.discover()`` 会自动跳过。

关于保真度（重要）
------------------
``BeautifulSoup`` 往返 XHTML 是**有损**的：实测即使在原文上不做任何修改，
输出也会有 CRLF→LF、DOCTYPE 后多一个换行、属性重排、`` />``→``/>`` 等差异。

因此本模块的策略是：

* **没有实际改动的文档一律不写回**，保持逐字节不变；
* 只有真正改过的文档才重写，并尽量还原原文的行尾风格与自闭合标签写法。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from xml.sax.saxutils import escape

from bs4 import BeautifulSoup, Comment, NavigableString

from modules.epub_io import EpubIOError, decode_text
from modules.epub_package import spine_documents

#: 块级标签：用于判断文本节点两侧的空白该留还是该丢。
BLOCK_TAGS = frozenset({
    "p", "div", "section", "article", "aside", "blockquote", "li", "ul", "ol",
    "dl", "dt", "dd", "h1", "h2", "h3", "h4", "h5", "h6", "figure", "figcaption",
    "table", "thead", "tbody", "tr", "td", "th", "hr", "br", "body", "html",
    "nav", "header", "footer", "main", "form", "pre",
})

#: 结构性块级标签：出现在段落内部时说明这段不是纯文本，不参与断行合并。
_HEAVY_BLOCKS = frozenset({
    "p", "div", "section", "article", "aside", "blockquote", "table",
    "ul", "ol", "dl", "figure", "pre",
})

#: 这些上下文里的文本一律不碰。
SKIP_CONTEXT_TAGS = frozenset({"head", "script", "style", "pre", "code", "svg", "math"})

#: 可清理的内联标签。
INLINE_TAGS = frozenset({"span", "font", "b", "em", "i", "strong", "u", "s", "small", "sub", "sup"})

#: 无内容即可删除的内联标签。
EMPTY_INLINE_TAGS = frozenset({"span", "font", "b", "em", "i", "strong", "u", "s", "small"})

#: 段落被视为「已结束」的句末字符。
_SENTENCE_ENDERS = "。！？!?…."

#: 句末字符之后可能紧跟的收尾符号。
_CLOSERS = "」』”’\"'）)】》〉"

_WS_RE = re.compile(r"\s+")
_TRAILING_WS_RE = re.compile(r"\s+$")
_XML_DECL_RE = re.compile(r"<\?xml\b[^>]*\?>")
_CJK_RE = re.compile(
    r"[\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff01-\uff60\uffe0-\uffe6]"
)

PREVIEW_DOCUMENT_LIMIT = 3
PREVIEW_CHAR_LIMIT = 2600

_EMPTY_SPINE_MESSAGE = "按 OPF 的 spine 未找到正文文档，请确认这是结构完整的 EPUB"


class EpubTextError(EpubIOError):
    """正文清洗失败。继承 ``EpubIOError``，便于工具统一兜底。"""


@dataclass(frozen=True)
class CleanOptions:
    """一次清洗的开关组合。"""

    normalize_whitespace: bool = True
    remove_empty_paragraphs: bool = True
    merge_broken_paragraphs: bool = True
    strip_inline: bool = True
    replacements: tuple = ()
    regex_replacements: bool = False


@dataclass
class CleanStats:
    """清洗统计。"""

    documents: int = 0
    changed_documents: int = 0
    whitespace_fixed: int = 0
    empty_paragraphs_removed: int = 0
    paragraphs_merged: int = 0
    inline_styles_removed: int = 0
    inline_tags_unwrapped: int = 0
    empty_inline_removed: int = 0
    replacements_applied: int = 0
    skipped_rules: int = 0
    changed_files: list = field(default_factory=list)

    def add(self, counters):
        for key, value in counters.items():
            setattr(self, key, getattr(self, key) + value)

    def as_dict(self):
        return {
            "documents": self.documents,
            "changed_documents": self.changed_documents,
            "whitespace_fixed": self.whitespace_fixed,
            "empty_paragraphs_removed": self.empty_paragraphs_removed,
            "paragraphs_merged": self.paragraphs_merged,
            "inline_styles_removed": self.inline_styles_removed,
            "inline_tags_unwrapped": self.inline_tags_unwrapped,
            "empty_inline_removed": self.empty_inline_removed,
            "replacements_applied": self.replacements_applied,
            "skipped_rules": self.skipped_rules,
            "changed_files": list(self.changed_files),
        }


def _empty_counters():
    return {
        "whitespace_fixed": 0,
        "empty_paragraphs_removed": 0,
        "paragraphs_merged": 0,
        "inline_styles_removed": 0,
        "inline_tags_unwrapped": 0,
        "empty_inline_removed": 0,
        "replacements_applied": 0,
        "skipped_rules": 0,
    }


# --------------------------------------------------------------------------- #
# 基础判断
# --------------------------------------------------------------------------- #
def _is_cjk(char):
    return bool(char) and bool(_CJK_RE.match(char))


def _is_gone(node):
    return node is None or getattr(node, "parent", None) is None


def _is_blockish(node):
    if node is None or isinstance(node, Comment):
        return True
    if isinstance(node, NavigableString):
        return False
    return getattr(node, "name", None) in BLOCK_TAGS


def _edge_char(node, last):
    """取相邻兄弟节点的边缘字符，用于判断中英文之间要不要留空格。"""
    if node is None or isinstance(node, Comment):
        return ""
    text = str(node) if isinstance(node, NavigableString) else node.get_text()
    text = text.replace("\xa0", " ").strip()
    if not text:
        return ""
    return text[-1] if last else text[0]


def _plain_text(tag):
    return tag.get_text().replace("\xa0", " ").strip()


def _in_skipped_context(node):
    parent = getattr(node, "parent", None)
    while parent is not None:
        if getattr(parent, "name", None) in SKIP_CONTEXT_TAGS:
            return True
        parent = getattr(parent, "parent", None)
    return False


# --------------------------------------------------------------------------- #
# 1. 空白与空段落规范
# --------------------------------------------------------------------------- #
def _replace_text(node, replacement, counters):
    if replacement == str(node):
        return
    if replacement == "":
        node.extract()
    else:
        node.replace_with(NavigableString(replacement))


def _collapse_inner(text, counters):
    def replace(match):
        before = match.string[match.start() - 1] if match.start() > 0 else ""
        after = match.string[match.end()] if match.end() < len(match.string) else ""
        result = " "
        if "\n" in match.group(0) and before and after and _is_cjk(before) and _is_cjk(after):
            # 换行是硬排版留下的断行痕迹，中文之间应当紧贴；
            # 而作者有意打的空格（如「第一章 开始」）要保留，所以只对含换行的空白这么做。
            result = ""
        if result != match.group(0):
            counters["whitespace_fixed"] += 1
        return result

    return _WS_RE.sub(replace, text)


def _normalize_text_node(node, counters):
    text = str(node)
    if not text:
        return

    lead = _WS_RE.match(text)
    tail = _TRAILING_WS_RE.search(text)
    start = lead.end() if lead else 0
    end = tail.start() if tail else len(text)
    core = text[start:end]

    if not core:
        prev_block = _is_blockish(node.previous_sibling)
        next_block = _is_blockish(node.next_sibling)
        if prev_block and next_block:
            # 块与块之间的换行缩进是排版格式，HTML 本就不渲染它。
            # 删掉没有功能收益，却会让「本来干净的文档」被改写，因此保持原样。
            return
        if prev_block or next_block:
            _replace_text(node, "", counters)
        else:
            _replace_text(node, " ", counters)
        return

    pieces = []
    if lead:
        before = _edge_char(node.previous_sibling, last=True)
        if _is_blockish(node.previous_sibling):
            lead_text = ""
        elif "\n" in lead.group(0) and _is_cjk(before) and _is_cjk(core[0]):
            lead_text = ""
        else:
            lead_text = " "
        if lead_text != lead.group(0):
            counters["whitespace_fixed"] += 1
        pieces.append(lead_text)

    pieces.append(_collapse_inner(core, counters))

    if tail:
        after = _edge_char(node.next_sibling, last=False)
        if _is_blockish(node.next_sibling):
            tail_text = ""
        elif "\n" in tail.group(0) and _is_cjk(after) and _is_cjk(core[-1]):
            tail_text = ""
        else:
            tail_text = " "
        if tail_text != tail.group(0):
            counters["whitespace_fixed"] += 1
        pieces.append(tail_text)

    _replace_text(node, "".join(pieces), counters)


def _normalize_whitespace(soup, counters):
    for node in list(soup.find_all(string=True)):
        if isinstance(node, Comment) or _is_gone(node) or _in_skipped_context(node):
            continue
        _normalize_text_node(node, counters)


def _remove_empty_paragraphs(soup, counters):
    for tag in list(soup.find_all("p")):
        if _is_gone(tag):
            continue
        if tag.find(["img", "image", "svg", "table", "audio", "video", "object"]):
            continue
        if _plain_text(tag):
            continue
        tag.decompose()
        counters["empty_paragraphs_removed"] += 1


# --------------------------------------------------------------------------- #
# 2. 合并被硬换行拆断的段落
# --------------------------------------------------------------------------- #
def _ends_sentence(text):
    text = text.rstrip()
    while text and text[-1] in _CLOSERS:
        text = text[:-1]
    return bool(text) and text[-1] in _SENTENCE_ENDERS


def _looks_like_title(tag):
    classes = " ".join(tag.get("class") or []).lower()
    if "title" in classes:
        return True
    return "title" in str(tag.get("epub:type", "")).lower()


def _has_note_type(tag):
    values = set(str(tag.get("epub:type", "")).lower().split())
    values |= set(str(tag.get("role", "")).lower().split())
    return bool(values & {"footnote", "endnote", "rearnote", "doc-footnote", "doc-endnote"})


def _has_block_child(tag):
    return any(getattr(child, "name", None) in _HEAVY_BLOCKS for child in tag.children)


def _only_filler_between(first, second):
    node = first.next_sibling
    while node is not None and node is not second:
        if isinstance(node, Comment):
            node = node.next_sibling
            continue
        if isinstance(node, NavigableString):
            if str(node).strip():
                return False
            node = node.next_sibling
            continue
        return False
    return node is second


#: 段落以这些字符开头时，视为「新的逻辑单元」，不与上一段合并。
NEW_UNIT_PREFIXES = frozenset("“”「『‘’\"'·•*—-–")


def _should_merge(first, second):
    """判断相邻两段是否为「被硬换行拆断的同一段」。

    除「上一段不像句末」这个主条件外，还加了若干保守守卫，宁可漏合并也不误合并：

    * 两段都非空、都不含块级子元素
    * 类名里带 title、或带注释语义（footnote / endnote）的不参与
    * 两段 ``class`` 必须一致（避免跨板块焊接）
    * 下一段若以引号、项目符号、破折号开头，视为新段落的开始
    """
    text_a = _plain_text(first)
    text_b = _plain_text(second)
    if not text_a or not text_b:
        return False
    if _has_block_child(first) or _has_block_child(second):
        return False
    if _looks_like_title(first) or _looks_like_title(second):
        return False
    if _has_note_type(first) or _has_note_type(second):
        return False
    if (first.get("class") or []) != (second.get("class") or []):
        return False
    if text_b[0] in NEW_UNIT_PREFIXES:
        return False
    return not _ends_sentence(text_a)


def _do_merge(first, second):
    # 去掉 first 末尾的换行与空白——它们正是「硬换行」留下的痕迹。
    while first.contents:
        last = first.contents[-1]
        if isinstance(last, NavigableString):
            if not str(last).strip():
                last.extract()
                continue
            break
        if getattr(last, "name", None) in ("br", "wbr"):
            last.extract()
            continue
        break

    before = _edge_char(first, last=True)
    after = _plain_text(second)[:1]
    if before and after and not (_is_cjk(before) and _is_cjk(after)):
        first.append(NavigableString(" "))

    while second.contents:
        first.append(second.contents[0])
    second.decompose()


def _merge_broken_paragraphs(soup, counters):
    containers = ("body", "div", "section", "article", "blockquote", "li", "td")
    for parent in list(soup.find_all(containers)):
        if _is_gone(parent):
            continue
        current = parent.find("p", recursive=False)
        while current is not None:
            sibling = current.find_next_sibling("p")
            if sibling is None:
                break
            if _only_filler_between(current, sibling) and _should_merge(current, sibling):
                _do_merge(current, sibling)
                counters["paragraphs_merged"] += 1
                continue
            current = sibling


# --------------------------------------------------------------------------- #
# 3. 清理冗余内联标签
# --------------------------------------------------------------------------- #
def _strip_redundant_inline(soup, counters):
    for tag in list(soup.find_all(INLINE_TAGS)):
        if _is_gone(tag):
            continue

        if tag.name == "font":
            tag.unwrap()
            counters["inline_tags_unwrapped"] += 1
            continue

        if tag.has_attr("style"):
            del tag["style"]
            counters["inline_styles_removed"] += 1

        if tag.name == "span" and not any(
            tag.get(attr) for attr in ("id", "class", "epub:type", "role")
        ):
            tag.unwrap()
            counters["inline_tags_unwrapped"] += 1

    # 合并相邻文本节点，避免拆 span 之后留下「两个空格」的缝隙。
    soup.smooth()

    for tag in list(soup.find_all(EMPTY_INLINE_TAGS)):
        if _is_gone(tag):
            continue
        if tag.find(["img", "image", "svg"]):
            continue
        if _plain_text(tag):
            continue
        tag.decompose()
        counters["empty_inline_removed"] += 1

    for tag in list(soup.find_all(EMPTY_INLINE_TAGS)):
        if _is_gone(tag):
            continue
        children = list(tag.children)
        if len(children) == 1 and getattr(children[0], "name", None) == tag.name:
            children[0].unwrap()
            counters["inline_tags_unwrapped"] += 1


# --------------------------------------------------------------------------- #
# 4. 自定义查找替换
# --------------------------------------------------------------------------- #
def parse_replacements(text):
    """把多行「查找 => 替换」解析成规则元组。

    分隔符可用 ``=>`` 或 ``→``；省略分隔符表示「删除匹配到的内容」。
    分隔符两侧的空白会自动去掉，规则内容本身保持原样。
    """
    rules = []
    for line in str(text or "").splitlines():
        if not line.strip():
            continue
        for separator in ("=>", "→"):
            if separator in line:
                pattern, replacement = line.split(separator, 1)
                rules.append((pattern.rstrip(), replacement.lstrip()))
                break
        else:
            rules.append((line, ""))
    return tuple(rules)


def prepare_options(options):
    """预编译替换规则，返回 ``(新 options, 被跳过的规则条数)``。

    规则只在整本书开始清洗时编译一次：否则每条无效规则会被按文档重复计数，
    「跳过 N 条」会随文档数膨胀，正则也要反复编译。
    """
    if not options.replacements:
        return options, 0

    compiled = []
    skipped = 0
    for pattern, replacement in options.replacements:
        if options.regex_replacements:
            try:
                compiled.append((re.compile(pattern), replacement))
            except re.error:
                skipped += 1
            continue
        if not pattern:
            skipped += 1
            continue
        compiled.append((pattern, replacement))

    return replace(options, replacements=tuple(compiled)), skipped


def _apply_replacements(soup, options, counters):
    compiled = options.replacements
    if not compiled:
        return

    for node in list(soup.find_all(string=True)):
        if isinstance(node, Comment) or _is_gone(node) or _in_skipped_context(node):
            continue

        text = str(node)
        new_text = text
        for pattern, replacement in compiled:
            if isinstance(pattern, str):
                if pattern in new_text:
                    counters["replacements_applied"] += new_text.count(pattern)
                    new_text = new_text.replace(pattern, replacement)
            else:
                new_text, count = pattern.subn(replacement, new_text)
                counters["replacements_applied"] += count

        if new_text != text:
            node.replace_with(NavigableString(new_text))


# --------------------------------------------------------------------------- #
# 保真度处理
# --------------------------------------------------------------------------- #
def _restore_formatting(output, source):
    """尽量还原原文的排版风格，减少无意义的 diff。"""
    if "\r\n" in source and "\r\n" not in output:
        output = output.replace("\n", "\r\n")

    # 原文若以「空格 + />」为主，就统一补回那个空格
    if source.count(" />") * 2 >= source.count("/>"):
        output = re.sub(r"(?<![\s/])/>", " />", output)

    # bs4 会在 DOCTYPE 与紧随其后的标签之间插入换行，原文没有则去掉
    output = re.sub(r"(<!DOCTYPE[^>]*>)\s*\n(?=<)", r"\1", output, count=1, flags=re.I)
    return output


def _fix_encoding_declaration(source, output):
    """统一按 UTF-8 写出，同步修正 XML 声明里写的编码。"""
    declaration = _XML_DECL_RE.search(source)
    if declaration and "utf-8" in declaration.group(0).lower():
        return output

    target = _XML_DECL_RE.search(output)
    if not target:
        return output
    fixed = re.sub(r'encoding\s*=\s*"[^"]*"', 'encoding="UTF-8"', target.group(0), flags=re.I)
    return output[: target.start()] + fixed + output[target.end():]


# --------------------------------------------------------------------------- #
# 对外接口
# --------------------------------------------------------------------------- #
def clean_document(source, options):
    """清洗单个 XHTML 文本，返回 ``(新文本, 本次计数)``。

    没有任何实际改动时**原样返回入参**，调用方据此跳过写盘，保证未改动的
    文档逐字节不变。
    """
    counters = _empty_counters()
    soup = BeautifulSoup(source, "html.parser")

    if options.strip_inline:
        _strip_redundant_inline(soup, counters)
    if options.normalize_whitespace:
        _normalize_whitespace(soup, counters)
    if options.remove_empty_paragraphs:
        _remove_empty_paragraphs(soup, counters)
    if options.merge_broken_paragraphs:
        _merge_broken_paragraphs(soup, counters)
    if options.replacements:
        if options.regex_replacements and any(
            isinstance(pattern, str) for pattern, _ in options.replacements
        ):
            # 直接用 clean_document 时的兜底；clean_book 会预先编译好传进来。
            options, skipped = prepare_options(options)
            counters["skipped_rules"] += skipped
        _apply_replacements(soup, options, counters)

    if not any(counters.values()):
        return source, counters

    output = _restore_formatting(str(soup), source)
    output = _fix_encoding_declaration(source, output)
    return output, counters


def map_text_nodes(source, mapper):
    """按**文本节点**改写 XHTML：``mapper(文本) -> 新文本``。

    与 :func:`clean_document` 同属「只碰可见文本」的改写入口，区别是改写规则
    完全由调用方决定（例如简繁转换），本函数只负责遍历与保真：

    * 跳过注释与 ``<script>`` / ``<style>`` / ``<head>`` 等上下文；
    * 标签、属性与 ``id`` / ``class`` / ``href`` 一律原样保留；
    * 没有任何改动时**原样返回入参**，调用方据此跳过写盘。

    返回 ``(新文本, 发生改动的文本节点数)``。
    """
    soup = BeautifulSoup(source, "html.parser")
    changed = 0

    for node in list(soup.find_all(string=True)):
        if isinstance(node, Comment) or _is_gone(node) or _in_skipped_context(node):
            continue
        text = str(node)
        new_text = mapper(text)
        if not isinstance(new_text, str) or new_text == text:
            continue
        node.replace_with(NavigableString(new_text))
        changed += 1

    if not changed:
        return source, 0

    output = _restore_formatting(str(soup), source)
    output = _fix_encoding_declaration(source, output)
    return output, changed


def clean_book(book_root, options, write=True):
    """按 spine 顺序清洗正文，返回 ``(stats, samples)``。

    ``write=False`` 时只做「试用」，不落盘，用于预览。
    ``samples`` 为前若干个文档的 ``(相对路径, 清洗后文本, 是否改动)``。
    """
    book_root = Path(book_root)
    documents = spine_documents(book_root)
    if not documents:
        raise EpubTextError(_EMPTY_SPINE_MESSAGE)

    stats = CleanStats()
    samples = []

    options, skipped_rules = prepare_options(options)
    stats.skipped_rules = skipped_rules

    for relative in documents:
        path = book_root / relative
        if not path.is_file():
            continue

        source = decode_text(path.read_bytes())
        cleaned, counters = clean_document(source, options)

        stats.documents += 1
        stats.add(counters)

        changed = cleaned != source
        if changed:
            stats.changed_documents += 1
            stats.changed_files.append(relative)
            if write:
                path.write_text(cleaned, encoding="utf-8")

        if len(samples) < PREVIEW_DOCUMENT_LIMIT:
            samples.append((relative, cleaned, changed))

    return stats, samples


def stats_text(stats):
    """把统计压成一句中文说明。"""
    parts = []
    if stats.documents:
        parts.append(f"扫描 {stats.documents} 个正文文档，其中 {stats.changed_documents} 个有改动")
    if stats.paragraphs_merged:
        parts.append(f"合并断行 {stats.paragraphs_merged} 处")
    if stats.empty_paragraphs_removed:
        parts.append(f"删除空段落 {stats.empty_paragraphs_removed} 个")
    if stats.whitespace_fixed:
        parts.append(f"规范化空白 {stats.whitespace_fixed} 处")
    unwrapped = stats.inline_tags_unwrapped + stats.empty_inline_removed
    if unwrapped or stats.inline_styles_removed:
        parts.append(f"清理内联标签 {unwrapped} 个、内联样式 {stats.inline_styles_removed} 处")
    if stats.replacements_applied:
        parts.append(f"查找替换 {stats.replacements_applied} 处")
    if stats.skipped_rules:
        parts.append(f"跳过无效规则 {stats.skipped_rules} 条")
    return "；".join(parts) if parts else "没有需要处理的内容。"


_PREVIEW_STYLE = """
body{margin:0;padding:20px;background:#fafbfc;color:#20242a;
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;
  font-size:15px;line-height:1.7}
h1{font-size:19px;margin:0 0 8px}
.summary{color:#68717d;font-size:13px;margin:0 0 18px}
.doc{border:1px solid #e1e5ea;border-radius:8px;background:#fff;padding:16px;margin-bottom:14px}
.doc h2{font-size:13px;margin:0 0 10px;color:#68717d;font-weight:600;word-break:break-all}
.flag{display:inline-block;margin-left:8px;padding:1px 7px;border-radius:999px;font-size:11px}
.flag.changed{background:#eff8f1;color:#28683b}
.flag.same{background:#f1f3f5;color:#77818c}
.content p{margin:0 0 10px}
.empty{color:#77818c;font-size:13px}
"""


def _body_inner_html(html_text):
    soup = BeautifulSoup(html_text, "html.parser")
    body = soup.find("body")
    return body.decode_contents() if body is not None else html_text


def render_preview(samples, stats):
    """把清洗结果渲染成可直接塞进 iframe 的预览文档。"""
    blocks = []
    for relative, cleaned, changed in samples:
        body = _body_inner_html(cleaned).strip()
        truncated = len(body) > PREVIEW_CHAR_LIMIT
        if truncated:
            body = body[:PREVIEW_CHAR_LIMIT]
        flag = "changed" if changed else "same"
        label = "已改动" if changed else "无改动"
        if not body:
            body = '<p class="empty">（该文档正文为空）</p>'
        elif truncated:
            body += '<p class="empty">…（预览已截断）</p>'
        blocks.append(
            f'<section class="doc"><h2>{escape(relative)}'
            f'<span class="flag {flag}">{label}</span></h2>'
            f'<div class="content">{body}</div></section>'
        )

    if not blocks:
        blocks.append('<p class="empty">没有可预览的正文文档。</p>')

    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        f"<title>清洗预览</title><style>{_PREVIEW_STYLE}</style></head><body>"
        "<h1>清洗预览</h1>"
        f'<p class="summary">{escape(stats_text(stats))}</p>'
        + "".join(blocks)
        + "</body></html>"
    )
