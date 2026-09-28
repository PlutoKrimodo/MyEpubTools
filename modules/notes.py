from flask import Blueprint, request, jsonify, render_template, send_file
from bs4 import BeautifulSoup, NavigableString
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import unquote, urlsplit
import posixpath
import re

from modules.epub_io import (
    EpubIOError,
    INVALID_EPUB_MESSAGE,
    decode_text,
    extract_epub,
    is_epub_bytes,
    read_xhtml_documents,
    write_epub,
)
from modules.epub_package import spine_documents

notes_bp = Blueprint("notes", __name__, template_folder=str(Path(__file__).resolve().parent.parent / "templates" / "notes"))

TOOL = {
    "key": "notes",
    "title": "注释处理",
    "url_prefix": "/notes",
    "order": 20,
    "description": "识别并内联或清理 EPUB 中的脚注、尾注与跨文件注释链接。",
}

NOTE_ID_RE = re.compile(
    r"^(?:fn|fnote|footnote|endnote|note|notef|enote|end-notes?)[_-]?(\d+)$",
    re.I,
)
NOTE_CLASS_RE = re.compile(
    r"(?:footnote|footnotes|endnote|endnotes|fnote|fnote\d*|note-list|notes?)",
    re.I,
)
NOTE_ROLE_VALUES = {
    "doc-footnote", "doc-endnote", "doc-noteref", "doc-notes",
    "footnote", "endnote"
}


def error_response(message, status=400):
    return jsonify({"success": False, "error": str(message)}), status


def parse_html(source):
    return BeautifulSoup(source, "html.parser")


def text_of(node):
    if not node:
        return ""
    s = BeautifulSoup(str(node), "html.parser")
    for x in s.find_all(["script", "style"]):
        x.decompose()
    return re.sub(r"\s+", " ", s.get_text(" ", strip=True)).strip()


def normalize_fragment(href):
    if not href or "#" not in href:
        return None
    fragment = unquote(href.split("#", 1)[1]).strip()
    return fragment or None


def normalize_path(path):
    return posixpath.normpath(path.replace("\\", "/")).lstrip("./")


def resolve_href(source_path, href):
    if not href:
        return None, None
    href = unquote(href.strip())
    parts = urlsplit(href)
    fragment = parts.fragment or None
    target = parts.path
    if not target:
        target = source_path
    else:
        target = normalize_path(posixpath.join(posixpath.dirname(source_path), target))
    return target, fragment


def id_is_note(identifier):
    return bool(identifier and NOTE_ID_RE.match(identifier.strip()))


def class_tokens(tag):
    return {str(x).lower() for x in (tag.get("class") or [])}


def is_note_semantic(tag):
    if not getattr(tag, "attrs", None):
        return False
    ident = tag.get("id", "")
    if id_is_note(ident):
        return True
    if any(NOTE_CLASS_RE.search(x) for x in class_tokens(tag)):
        return True
    epub_type = str(tag.get("epub:type", "")).lower().split()
    role = str(tag.get("role", "")).lower().split()
    if set(epub_type) & {"footnote", "endnote", "rearnote"}:
        return True
    if set(role) & NOTE_ROLE_VALUES:
        return True
    return False


# 明确的「注释列表容器」类名：只认复数 / 列表语义，
# 避免把正文里单个 class="note"、"footnote" 的普通块当成容器整块删除。
NOTE_CONTAINER_CLASSES = {
    "footnotes", "endnotes", "rearnotes", "note-list", "notelist", "notes",
}
# id / class 里的容器关键词：必须是独立词（可带连接符），"notebook"、"keynote" 不算。
NOTE_CONTAINER_ID_RE = re.compile(r"(?:^|[-_])(?:foot|end)?notes?(?:$|[-_])", re.I)


def container_has_note_items(tag):
    """容器内是否真的含注释条目（注释 id 或 footnote/endnote 语义）。"""
    for child in tag.find_all(True):
        ident = child.get("id")
        if ident and id_is_note(ident):
            return True
        if str(child.get("epub:type", "")).lower() in {"footnote", "endnote", "rearnote"}:
            return True
    return False


