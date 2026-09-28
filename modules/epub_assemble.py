# -*- coding: utf-8 -*-
"""组装新 EPUB 的公共层：从一本或多本源书里挑出若干文档，重新装订成一本。

拆分与合并共用这一套逻辑：

* **拆分**：1 本源书，挑选其中若干正文文档 → 1 本新书
* **合并**：N 本源书，各自全部正文 → 1 本新书

本模块**没有** ``TOOL`` 字典，属于公共模块，``registry.discover()`` 会自动跳过。

核心设计：保持源书内部的相对目录结构
------------------------------------
多个源书时，给第 i 本的**所有**文件统一加一层 ``b{i}/`` 前缀。由于引用方与被
引用方一起平移，**同书内部的相对链接一个字都不用改**——这消掉了「重写链接」
这个最容易出错、最容易静默破坏内链的环节。

剩下只需要处理两类「断链」：

1. 链接指向**未被收录**的文档 → 摘掉 ``href`` 属性（标签保留、文字保留，
   只是不再是链接），并计入报告；
2. 链接指向**未收录的资源**（理论上不该发生，因为依赖会被递归收集）→ 同样摘掉。

输出格式固定为 **OPF 2.0 + NCX**：与项目里其它工具的产物一致，
兼容性最广，不需要维护 EPUB 2 / 3 两套目录结构。
"""
from __future__ import annotations

import posixpath
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlsplit
from xml.sax.saxutils import escape, quoteattr

from modules.epub_io import EPUB_MIMETYPE, HTML_EXTS, EpubIOError, decode_text
from modules.epub_package import find_opf_path, read_metadata

CONTAINER_XML = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
    "  <rootfiles>\n"
    '    <rootfile full-path="content.opf" media-type="application/oebps-package+xml"/>\n'
    "  </rootfiles>\n"
    "</container>\n"
)

NCX_MEDIA_TYPE = "application/x-dtbncx+xml"

#: 扩展名 → media-type；未命中时用 application/octet-stream。
MEDIA_TYPES = {
    ".xhtml": "application/xhtml+xml",
    ".html": "application/xhtml+xml",
    ".htm": "application/xhtml+xml",
    ".css": "text/css",
    ".js": "text/javascript",
    ".ncx": NCX_MEDIA_TYPE,
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".ttf": "application/font-sfnt",
    ".otf": "application/font-sfnt",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".mp3": "audio/mpeg",
    ".mp4": "video/mp4",
}

_NO_DOCUMENT_MESSAGE = "没有选中任何正文文档，无法生成新书"

# 用于「摘断链」：任何 href/src/poster（含 <a href>）都要检查
_ATTR_RE = re.compile(
    r"""(?P<name>(?:xlink:)?(?:href|src|poster))\s*=\s*(?P<quote>["'])(?P<value>.*?)(?P=quote)""",
    re.I | re.S,
)
_TAG_RE = re.compile(
    r"""<(?P<name>[A-Za-z][\w:.-]*)(?P<attrs>(?:[^>"']|"[^"]*"|'[^']*')*)>""", re.S
)
_ATTR_IN_TAG_RE = re.compile(
    r"""(?P<name>[A-Za-z_:][\w:.-]*)\s*=\s*(?P<quote>["'])(?P<value>.*?)(?P=quote)""", re.S
)
_URL_RE = re.compile(r"""url\(\s*(?P<quote>["']?)(?P<value>[^)"']+)(?P=quote)\s*\)""", re.I)
_IMPORT_RE = re.compile(r"""@import\s+(?:url\(\s*)?["'](?P<value>[^"']+)["']""", re.I)

