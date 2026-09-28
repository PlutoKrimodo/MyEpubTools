# -*- coding: utf-8 -*-
"""EPUB → 纯文本公共层。

与其它公共层的分工：本模块只负责**从正文里把文字取出来**，不碰归档、不碰 OPF 写入。

提取规则
--------
* 范围：按 ``spine`` 顺序的正文文档，复用
  :func:`modules.epub_package.spine_documents`——封面 / 目录 / 导航文档不进入结果。
* 取块：只取**最内层**的块级元素（``p`` / ``h1``~``h6`` / ``li`` / ``div`` …）。
  ``<div><p>正文</p></div>`` 只会产出一次，不会父子各算一遍。
* 换行：正文里的 ``<br>`` 保留成独立一行（先用哨兵占位，避免和源码缩进的换行混淆）。
* 丢弃：``script`` / ``style`` / ``svg`` 以及注音标签 ``rt`` / ``rp``
  ——中文拼音、日文假名注音不该混进正文。
* 空白：行内的连续空白折叠成一个空格；段首缩进（含全角空格 ``U+3000``）去掉，
  与 ``build_epub`` 生成 EPUB 时的处理一致，保证往返稳定。
* 标题：``h1``~``h6`` 视为标题单独成行；文档没有标题时回落到 ``<title>``，
  但它与书名相同时不用（很多书每个文档的 ``<title>`` 都是书名，那会变成噪声）。

本模块**没有** ``TOOL`` 字典，属于公共模块，``registry.discover()`` 会自动跳过。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from bs4 import BeautifulSoup

from modules.epub_io import EpubIOError, extract_epub, decode_text
from modules.epub_package import read_metadata, spine_documents

#: 参与提取的块级元素。``div`` / ``section`` 这类容器只在「不含其它块」时才产出内容。
BLOCK_TAGS = (
    "h1", "h2", "h3", "h4", "h5", "h6",
    "p", "li", "blockquote", "div", "section", "article", "aside",
    "td", "th", "dd", "dt", "pre", "figcaption", "figure",
)

HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})

_NOISE_TAGS = ("script", "style", "svg", "noscript", "rt", "rp")

#: ``<br>`` 的占位符：正文里不可能出现的控制字符，用它把「真换行」与源码缩进分开。
_BR_SENTINEL = "\x00"

_WHITESPACE_RE = re.compile(r"[ \t\r\n\f\v\u00a0\u200b]+")

#: 可选换行符。``\n`` / ``\r\n``。
LINE_ENDINGS = {"lf": "\n", "crlf": "\r\n"}

#: 可选输出编码。写出字节时由 :func:`encode_text` 负责。
ENCODINGS = ("utf-8", "utf-8-sig", "gb18030")


class EpubToTxtError(EpubIOError):
    """EPUB 正文提取失败。继承 ``EpubIOError``，便于工具统一兜底。"""


@dataclass(frozen=True)
class ExtractOptions:
    """导出为纯文本的选项。"""

    keep_titles: bool = True
    """保留 ``h1``~``h6`` 标题行；关掉则连小标题一起去掉，只留正文段落。"""

    blank_line: bool = False
    """每一行之后空一行。"""

    line_ending: str = "lf"
    """``lf`` 或 ``crlf``。"""

    encoding: str = "utf-8"
    """``utf-8`` / ``utf-8-sig`` / ``gb18030``。"""

    def __post_init__(self):
        if self.line_ending not in LINE_ENDINGS:
            raise EpubToTxtError(f"不支持的换行符：{self.line_ending}")
        if self.encoding not in ENCODINGS:
            raise EpubToTxtError(f"不支持的输出编码：{self.encoding}")


@dataclass
class ExtractStats:
    """提取结果的分项计数，可直接转成 JSON 回传前端。"""

    title: str = ""
    author: str = ""
    documents: int = 0
    headings: int = 0
    paragraphs: int = 0
    characters: int = 0

    def as_dict(self):
        return {
            "title": self.title,
            "author": self.author,
            "documents": self.documents,
            "headings": self.headings,
            "paragraphs": self.paragraphs,
            "characters": self.characters,
        }

    def summary(self):
        """一句中文摘要，供日志与状态条使用。"""
        return (
            f"正文文档 {self.documents} 个、标题 {self.headings} 条、"
            f"段落 {self.paragraphs} 段，共 {self.characters} 字"
        )


@dataclass
class ExtractResult:
    """提取结果：``lines`` 是逐行文本，``text`` 是按选项拼好的整段文本。"""

    lines: list = field(default_factory=list)
    text: str = ""
    stats: ExtractStats = field(default_factory=ExtractStats)
    options: ExtractOptions = field(default_factory=ExtractOptions)


# --------------------------------------------------------------------------- #
# 单篇文档
# --------------------------------------------------------------------------- #
def _normalize(text):
    """折叠行内空白并去掉首尾空白（含段首的全角空格缩进）。"""
    return _WHITESPACE_RE.sub(" ", text).strip()


def _prepare_soup(raw):
    soup = BeautifulSoup(raw, "html.parser")
    for tag in soup.find_all(_NOISE_TAGS):
        if tag.parent is not None:
            tag.decompose()
    return soup


def _block_lines(element):
    """取一个块级元素里的文字，``<br>`` 变成独立的行。"""
    for br in element.find_all("br"):
        br.replace_with(_BR_SENTINEL)

    parts = (_normalize(part) for part in element.get_text().split(_BR_SENTINEL))
    return [part for part in parts if part]


def _leaf_blocks(soup):
    """按文档顺序产出最内层块级元素，避免 ``div`` 与其中的 ``p`` 重复计数。"""
    block_tags = list(BLOCK_TAGS)
    for element in soup.find_all(block_tags):
        if element.find(block_tags) is not None:
            continue
        yield element


def _document_title(soup, book_title):
    """文档的标题：首个标题元素，回落到 ``<title>``（与书名相同则不用）。"""
    for element in soup.find_all(list(HEADING_TAGS)):
        text = _normalize(element.get_text())
        if text:
            return text

    node = soup.find("title")
    if node is not None:
        text = _normalize(node.get_text())
        if text and text != _normalize(book_title or ""):
            return text
    return ""


def extract_document(raw, book_title="", options=None):
    """提取单篇正文文档，返回 ``(标题, [行, ...], 标题条数, 段落条数)``。"""
    options = options or ExtractOptions()
    soup = _prepare_soup(raw)
    title = _document_title(soup, book_title)

    lines = []
    headings = 0
    paragraphs = 0
    title_emitted = False

    for element in _leaf_blocks(soup):
        block_lines = _block_lines(element)
        if not block_lines:
            continue
        if element.name in HEADING_TAGS:
            headings += 1
            title_emitted = True
            if options.keep_titles:
                lines.extend(block_lines)
            continue
        paragraphs += len(block_lines)
        lines.extend(block_lines)

    # 文档里没有标题元素、但 ``<title>`` 能给出标题时，补一行上去。
    # 要求本就有正文行：否则会给「只有一张图、没有文字」的文档凭空造出一行标题。
    if title and not title_emitted and options.keep_titles and lines:
        lines.insert(0, title)
        headings += 1

    return title, lines, headings, paragraphs


# --------------------------------------------------------------------------- #
# 整本书
# --------------------------------------------------------------------------- #
def _render_lines(lines, options):
    """按换行符与空行选项拼成最终文本，保证文件以单个换行结尾。"""
    if not lines:
        return ""
    ending = LINE_ENDINGS[options.line_ending]
    separator = ending + ending if options.blank_line else ending
    return separator.join(lines) + ending


def extract(book_root, options=None):
    """提取解包目录 ``book_root`` 的正文，返回 :class:`ExtractResult`。"""
    options = options or ExtractOptions()
    book_root = Path(book_root)

    metadata = read_metadata(book_root)
    book_title = metadata.values.get("title", "")
    stats = ExtractStats(title=book_title, author=metadata.values.get("creator", ""))

    documents = spine_documents(book_root)
    if not documents:
        raise EpubToTxtError(
            "这份 EPUB 的 spine 里没有可用的正文文档，无法提取文本"
            "（只有封面 / 目录的书没有正文）。"
        )

    lines = []
    for relative in documents:
        target = book_root / relative
        if not target.is_file():
            continue
        raw = decode_text(target.read_bytes())
        _title, document_lines, headings, paragraphs = extract_document(
            raw, book_title, options
        )
        if not document_lines:
            continue
        stats.documents += 1
        stats.headings += headings
        stats.paragraphs += paragraphs
        lines.extend(document_lines)

    if not lines:
        raise EpubToTxtError("正文文档里没有提取到任何文字。")

    text = _render_lines(lines, options)
    stats.characters = len(text) - text.count("\n") - text.count("\r")
    return ExtractResult(lines=lines, text=text, stats=stats, options=options)


def extract_from_epub(source, work_dir, options=None):
    """从 EPUB（bytes 或路径）提取正文。

    ``source`` 先按公共层规则解包到 ``work_dir``，再走 :func:`extract`；
    解包结果由调用方决定何时清理。
    """
    options = options or ExtractOptions()
    book_root = extract_epub(source, work_dir)
    return extract(book_root, options)


def encode_text(text, encoding="utf-8"):
    """把文本编成要写盘的字节；``utf-8-sig`` 会补上 BOM。"""
    if encoding not in ENCODINGS:
        raise EpubToTxtError(f"不支持的输出编码：{encoding}")
    if encoding == "utf-8-sig":
        return ("\ufeff" + text).encode("utf-8")
    return text.encode(encoding)


def write_text_file(path, result):
    """按结果里的选项把文本写到 ``path``（以字节写出，不受平台换行影响）。"""
    options = result.options
    payload = encode_text(result.text, options.encoding)
    with open(path, "wb") as handle:
        handle.write(payload)
    return Path(path)