def is_note_container(tag):
    """判断是否是「注释列表容器」（装注释条目的外壳），区别于单个注释条目。

    收紧点（相对旧实现）：
      1. class 只认复数 / 列表语义，不再用 NOTE_CLASS_RE 匹配单个 note/footnote；
      2. id 必须是 footnotes / endnotes / notes 这类独立词，避免 notebook 之类误判；
      3. 宽松匹配（id / class 像注释列表）还必须真的内含注释条目，否则不算容器，
         防止把正文里的普通块整体删掉。
    """
    if not tag or not getattr(tag, "attrs", None):
        return False

    classes = class_tokens(tag)
    ident = str(tag.get("id", "")).strip().lower()
    epub_type = str(tag.get("epub:type", "")).lower().split()
    role = str(tag.get("role", "")).lower().split()

    # 明确语义：命中即认定为容器，不必再检查内容。
    if classes & NOTE_CONTAINER_CLASSES:
        return True
    if set(epub_type) & {"footnotes", "endnotes", "rearnotes"}:
        return True
    if set(role) & {"doc-endnotes", "doc-footnotes", "doc-notes"}:
        return True

    # 单个注释条目本身不是容器（如 id="fn1"）。
    if id_is_note(ident):
        return False

    # 宽松匹配：id / class 看起来像注释列表，且内部确实装着注释条目才算。
    looks_like_container = bool(NOTE_CONTAINER_ID_RE.search(ident)) or any(
        NOTE_CONTAINER_ID_RE.search(c) for c in classes
    )
    if looks_like_container and container_has_note_items(tag):
        return True
    return False


def make_note_record(path, node, note_id):
    content_node = node
    if node.name == "a" and node.parent and node.parent.name in {"p", "li", "div", "section", "aside"}:
        content_node = node.parent
    return {
        "path": path,
        "id": note_id,
        "node": node,
        "text": text_of(content_node),
        "container": content_node,
    }


def collect_notes(path, soup):
    records = {}
    for tag in soup.find_all(True):
        ident = tag.get("id")
        if ident and (id_is_note(ident) or is_note_semantic(tag)):
            if id_is_note(ident):
                records[(path, ident)] = make_note_record(path, tag, ident)
            elif tag.get("epub:type") in {"footnote", "endnote"}:
                records[(path, ident)] = make_note_record(path, tag, ident)

    for anchor in soup.find_all("a", id=True):
        ident = anchor.get("id")
        if not id_is_note(ident):
            continue
        container = anchor.find_parent(
            lambda x: x.name in {"li", "p", "div", "section", "aside"} and is_note_semantic(x)
        )
        node = container or anchor
        records[(path, ident)] = make_note_record(path, node, ident)

    for container in soup.find_all(True):
        if not is_note_container(container):
            continue
        for tag in container.find_all(True, id=True):
            ident = tag.get("id")
            if id_is_note(ident):
                records[(path, ident)] = make_note_record(path, tag, ident)

    return records


def note_index_for_docs(docs):
    index = {}
    for path, soup in docs.items():
        index.update(collect_notes(path, soup))
    return index


def looks_like_noteref(tag):
    """判断 ``<a>`` 是否像「正文注释引用标记」。

    只认**强信号**：fragment 是注释式 id、带注释语义属性、或有 noteref 类名。
    形如 ``#3`` 的纯数字锚点不再直接算——章节 / 图表交叉引用同样常用纯数字
    锚点，仅凭它删除会误伤正文；纯数字编号改由调用方用 :func:`resolve_note`
    确认确实能对应到注释条目后再删。
    """
    if not tag or tag.name != "a":
        return False
    href = tag.get("href")
    if not href:
        return False
    _target_path, fragment = resolve_href("", href)
    if id_is_note(fragment):
        return True
    if set(str(tag.get("epub:type", "")).lower().split()) & {"noteref", "footnote", "endnote"}:
        return True
    if set(str(tag.get("role", "")).lower().split()) & {"doc-noteref", "noteref"}:
        return True
    if any("noteref" in c or "footnote" in c or "endnote" in c for c in class_tokens(tag)):
        return True
    # 带前缀的编号（n1 / fn_1 / note-1 / footnote1 …）仍是强信号。
    return bool(fragment and re.match(r"^(?:n|note|fn|footnote|endnote|noteref)[_-]?\d+$", fragment, re.I))


def reference_number_from_tag(tag):
    href = tag.get("href", "")
    fragment = normalize_fragment(href)
    if fragment:
        m = re.search(r"(\d+)$", fragment)
        if m:
            return m.group(1)
    m = re.search(r"(\d+)", text_of(tag))
    return m.group(1) if m else None


def candidate_note_keys(source_path, href):
    target_path, fragment = resolve_href(source_path, href)
    keys = []
    if target_path and fragment:
        keys.append((target_path, fragment))
    if fragment:
        keys.append((source_path, fragment))
    return keys