#: 只有这些标签上的引用才算「资源依赖」。
#: ``<a href>`` 是**超链接**，绝不能当依赖——否则会把整本书的章节顺着链接全拖进来。
RESOURCE_ATTRS = {
    "link": ("href",),
    "img": ("src",),
    "image": ("xlink:href", "href"),
    "script": ("src",),
    "source": ("src",),
    "audio": ("src",),
    "video": ("src", "poster"),
    "object": ("data",),
    "embed": ("src",),
    "iframe": ("src",),
    "input": ("src",),
}


class AssembleError(EpubIOError):
    """组装新书失败。继承 ``EpubIOError``，便于工具统一兜底。"""


@dataclass(frozen=True)
class SourceBook:
    """一本已解包的源书。"""

    root: Path
    label: str = ""

    def __post_init__(self):
        object.__setattr__(self, "root", Path(self.root))


@dataclass(frozen=True)
class Selection:
    """要收录进新书的一个文档。``order`` 即新书里的 spine 顺序。"""

    source_index: int
    rel_path: str
    title: str = ""


@dataclass
class AssembleReport:
    """组装过程的可观测结果。"""

    documents: int = 0
    resources: int = 0
    covers: int = 0
    broken_links_removed: int = 0
    missing_files: list = field(default_factory=list)
    sources: int = 0
    prefix_applied: bool = False

    def as_dict(self):
        return {
            "documents": self.documents,
            "resources": self.resources,
            "covers": self.covers,
            "broken_links_removed": self.broken_links_removed,
            "missing_files": list(self.missing_files),
            "sources": self.sources,
            "prefix_applied": self.prefix_applied,
        }


def media_type_for(rel_path):
    return MEDIA_TYPES.get(Path(rel_path).suffix.lower(), "application/octet-stream")


# --------------------------------------------------------------------------- #
# 引用解析
# --------------------------------------------------------------------------- #
def resolve_reference(base_rel, reference):
    """把 ``reference`` 按 ``base_rel`` 所在目录解析成书内 posix 相对路径。

    外部链接（http/https/data/mailto）与纯片段（``#x``）返回 None。
    """
    if not reference:
        return None
    text = unquote(reference.strip())
    if not text or text.startswith("#"):
        return None
    parts = urlsplit(text)
    if parts.scheme or parts.netloc:
        return None
    target = parts.path
    if not target:
        return None
    joined = posixpath.join(posixpath.dirname(base_rel), target)
    normalized = posixpath.normpath(joined)
    if normalized.startswith("../") or normalized == "..":
        return None
    return normalized.lstrip("./")


def _references_in(text, rel_path):
    """列出一个 HTML 或 CSS 文件里引用到的**资源**路径（不含超链接）。"""
    found = []

    for tag in _TAG_RE.finditer(text):
        wanted = RESOURCE_ATTRS.get(tag.group("name").lower())
        if not wanted:
            continue
        for attribute in _ATTR_IN_TAG_RE.finditer(tag.group("attrs")):
            if attribute.group("name").lower() in wanted:
                found.append(attribute.group("value"))

    # url(...) 覆盖了 CSS 里的引用，以及 HTML 内联 style 与 <style> 块
    for match in _URL_RE.finditer(text):
        found.append(match.group("value"))
    for match in _IMPORT_RE.finditer(text):
        found.append(match.group("value"))

    resolved = []
    for reference in found:
        target = resolve_reference(rel_path, reference)
        if target:
            resolved.append(target)
    return resolved


def collect_dependencies(book_root, rel_path, report=None, seen=None):
    """递归收集一个文档直接/间接依赖的书内文件（不含自身）。"""
    book_root = Path(book_root)
    seen = set() if seen is None else seen
    pending = [rel_path]
    collected = set()

    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)

        path = book_root / current
        if not path.is_file():
            if report is not None and current != rel_path:
                report.missing_files.append(current)
            continue

        if path.suffix.lower() not in HTML_EXTS and path.suffix.lower() != ".css":
            continue

        try:
            text = decode_text(path.read_bytes())
        except OSError:
            continue

        for target in _references_in(text, current):
            if target in seen:
                continue
            collected.add(target)
            pending.append(target)

    return collected


