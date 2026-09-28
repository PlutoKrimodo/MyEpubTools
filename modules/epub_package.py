# -*- coding: utf-8 -*-
"""EPUB 包文档（OPF / NCX）读写公共层。

与 :mod:`modules.epub_io` 的分工：

* ``epub_io``：**归档级**——解包 / 重打包 / 成员路径校验 / 文本解码
* ``epub_package``：**文档级**——定位 OPF、读写 ``dc:*`` 元数据、同步目录标题

本模块**没有** ``TOOL`` 字典，属于公共模块，``registry.discover()`` 会自动跳过。

写入策略（重要，勿改回 ElementTree）
------------------------------------
不能用 :mod:`xml.etree.ElementTree` 整体往返重写 OPF。实测三种命名空间策略都不可行：

* ``register_namespace('opf', OPF_NS)``：全文元素被迫加上 ``opf:`` 前缀，改动面巨大
* ``tostring(default_namespace=OPF_NS)``：直接抛
  ``ValueError: cannot use non-qualified names with default_namespace option``
* ``register_namespace('', OPF_NS)``：``opf:role="aut"`` 会被写成 ``role="aut"``，
  丢失 ``opf`` 命名空间，语义损坏

根因是 ``ElementTree`` 的命名空间映射是 ``URI -> 前缀`` 的单值字典，同一个 URI
无法同时充当默认命名空间与 ``opf`` 前缀。

因此本模块只把 ``ElementTree`` 用于**读取**；写入采用**文本级定点重写**：
只重建 ``<metadata>`` 内部被托管的 ``dc:*`` 元素，``<package>`` 起始标签、
``manifest`` / ``spine`` / ``guide``、缩进与换行、以及任何非托管子元素
（``<meta>``、注释、``dc:contributor`` 等）全部逐字节保留。
"""
from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

from modules.epub_io import HTML_EXTS, EpubIOError, decode_text

OPF_NS = "http://www.idpf.org/2007/opf"
DC_NS = "http://purl.org/dc/elements/1.1/"
NCX_NS = "http://www.daisy.org/z3986/2005/ncx/"
NCX_MEDIA_TYPE = "application/x-dtbncx+xml"

CONTAINER_RELATIVE = "META-INF/container.xml"

#: 本层托管的元数据字段，顺序即「新增元素」时的追加顺序。
FIELD_KEYS = (
    "title",
    "creator",
    "language",
    "identifier",
    "publisher",
    "date",
    "description",
    "subject",
)

MANAGED_ELEMENTS = frozenset(FIELD_KEYS)

#: 允许多值的字段：作者用 ``/`` 分隔，主题标签每行一个。
MULTI_VALUE_KEYS = frozenset({"creator", "subject"})

CREATOR_SEPARATOR = "/"

DEFAULT_INDENT = "\n    "

_UNKNOWN_OPF_MESSAGE = "EPUB 中找不到 .opf 包文件"
_MISSING_METADATA_MESSAGE = "OPF 中缺少 <metadata> 节点，无法写入元数据"


class EpubPackageError(EpubIOError):
    """OPF / NCX 文档读写失败。继承 ``EpubIOError``，便于工具统一兜底。"""


@dataclass(frozen=True)
class BookMetadata:
    """一次元数据读取的结果。"""

    opf_path: str
    values: dict

    @property
    def filled(self):
        """有值的字段数量，用于生成提示文案。"""
        return sum(1 for value in self.values.values() if str(value or "").strip())


# --------------------------------------------------------------------------- #
# 定位
# --------------------------------------------------------------------------- #
def _local_name(tag):
    """取 ``{uri}title`` / ``dc:title`` 的本地名 ``title``。"""
    name = str(tag)
    if "}" in name:
        name = name.rsplit("}", 1)[-1]
    if ":" in name:
        name = name.rsplit(":", 1)[-1]
    return name