def resolve_note(source_path, anchor, note_index):
    href = anchor.get("href", "")
    for key in candidate_note_keys(source_path, href):
        if key in note_index:
            return note_index[key]

    fragment = normalize_fragment(href)
    number = reference_number_from_tag(anchor)

    if number:
        target_path, _ = resolve_href(source_path, href)
        target_path = target_path or source_path
        aliases = [
            f"fn_{number}", f"fn-{number}", f"fn{number}",
            f"notef{number}", f"notef_{number}", f"notef-{number}",
            f"footnote{number}", f"footnote_{number}", f"footnote-{number}",
            f"endnote{number}", f"endnote_{number}", f"endnote-{number}",
            f"note{number}", f"note_{number}", f"note-{number}",
            f"n{number}", f"n_{number}", f"n-{number}",
        ]
        for ident in aliases:
            if (target_path, ident) in note_index:
                return note_index[(target_path, ident)]
            if (source_path, ident) in note_index:
                return note_index[(source_path, ident)]

    return None


def unwrap_reference_container(anchor):
    parent = anchor.parent
    if parent and parent.name == "sup":
        return parent
    return anchor


def remove_node(node):
    if node and getattr(node, "parent", None):
        node.decompose()


def is_destroyed(node):
    """判断节点是否已被先前的删除操作连带销毁。

    bs4 销毁节点时会清空其属性表，因此属性表为空即表示该节点已不可用，
    此时对它做任何属性访问都会抛 AttributeError。
    """
    return getattr(node, "attrs", None) is None


def inline_reference(anchor, note_text):
    container = unwrap_reference_container(anchor)
    new_text = NavigableString("（" + note_text + "）")
    container.insert_after(new_text)


def remove_reference(anchor):
    remove_node(unwrap_reference_container(anchor))


def remove_note_structures(soup):
    """删除文末的注释容器与注释条目。

    只负责「文末注释」：正文里的引用标记（<a> 链接）不在这里删，
    否则只勾选「删除文末注释」时，会把正文的 [1] 一起误删。
    """
    containers = []
    for tag in soup.find_all(True):
        if is_note_container(tag):
            containers.append(tag)
    selected = []
    for node in containers:
        if not any(parent in containers for parent in node.parents):
            selected.append(node)
    for node in selected:
        remove_node(node)

    for tag in list(soup.find_all(True)):
        if is_destroyed(tag):
            # 已被前面删除的父节点连带销毁
            continue
        ident = tag.get("id")
        if ident and id_is_note(ident):
            # 带 href 的 <a> 属于正文里的引用链接，归 remove_marker 管；
            # 这里只删文末的注释定义块，避免未勾选删除标识时误删正文。
            if tag.name == "a" and tag.get("href"):
                continue
            remove_node(tag)

    # 注意：不再删除 a[epub:type=noteref]——删除正文标识是 remove_marker 的职责。

    for hr in list(soup.find_all("hr")):
        attrs = " ".join(
            [str(hr.get("id", ""))] + list(hr.get("class") or [])
        ).lower()
        if any(x in attrs for x in ("footnote", "endnote", "fnote", "notes")):
            remove_node(hr)


def process_document(path, soup, actions, note_index, stats):
    # 内联：只处理能解析到注释正文的引用标记，把注释文本插到标记之后。
    if "inline" in actions:
        for anchor in list(soup.find_all("a", href=True)):
            if is_destroyed(anchor):
                continue
            note = resolve_note(path, anchor, note_index)
            if not note:
                continue
            note_text = note["text"]
            if note_text:
                inline_reference(anchor, note_text)
                stats["inlined"] += 1

    # 删除正文标识：强信号（注释式 id / 注释语义属性 / noteref 类名）直接删；
    # 纯数字锚点等弱信号必须先能解析到注释条目，避免误删正文交叉引用。
    if "remove_marker" in actions:
        for anchor in list(soup.find_all("a", href=True)):
            if is_destroyed(anchor):
                continue
            if not (looks_like_noteref(anchor) or resolve_note(path, anchor, note_index)):
                continue
            remove_reference(anchor)
            stats["markers_removed"] += 1

    if "remove_note" in actions:
        remove_note_structures(soup)

    return soup