# --------------------------------------------------------------------------- #
# 链接处理
# --------------------------------------------------------------------------- #
def _strip_broken_links(text, rel_path, allowed, report):
    """把指向未收录文件的 ``href``/``src`` 摘掉。

    只摘属性、不删标签：文字与版式全部保留，只是不再是链接。
    用正则做定点替换而非重新序列化，避免把 HTML 整体改写。
    """
    def replace(match):
        target = resolve_reference(rel_path, match.group("value"))
        if target is None or target in allowed:
            return match.group(0)
        report.broken_links_removed += 1
        return ""

    return _ATTR_RE.sub(replace, text)


def _rewrite_css_urls(text, rel_path, allowed, report):
    def replace(match):
        reference = match.group("value").strip()
        target = resolve_reference(rel_path, reference)
        if target is None or target in allowed:
            return match.group(0)
        report.broken_links_removed += 1
        return "none"

    return _URL_RE.sub(replace, text)


# --------------------------------------------------------------------------- #
# 目录与包文档
# --------------------------------------------------------------------------- #
def _build_ncx(uid, title, entries):
    """``entries``: ``[(label, src)]``。"""
    points = []
    for index, (label, src) in enumerate(entries, start=1):
        points.append(
            f'      <navPoint id="nav{index}" playOrder="{index}">\n'
            f"         <navLabel><text>{escape(label)}</text></navLabel>\n"
            f"         <content src={quoteattr(src)}/>\n"
            f"      </navPoint>"
        )
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<ncx version="2005-1" xmlns="http://www.daisy.org/z3986/2005/ncx/">\n'
        "   <head>\n"
        f'      <meta name="dtb:uid" content={quoteattr(uid)}/>\n'
        '      <meta name="dtb:depth" content="1"/>\n'
        '      <meta name="dtb:totalPageCount" content="0"/>\n'
        '      <meta name="dtb:maxPageNumber" content="0"/>\n'
        "   </head>\n"
        "   <docTitle>\n"
        f"      <text>{escape(title)}</text>\n"
        "   </docTitle>\n"
        "   <navMap>\n" + "\n".join(points) + "\n   </navMap>\n</ncx>\n"
    )