def find_opf_path(book_root):
    """定位解包目录里的 OPF 文件（相对 ``book_root`` 的 posix 路径）。

    优先读 ``META-INF/container.xml`` 的 ``<rootfile full-path="...">``
    ——OPF 不一定在根目录，很多书放在 ``OEBPS/`` 下；解析失败才回落到扫描 ``*.opf``。
    """
    book_root = Path(book_root)
    container = book_root / CONTAINER_RELATIVE

    if container.is_file():
        try:
            root = ET.fromstring(decode_text(container.read_bytes()))
        except ET.ParseError:
            root = None
        if root is not None:
            for element in root.iter():
                if _local_name(element.tag) != "rootfile":
                    continue
                full_path = (element.get("full-path") or "").strip()
                if full_path and (book_root / full_path).is_file():
                    return full_path.replace("\\", "/").lstrip("./")

    candidates = sorted(path for path in book_root.rglob("*.opf") if path.is_file())
    if not candidates:
        raise EpubPackageError(_UNKNOWN_OPF_MESSAGE)
    return candidates[0].relative_to(book_root).as_posix()


# --------------------------------------------------------------------------- #
# 读取
# --------------------------------------------------------------------------- #
def _parse_opf(raw):
    """解析 OPF 文本，返回 ``(root, metadata 元素或 None)``。"""
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise EpubPackageError(f"OPF 不是合法的 XML：{exc}") from exc

    metadata = None
    for child in root:
        if _local_name(child.tag) == "metadata":
            metadata = child
            break
    return root, metadata


def _element_text(element):
    return "".join(element.itertext()).strip()


def read_metadata(book_root):
    """读取 OPF 中的元数据，返回 :class:`BookMetadata`。

    ``values`` 的键固定为 :data:`FIELD_KEYS`；多值字段已合并成单个字符串
    （``creator`` 用 ``" / "``，``subject`` 用换行），便于直接填入表单。
    """
    book_root = Path(book_root)
    opf_path = find_opf_path(book_root)
    raw = decode_text((book_root / opf_path).read_bytes())
    _root, metadata = _parse_opf(raw)

    values = {key: "" for key in FIELD_KEYS}
    if metadata is not None:
        creators = []
        subjects = []
        for child in metadata:
            local = _local_name(child.tag)
            text = _element_text(child)
            if local == "creator":
                if text:
                    creators.append(text)
            elif local == "subject":
                if text:
                    subjects.append(text)
            elif local in FIELD_KEYS and not values[local]:
                values[local] = text
        values["creator"] = " / ".join(creators)
        values["subject"] = "\n".join(subjects)

    return BookMetadata(opf_path=opf_path, values=values)


# --------------------------------------------------------------------------- #
# 写入：文本级定点重写
# --------------------------------------------------------------------------- #
# 一次匹配一个「标签 / 注释 / CDATA / 处理指令」，属性值内的 > 不会截断匹配。
_TAG_RE = re.compile(
    r"<!--.*?-->"
    r"|<!\[CDATA\[.*?\]\]>"
    r"|<\?.*?\?>"
    r"|</?[A-Za-z_][\w.:-]*(?:[^>\"']|\"[^\"]*\"|'[^']*')*>",
    re.S,
)

_METADATA_OPEN_RE = re.compile(r"<metadata\b(?:[^>\"']|\"[^\"]*\"|'[^']*')*>", re.S | re.I)
_METADATA_CLOSE_RE = re.compile(r"</metadata\s*>", re.I)
_XML_DECL_RE = re.compile(r"<\?xml\b[^>]*\?>")


def _start_tag_info(source):
    """解析元素起始标签，返回 ``(原始标签名, 属性串, 是否自闭合)``。

    属性串保持原样（含 ``opf:role="aut"``、``id="bookid"`` 这类带前缀的属性），
    这是保留 OPF 语义的关键。
    """
    tag = _TAG_RE.match(source or "")
    if not tag:
        return None, "", False
    text = tag.group(0)
    if text.startswith("<!") or text.startswith("<?"):
        return None, "", False

    body = text[1:]
    if body.startswith("/"):
        return None, "", False
    self_closing = body.endswith("/>")
    body = body[:-2] if self_closing else body[:-1]
    body = body.rstrip()

    parts = re.split(r"\s+", body, maxsplit=1)
    name = parts[0]
    attrs = (" " + parts[1].strip()) if len(parts) > 1 and parts[1].strip() else ""
    return name, attrs, self_closing