def detect_style(text):
    patterns = [
        ("方括号", re.compile(r"\[\s*\d{1,4}\s*\]")),
        ("圆括号", re.compile(r"\(\s*\d{1,4}\s*\)")),
        ("花括号", re.compile(r"\{\s*\d{1,4}\s*\}")),
        ("纯数字", re.compile(r"(?<![\w\]])\d{1,4}(?![\w\[])(?!\s*(?:年|月|日|页|章|节))")),
    ]
    counts = {name: len(rx.findall(text)) for name, rx in patterns}
    best = max(counts, key=counts.get)
    return {"style": best if counts[best] else "其他", "counts": counts}


def process_single_html(source, actions, style):
    path = "sample.xhtml"
    soup = parse_html(source)
    docs = {path: soup}
    index = note_index_for_docs(docs)
    stats = {"inlined": 0, "references_removed": 0, "markers_removed": 0, "notes_found": len(index)}
    process_document(path, soup, actions, index, stats)
    return str(soup), stats


def process_epub(epub_bytes, actions, style):
    with TemporaryDirectory() as td:
        book = extract_epub(epub_bytes, Path(td) / "book")
        docs = {
            rel: parse_html(text)
            for rel, text in read_xhtml_documents(book).items()
        }

        # 索引基于**全部**文档：注释定义可能不在 spine 正文里，索引完整时
        # 「内联」与跨文件引用才能解析到注释文本。
        index = note_index_for_docs(docs)

        # 写回只针对 spine 正文，封面 / 目录 / 导航文档一律不动（与 textclean 口径一致）。
        # 读不到 spine 的非常规结构退回「处理全部文档」，避免整本不处理。
        try:
            targets = spine_documents(book)
        except EpubIOError:
            targets = []
        allowed = {normalize_path(rel) for rel in (targets or docs)}

        stats = {
            "files": len(allowed),
            "notes_found": len(index),
            "inlined": 0,
            "references_removed": 0,
            "markers_removed": 0,
        }

        for rel, soup in docs.items():
            if normalize_path(rel) not in allowed:
                continue
            process_document(rel, soup, actions, index, stats)
            (book / Path(rel)).write_text(str(soup), encoding="utf-8")

        output = write_epub(book)

    return output, stats


@notes_bp.route("/")
def index():
    return render_template("index.html")


@notes_bp.post("/api/detect")
def api_detect():
    try:
        if request.files.get("file"):
            source = decode_text(request.files["file"].read())
        else:
            source = request.form.get("html", "")
        if not source.strip():
            return error_response("没有提供 HTML 样本")
        text = parse_html(source).get_text(" ", strip=True)
        return jsonify({"success": True, **detect_style(text)})
    except Exception as exc:
        return error_response(exc, 500)


@notes_bp.post("/api/preview")
def api_preview():
    try:
        source = request.form.get("html", "")
        actions = request.form.getlist("actions")
        style = request.form.get("style", "方括号")
        if not source.strip():
            return error_response("没有提供 HTML")
        valid_actions = {"remove_marker", "inline", "remove_note"}
        actions = [a for a in actions if a in valid_actions]
        if not actions:
            return error_response("请至少选择一项处理功能")
        result, stats = process_single_html(source, actions, style)
        return jsonify({"success": True, "html": result, "stats": stats})
    except Exception as exc:
        return error_response(exc, 500)


@notes_bp.post("/api/process")
def api_process():
    try:
        f = request.files.get("epub")
        if not f or not f.filename:
            return error_response("请上传 EPUB 文件")
        if not f.filename.lower().endswith(".epub"):
            return error_response("只支持 .epub 文件")
        data = f.read()
        if not is_epub_bytes(data):
            return error_response(INVALID_EPUB_MESSAGE)

        actions = request.form.getlist("actions")
        style = request.form.get("style", "方括号")
        valid_actions = {"remove_marker", "inline", "remove_note"}
        actions = [a for a in actions if a in valid_actions]
        if not actions:
            return error_response("请至少选择一项处理功能")

        output, stats = process_epub(data, actions, style)
        response = send_file(
            output,
            mimetype="application/epub+zip",
            as_attachment=True,
            download_name="processed.epub",
        )
        # 统计经响应头回传（下载流本身无法携带 JSON），供前端展示处理结果。
        response.headers["X-Notes-Found"] = str(stats.get("notes_found", 0))
        response.headers["X-Notes-Inlined"] = str(stats.get("inlined", 0))
        response.headers["X-Notes-Removed"] = str(stats.get("markers_removed", 0))
        return response
    except Exception as exc:
        return error_response(exc, 500)


@notes_bp.errorhandler(413)
def too_large(_):
    return error_response("文件超过 50MB 限制", 413)


@notes_bp.errorhandler(Exception)
def unhandled(exc):
    return error_response(exc, 500)