TOC_PAGE_TEMPLATE = """<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Strict//EN" \
"http://www.w3.org/TR/xhtml1/DTD/xhtml1-strict.dtd">
<html xmlns="http://www.w3.org/1999/xhtml" lang="{lang}" xml:lang="{lang}">
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


def _build_toc_page(title, lang, entries, css_hrefs):
    items = "\n".join(
        f'<li><a href={quoteattr(src)}>{escape(label)}</a></li>' for label, src in entries
    )
    links = "\n".join(
        f'<link href={quoteattr(href)} rel="stylesheet" type="text/css" />'
        for href in css_hrefs
    )
    return TOC_PAGE_TEMPLATE.format(
        lang=escape(lang or "zh-Hans"),
        title=escape(title),
        css_links=links,
        items=items,
    )


def _build_opf(meta, items, spine_ids, guide_cover):
    """``items``: ``[(item_id, href, media_type)]``；``spine_ids``: item id 顺序。"""
    manifest = "\n    ".join(
        f'<item id={quoteattr(item_id)} href={quoteattr(href)} media-type={quoteattr(media)}/>'
        for item_id, href, media in items
    )
    spine = "\n    ".join(f'<itemref idref={quoteattr(item_id)} linear="yes"/>' for item_id in spine_ids)
    guide = ""
    if guide_cover:
        guide = (
            "  <guide>\n"
            f'    <reference type="cover" title="Cover" href={quoteattr(guide_cover)}/>\n'
            "  </guide>\n"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="bookid">\n'
        '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:opf="http://www.idpf.org/2007/opf">\n'
        f"    <dc:title>{escape(meta['title'])}</dc:title>\n"
        f'    <dc:creator opf:role="aut">{escape(meta["author"])}</dc:creator>\n'
        f'    <dc:identifier id="bookid">{escape(meta["identifier"])}</dc:identifier>\n'
        f"    <dc:language>{escape(meta['language'])}</dc:language>\n"
        "  </metadata>\n"
        "  <manifest>\n"
        f"    {manifest}\n"
        "  </manifest>\n"
        '  <spine toc="ncx">\n'
        f"    {spine}\n"
        "  </spine>\n"
        f"{guide}"
        "</package>\n"
    )


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def cover_href(book_root):
    """找出一本源书的封面文档（相对路径），找不到返回 None。"""
    book_root = Path(book_root)
    opf_path = find_opf_path(book_root)
    raw = decode_text((book_root / opf_path).read_bytes())
    opf_dir = posixpath.dirname(opf_path)

    # guide 里的 type="cover"
    for match in re.finditer(r"<reference\b[^>]*>", raw, re.I):
        tag = match.group(0)
        if not re.search(r'type\s*=\s*["\']cover["\']', tag, re.I):
            continue
        href = re.search(r'href\s*=\s*["\']([^"\']+)["\']', tag, re.I)
        if href:
            target = posixpath.normpath(posixpath.join(opf_dir, href.group(1).strip()))
            if (book_root / target).is_file():
                return target.lstrip("./")

    # manifest 里第一个 properties 含 cover-image 的条目
    for match in re.finditer(r"<item\b[^>]*>", raw, re.I):
        tag = match.group(0)
        if "cover-image" not in tag.lower():
            continue
        href = re.search(r'href\s*=\s*["\']([^"\']+)["\']', tag, re.I)
        if href:
            target = posixpath.normpath(posixpath.join(opf_dir, href.group(1).strip()))
            if (book_root / target).is_file():
                return target.lstrip("./")
    return None


def _local_name(tag):
    name = str(tag)
    if "}" in name:
        name = name.rsplit("}", 1)[-1]
    if ":" in name:
        name = name.rsplit(":", 1)[-1]
    return name


def document_titles(book_root):
    """``{相对路径: 标题}``，用于把章节列表显示成人看得懂的名字。

    优先取 NCX 的 ``navLabel``；没有就用 HTML 的 ``<title>`` / 首个 ``<h1>``；
    再没有就回落到去扩展名的文件名。全部读不出来也不报错——只是标签难看一点。
    """
    from xml.etree import ElementTree as ET

    book_root = Path(book_root)
    titles = {}

    for ncx in sorted(book_root.rglob("*.ncx")):
        base = ncx.parent.relative_to(book_root).as_posix()
        try:
            root = ET.fromstring(decode_text(ncx.read_bytes()))
        except (ET.ParseError, OSError):
            continue
        for element in root.iter():
            if _local_name(element.tag) != "navPoint":
                continue
            label = ""
            source = ""
            for child in element.iter():
                local = _local_name(child.tag)
                if local == "text" and not label:
                    label = (child.text or "").strip()
                elif local == "content" and not source:
                    source = (child.get("src") or "").strip()
            if not label or not source:
                continue
            path = posixpath.join(base, urlsplit(source).path)
            titles.setdefault(posixpath.normpath(path).lstrip("./"), label)

    for path in sorted(book_root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in HTML_EXTS:
            continue
        relative = path.relative_to(book_root).as_posix()
        if titles.get(relative):
            continue
        text = decode_text(path.read_bytes())
        match = re.search(r"<title\b[^>]*>(.*?)</title>", text, re.S | re.I)
        if not match:
            match = re.search(r"<h1\b[^>]*>(.*?)</h1>", text, re.S | re.I)
        if match:
            label = re.sub(r"<[^>]+>", " ", match.group(1))
            label = re.sub(r"\s+", " ", label).strip()
            if label:
                titles[relative] = label

    return titles


def _prefix_for(index, total):
    return "" if total <= 1 else f"b{index + 1}/"


def _default_css(book_root, rel_path):
    """取一个文档直接引用的样式表（用于让新建的目录页沿用原书版式）。"""
    path = Path(book_root) / rel_path
    if not path.is_file():
        return []
    text = decode_text(path.read_bytes())
    hrefs = []
    for match in re.finditer(r"<link\b[^>]*>", text, re.I):
        tag = match.group(0)
        if "stylesheet" not in tag.lower():
            continue
        href = re.search(r'href\s*=\s*["\']([^"\']+)["\']', tag, re.I)
        if href:
            target = resolve_reference(rel_path, href.group(1))
            if target:
                hrefs.append(target)
    return hrefs


def assemble(sources, selections, dest_root, meta, include_cover=True, regenerate_toc=True):
    """把 ``selections`` 指定的文档装订成一本新书，写入 ``dest_root``。

    ``sources``: :class:`SourceBook` 列表
    ``selections``: :class:`Selection` 列表（顺序即新书 spine 顺序）
    ``meta``: ``{"title", "author", "language", "identifier"}``，缺项会自动补默认值
    """
    if not selections:
        raise AssembleError(_NO_DOCUMENT_MESSAGE)

    dest_root = Path(dest_root)
    total = len(sources)
    report = AssembleReport(sources=total, prefix_applied=total > 1)

    # 1. 先把要收录的文件集合算出来（文档 + 传递依赖 + 封面）
    wanted = {}          # (source_index, old_rel) -> kind
    document_keys = []
    cover_keys = []

    for source_index, source in enumerate(sources):
        if include_cover and source_index == 0:
            href = cover_href(source.root)
            if href:
                cover_keys.append((source_index, href))
                wanted[(source_index, href)] = "cover"
                for dependency in collect_dependencies(source.root, href, report):
                    wanted.setdefault((source_index, dependency), "resource")

    selected_keys = set()
    for selection in selections:
        if not 0 <= selection.source_index < total:
            raise AssembleError(f"源书序号越界：{selection.source_index}")
        source = sources[selection.source_index]
        key = (selection.source_index, selection.rel_path)
        if key in selected_keys:
            raise AssembleError(f"同一个文档被重复选中：{selection.rel_path}")
        selected_keys.add(key)

        # 如果这一章恰好就是封面，它在 spine 里已经作为封面存在，不重复收录
        if wanted.get(key) == "cover":
            continue

        wanted[key] = "document"
        document_keys.append(key)
        for dependency in collect_dependencies(source.root, selection.rel_path, report):
            wanted.setdefault((selection.source_index, dependency), "resource")

    if not document_keys:
        raise AssembleError(_NO_DOCUMENT_MESSAGE)

    # 2. 落盘：同书内部保持原有相对结构，多书时统一加 b{i}/ 前缀
    item_index = {}      # (source_index, old_rel) -> (item_id, new_rel)
    counters = {"doc": 0, "res": 0}
    for (source_index, old_rel), kind in wanted.items():
        prefix = _prefix_for(source_index, total)
        new_rel = f"{prefix}{old_rel}"
        target = dest_root / new_rel
        target.parent.mkdir(parents=True, exist_ok=True)
        source_path = sources[source_index].root / old_rel
        target.write_bytes(source_path.read_bytes())

        if kind == "document":
            counters["doc"] += 1
            item_id = f"doc{counters['doc']}"
        elif kind == "cover":
            counters["res"] += 1
            item_id = "cover"
        else:
            counters["res"] += 1
            item_id = f"res{counters['res']}"
        item_index[(source_index, old_rel)] = (item_id, new_rel)

    report.documents = counters["doc"]
    report.resources = counters["res"]
    report.covers = len(cover_keys)

    allowed_by_source = {}
    for (source_index, old_rel) in wanted:
        allowed_by_source.setdefault(source_index, set()).add(old_rel)

    # 3. 摘掉断链：只动属性，不重新序列化 HTML
    for (source_index, old_rel), kind in wanted.items():
        if kind not in ("document", "cover"):
            continue
        if Path(old_rel).suffix.lower() not in HTML_EXTS:
            continue
        new_rel = item_index[(source_index, old_rel)][1]
        path = dest_root / new_rel
        text = decode_text(path.read_bytes())
        rewritten = _strip_broken_links(
            text, old_rel, allowed_by_source.get(source_index, set()), report
        )
        if rewritten != text:
            path.write_text(rewritten, encoding="utf-8")

    for (source_index, old_rel), kind in wanted.items():
        if Path(old_rel).suffix.lower() != ".css":
            continue
        new_rel = item_index[(source_index, old_rel)][1]
        path = dest_root / new_rel
        text = decode_text(path.read_bytes())
        rewritten = _rewrite_css_urls(
            text, old_rel, allowed_by_source.get(source_index, set()), report
        )
        if rewritten != text:
            path.write_text(rewritten, encoding="utf-8")

    # 4. 重建目录页（沿用原书版式，只列本份的章节）
    toc_item = None
    if regenerate_toc:
        first_source, first_rel = document_keys[0]
        css_hrefs = [
            _prefix_for(first_source, total) + href
            for href in _default_css(sources[first_source].root, first_rel)
            if (first_source, href) in wanted
        ]
        entries = []
        for selection in selections:
            new_rel = item_index[(selection.source_index, selection.rel_path)][1]
            entries.append((selection.title or Path(selection.rel_path).stem, new_rel))
        toc_page = _build_toc_page(meta["title"], meta["language"], entries, css_hrefs)
        toc_rel = "toc.html"
        (dest_root / toc_rel).write_text(toc_page, encoding="utf-8")
        toc_item = ("tocpage", toc_rel, media_type_for(toc_rel))

    # 5. 包文档与 NCX
    manifest_items = []
    for (source_index, old_rel), kind in wanted.items():
        item_id, new_rel = item_index[(source_index, old_rel)]
        manifest_items.append((item_id, new_rel, media_type_for(new_rel)))

    spine_ids = []
    for source_index, href in cover_keys:
        item_id, _new_rel = item_index[(source_index, href)]
        spine_ids.append(item_id)
    if toc_item:
        manifest_items.append(toc_item)
        spine_ids.append(toc_item[0])
    for key in document_keys:
        spine_ids.append(item_index[key][0])

    manifest_items.append(("ncx", "toc.ncx", NCX_MEDIA_TYPE))

    entries = []
    if cover_keys:
        entries.append(("封面", item_index[cover_keys[0]][1]))
    if toc_item:
        entries.append(("目录", toc_item[1]))
    for selection in selections:
        new_rel = item_index[(selection.source_index, selection.rel_path)][1]
        entries.append((selection.title or Path(selection.rel_path).stem, new_rel))

    uid = meta["identifier"]
    (dest_root / "toc.ncx").write_text(
        _build_ncx(uid, meta["title"], entries), encoding="utf-8"
    )
    (dest_root / "content.opf").write_text(
        _build_opf(meta, manifest_items, spine_ids, cover_keys and item_index[cover_keys[0]][1]),
        encoding="utf-8",
    )

    meta_inf = dest_root / "META-INF"
    meta_inf.mkdir(parents=True, exist_ok=True)
    (meta_inf / "container.xml").write_text(CONTAINER_XML, encoding="utf-8")
    (dest_root / "mimetype").write_text(EPUB_MIMETYPE, encoding="ascii")

    return report


def book_metadata(book_root):
    """读一本源书的元数据（``values`` 字典）。"""
    return read_metadata(Path(book_root)).values


def new_identifier():
    return f"urn:uuid:{uuid.uuid4()}"