def _split_children(inner):
    """把 ``<metadata>`` 的内部内容切成 ``[(前置空白, 元素源码), ...]``。

    拼接所有片段可逐字节还原 ``inner``，包括注释与缩进。
    """
    chunks = []
    depth = 0
    leading = ""
    buffer = ""
    position = 0

    for match in _TAG_RE.finditer(inner):
        text = match.group(0)
        literal = inner[position:match.start()]
        if depth == 0:
            leading += literal
        else:
            buffer += literal
        position = match.end()

        if text.startswith("<!--") or text.startswith("<![CDATA[") or text.startswith("<?"):
            if depth == 0:
                leading += text
            else:
                buffer += text
            continue

        if text.startswith("</"):
            depth -= 1
            buffer += text
            if depth <= 0:
                depth = 0
                chunks.append((leading, buffer))
                leading = ""
                buffer = ""
            continue

        self_closing = text.endswith("/>")
        buffer += text
        if depth == 0:
            if self_closing:
                chunks.append((leading, buffer))
                leading = ""
                buffer = ""
            else:
                depth = 1
        elif not self_closing:
            depth += 1

    tail = inner[position:]
    if depth == 0:
        leading += tail
        buffer = ""
    else:
        buffer += tail
    if leading or buffer:
        chunks.append((leading, buffer))

    return chunks


def _ensure_namespace(open_tag, prefix, uri):
    """确保起始标签声明了某个命名空间前缀。"""
    if re.search(r"\bxmlns:%s\s*=" % re.escape(prefix), open_tag):
        return open_tag
    if open_tag.endswith("/>"):
        return open_tag[:-2].rstrip() + f' xmlns:{prefix}="{uri}"/>'
    return open_tag[:-1].rstrip() + f' xmlns:{prefix}="{uri}">'


def _normalize_values(values):
    """把表单值规整为 ``{字段: [待输出文本, ...]}``；空值即空列表（表示删除该元素）。"""
    def single(key):
        return str(values.get(key) or "").strip()

    creators = [
        item.strip()
        for item in single("creator").split(CREATOR_SEPARATOR)
        if item.strip()
    ]
    subjects = [line.strip() for line in single("subject").splitlines() if line.strip()]

    normalized = {}
    for key in FIELD_KEYS:
        if key == "creator":
            normalized[key] = creators
        elif key == "subject":
            normalized[key] = subjects
        else:
            text = single(key)
            normalized[key] = [text] if text else []
    return normalized


def _package_unique_identifier(raw):
    match = _TAG_RE.match(raw.lstrip())
    if not match:
        return ""
    name, attrs, _closing = _start_tag_info(match.group(0))
    if name is None or _local_name(name) != "package":
        return ""
    found = re.search(r'\bunique-identifier\s*=\s*"([^"]*)"', attrs)
    return found.group(1) if found else ""


def _default_attributes(field, unique_identifier):
    if field == "creator":
        return ' opf:role="aut"'
    if field == "identifier":
        return f' id="{unique_identifier}"' if unique_identifier else ""
    return ""


def rewrite_metadata_block(raw, values):
    """重写 OPF 文本中 ``<metadata>`` 的托管元素，其余内容逐字节保留。"""
    open_match = _METADATA_OPEN_RE.search(raw)
    if not open_match:
        raise EpubPackageError(_MISSING_METADATA_MESSAGE)
    close_match = _METADATA_CLOSE_RE.search(raw, open_match.end())
    if not close_match:
        raise EpubPackageError(_MISSING_METADATA_MESSAGE)

    open_tag = open_match.group(0)
    inner = raw[open_match.end():close_match.start()]
    chunks = _split_children(inner)
    normalized = _normalize_values(values)
    unique_identifier = _package_unique_identifier(raw)

    needs_dc = any(normalized[key] for key in FIELD_KEYS)
    needs_opf = bool(normalized["creator"])
    if needs_dc:
        open_tag = _ensure_namespace(open_tag, "dc", DC_NS)
    if needs_opf:
        open_tag = _ensure_namespace(open_tag, "opf", OPF_NS)

    # 末段若只是空白，视作「收尾留白」，新元素插在它之前。
    trailing = ""
    if chunks and not chunks[-1][1] and chunks[-1][0]:
        trailing = chunks[-1][0]
        chunks = chunks[:-1]

    indent = DEFAULT_INDENT
    for leading, _element in chunks:
        if "\n" in leading:
            indent = leading
            break

    pieces = []
    emitted = set()
    for leading, element in chunks:
        if not element:
            pieces.append(leading)
            continue

        name, attrs, _closing = _start_tag_info(element)
        local = _local_name(name) if name else ""
        if local not in MANAGED_ELEMENTS:
            pieces.append(leading + element)
            continue

        # 同一字段出现多次时只在首次位置输出，其余旧元素删除（避免重复）。
        if local in emitted:
            continue
        emitted.add(local)

        texts = normalized[local]
        if not texts:
            pieces.append(leading)  # 值被清空 → 删除该元素，保留原有空白
            continue

        for index, text in enumerate(texts):
            prefix = leading if index == 0 else indent
            pieces.append(f"{prefix}<{name}{attrs}>{escape(text)}</{name}>")

    # 原本不存在、但用户填了值的字段，按 FIELD_KEYS 顺序补在末尾。
    for field in FIELD_KEYS:
        if field in emitted or not normalized[field]:
            continue
        tag = f"dc:{field}"
        attrs = _default_attributes(field, unique_identifier)
        for text in normalized[field]:
            pieces.append(f"{indent}<{tag}{attrs}>{escape(text)}</{tag}>")

    new_inner = "".join(pieces) + trailing
    new_raw = raw[: open_match.start()] + open_tag + new_inner + close_match.group(0) + raw[close_match.end():]

    # XML 声明与实际写出的编码保持一致（统一写 UTF-8）。
    declaration = _XML_DECL_RE.search(new_raw)
    if declaration and "utf-8" not in declaration.group(0).lower():
        fixed = re.sub(
            r'encoding\s*=\s*"[^"]*"',
            'encoding="UTF-8"',
            declaration.group(0),
            flags=re.I,
        )
        new_raw = new_raw[: declaration.start()] + fixed + new_raw[declaration.end():]

    try:
        ET.fromstring(new_raw)
    except ET.ParseError as exc:  # 宁可报错，也不写坏用户的文件
        raise EpubPackageError(f"写入元数据后 OPF 不是合法 XML：{exc}") from exc

    return new_raw


def write_metadata(book_root, values):
    """把 ``values`` 写回 OPF。返回 ``(opf 相对路径, 文件是否真的变化)``。"""
    book_root = Path(book_root)
    opf_path = find_opf_path(book_root)
    opf_file = book_root / opf_path
    raw = decode_text(opf_file.read_bytes())

    new_raw = rewrite_metadata_block(raw, values)
    if new_raw == raw:
        return opf_path, False

    opf_file.write_text(new_raw, encoding="utf-8")
    return opf_path, True


# --------------------------------------------------------------------------- #
# 目录标题同步
# --------------------------------------------------------------------------- #
_NCX_DOC_TITLE_RE = re.compile(
    r"(<docTitle\b[^>]*>\s*<text\b[^>]*>)(.*?)(</text>)",
    re.S | re.I,
)
_HTML_TITLE_RE = re.compile(r"(<title\b[^>]*>)(.*?)(</title>)", re.S | re.I)


def _manifest_items(root):
    """列出 ``manifest`` 中的条目：``[(href, media_type, properties), ...]``。"""
    items = []
    for element in root.iter():
        if _local_name(element.tag) != "item":
            continue
        href = (element.get("href") or "").strip()
        if href:
            items.append(
                (
                    href,
                    (element.get("media-type") or "").strip().lower(),
                    (element.get("properties") or "").strip().lower(),
                )
            )
    return items


def sync_titles(book_root, title):
    """把书名同步到 NCX 的 ``docTitle`` 与 EPUB3 nav 文档的 ``<title>``。

    返回被更新的文件相对路径列表；文件不存在或找不到目标节点时静默跳过
    （元数据写入本身不应因为目录文件缺失而失败）。
    """
    title = str(title or "").strip()
    if not title:
        return []

    book_root = Path(book_root)
    opf_path = find_opf_path(book_root)
    opf_dir = posixpath.dirname(opf_path)

    try:
        root, _metadata = _parse_opf(decode_text((book_root / opf_path).read_bytes()))
    except EpubIOError:
        return []

    updated = []
    ncx_hrefs = []
    nav_hrefs = []
    for href, media_type, properties in _manifest_items(root):
        if media_type == NCX_MEDIA_TYPE:
            ncx_hrefs.append(href)
        if "nav" in properties.split():
            nav_hrefs.append(href)

    # NCX：优先用 manifest 声明，回落为扫描 *.ncx
    if not ncx_hrefs:
        ncx_hrefs = [
            path.relative_to(book_root).as_posix()
            for path in sorted(book_root.rglob("*.ncx"))
        ]

    for href in ncx_hrefs:
        target = book_root / posixpath.normpath(posixpath.join(opf_dir, href))
        if not target.is_file():
            continue
        raw = decode_text(target.read_bytes())
        new_raw = _NCX_DOC_TITLE_RE.sub(
            lambda match: match.group(1) + escape(title) + match.group(3),
            raw,
            count=1,
        )
        if new_raw != raw:
            target.write_text(new_raw, encoding="utf-8")
            updated.append(target.relative_to(book_root).as_posix())

    for href in nav_hrefs:
        target = book_root / posixpath.normpath(posixpath.join(opf_dir, href))
        if not target.is_file():
            continue
        raw = decode_text(target.read_bytes())
        new_raw = _HTML_TITLE_RE.sub(
            lambda match: match.group(1) + escape(title) + match.group(3),
            raw,
            count=1,
        )
        if new_raw != raw:
            target.write_text(new_raw, encoding="utf-8")
            updated.append(target.relative_to(book_root).as_posix())

    return updated


# --------------------------------------------------------------------------- #
# 正文文档定位
# --------------------------------------------------------------------------- #
#: 文件名 / manifest id 里出现这些「整词」时，视为封面、目录或导航文档。
_FRONT_MATTER_TOKENS = frozenset({"cover", "toc", "nav", "contents"})


def _is_front_matter(item, guide_hrefs):
    if "nav" in item["properties"]:
        return True
    if posixpath.normpath(item["href"]) in guide_hrefs:
        return True

    stem = posixpath.splitext(posixpath.basename(item["href"]))[0]
    tokens = set(re.split(r"[-_.\s]+", f"{item['id']} {stem}".lower()))
    return bool(tokens & _FRONT_MATTER_TOKENS)


def spine_documents(book_root, exclude_front_matter=True):
    """按 spine 顺序返回正文文档的相对路径列表。

    默认排除封面 / 目录 / 导航文档：``properties`` 含 ``nav``、被 ``<guide>``
    引用、或 manifest id / 文件名里出现 ``cover`` / ``toc`` / ``nav`` / ``contents``
    这类「整词」的条目。

    找不到可用正文时返回空列表（由调用方决定如何提示），**不会**退化成
    「处理全书所有 HTML」——那会违背「只处理 spine 正文」的约定。
    """
    book_root = Path(book_root)
    opf_path = find_opf_path(book_root)
    raw = decode_text((book_root / opf_path).read_bytes())
    root, _metadata = _parse_opf(raw)
    opf_dir = posixpath.dirname(opf_path)

    manifest = {}
    for element in root.iter():
        if _local_name(element.tag) != "item":
            continue
        item_id = (element.get("id") or "").strip()
        href = (element.get("href") or "").strip()
        if not item_id or not href:
            continue
        manifest[item_id] = {
            "id": item_id,
            "href": href,
            "properties": (element.get("properties") or "").lower().split(),
        }

    guide_hrefs = set()
    for element in root.iter():
        if _local_name(element.tag) != "reference":
            continue
        href = (element.get("href") or "").strip()
        if href:
            guide_hrefs.add(posixpath.normpath(href))

    documents = []
    for element in root.iter():
        if _local_name(element.tag) != "itemref":
            continue
        item = manifest.get((element.get("idref") or "").strip())
        if item is None:
            continue
        if exclude_front_matter and _is_front_matter(item, guide_hrefs):
            continue

        relative = posixpath.normpath(posixpath.join(opf_dir, item["href"])).lstrip("./")
        target = book_root / relative
        if target.is_file() and target.suffix.lower() in HTML_EXTS:
            documents.append(relative)

    return documents
