# -*- coding: utf-8 -*-
"""EPUB 拆分与合并工具。

一个页面、两种模式，共用 :mod:`modules.epub_assemble` 的组装逻辑：

* **拆分**：上传一本 EPUB，勾选其中若干正文章节 → 导出只含这些章节的新书
* **合并**：上传多本 EPUB（可调顺序）→ 合并成一本

两种模式都会：沿用封面、重建 OPF 与 NCX、重新生成目录页、递归收齐被引用到的
样式表 / 图片 / 字体，并把指向未收录内容的链接摘掉（文字保留）。
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from flask import Blueprint, request, send_file, url_for

from modules.epub_assemble import (
    Selection,
    SourceBook,
    assemble,
    book_metadata,
    document_titles,
    new_identifier,
)
from modules.epub_io import (
    EPUB_MIMETYPE,
    INVALID_EPUB_MESSAGE,
    EpubIOError,
    extract_epub,
    is_epub_bytes,
    write_epub,
)
from modules.epub_package import spine_documents
from modules.toolkit import (
    Field,
    Mode,
    PageConfig,
    ToolkitError,
    attach_message,
    check_zip_bomb,
    install_error_handlers,
    ok,
    render_tool_page,
    safe_filename,
)

splitmerge_bp = Blueprint("splitmerge", __name__)

TOOL = {
    "key": "splitmerge",
    "title": "拆分 / 合并",
    "url_prefix": "/splitmerge",
    "order": 70,
    "description": "从一本书里抽出选中章节另存为新书，或把多本书按顺序合并成一本。",
}

MODE_SPLIT = "split"
MODE_MERGE = "merge"
MODE_VALUES = (MODE_SPLIT, MODE_MERGE)

SPLIT_ONE_FILE_MESSAGE = "拆分模式一次只能上传一本 EPUB"
MERGE_NEED_TWO_MESSAGE = "合并模式至少需要两本 EPUB"
NO_SELECTION_MESSAGE = "请至少勾选一个章节"
EMPTY_TITLE_MESSAGE = "新书名不能为空"

PAGE = PageConfig(
    key="splitmerge",
    title="EPUB 拆分与合并",
    subtitle="从一本书里抽出选中章节另存为新书，或把多本按顺序合并成一本。",
    endpoint="/splitmerge/api/run",
    load_endpoint="/splitmerge/api/load",
    load_label="重新读取",
    modes=(
        Mode(
            value=MODE_SPLIT,
            label="拆分（抽章）",
            upload_label="EPUB 文件",
            accept=".epub,application/epub+zip",
            hint="上传一本 EPUB，选中后下方会列出章节供勾选。最大 50MB。",
            submit_label="导出选中章节",
            multiple=False,
        ),
        Mode(
            value=MODE_MERGE,
            label="合并（多本）",
            upload_label="EPUB 文件（可多选）",
            accept=".epub,application/epub+zip",
            hint="一次选中多本 EPUB，选中后可在下方调整顺序。最大 50MB / 本。",
            submit_label="合并为一本",
            multiple=True,
        ),
    ),
    upload_multiple=False,
    upload_order_hint="顺序即合并顺序，用 ↑ ↓ 调整（第一本的封面会成为合集封面）：",
    fields_title="新书信息",
    fields_hint="书名与作者默认为第一本的信息，建议改成合集名；标识符会自动生成新的 UUID。",
    fields=(
        Field(
            "documents",
            "章节选择",
            type="filelist",
            hint="仅拆分模式有效：勾选要抽出的章节（可用「全选 / 清空」）。",
        ),
        Field("title", "新书名", type="text", required=True, hint="导出文件名按此生成"),
        Field("author", "作者", type="text", hint="多位作者用 / 分隔"),
        Field("language", "语言", type="text", default="zh-Hans"),
    ),
    result_kind="download",
    result_hint="导出后会自动下载新的 EPUB，同时在上方给出下载链接。",
)

# AssembleError 继承自 EpubIOError，注册一次即可覆盖。
install_error_handlers(splitmerge_bp, extra_exceptions=(EpubIOError,))


# --------------------------------------------------------------------------- #
# 上传与表单
# --------------------------------------------------------------------------- #
def _read_uploads(name="file"):
    """读取并校验多份上传，返回 ``[(文件名, 字节), ...]``（保持前端给的顺序）。"""
    storages = [item for item in request.files.getlist(name) if item and item.filename]
    if not storages:
        raise ToolkitError("请选择 EPUB 文件")

    uploads = []
    for storage in storages:
        filename = Path(storage.filename).name
        if Path(filename).suffix.lower() != ".epub":
            raise ToolkitError(f"文件类型不支持：{filename}（仅支持 .epub）")
        data = storage.read()
        if not data:
            raise ToolkitError(f"文件为空：{filename}")
        if not is_epub_bytes(data):
            raise ToolkitError(f"{INVALID_EPUB_MESSAGE}：{filename}")
        check_zip_bomb(data)
        uploads.append((filename, data))
    return uploads


def _mode():
    mode = (request.form.get("mode") or MODE_SPLIT).strip()
    if mode not in MODE_VALUES:
        raise ToolkitError("未知的处理模式，请刷新页面后重试")
    return mode


def _meta_from_form(fallback_author=""):
    # 书名不回落：它已被预填成原书名，被清空就是用户想清空，直接报错比静默改回原值更好
    title = (request.form.get("title") or "").strip()
    if not title:
        raise ToolkitError(EMPTY_TITLE_MESSAGE)
    author = (request.form.get("author") or "").strip() or fallback_author.strip() or "未知"
    language = (request.form.get("language") or "").strip() or "zh-Hans"
    return {
        "title": title,
        "author": author,
        "language": language,
        "identifier": new_identifier(),
    }


# --------------------------------------------------------------------------- #
# 路由
# --------------------------------------------------------------------------- #
@splitmerge_bp.route("/")
def index():
    return render_tool_page(
        replace(
            PAGE,
            endpoint=url_for("splitmerge.api_run"),
            load_endpoint=url_for("splitmerge.api_load"),
        )
    )


@splitmerge_bp.post("/api/load")
def api_load():
    """读取源书信息：书名 / 作者 / 语言，拆分模式下再给出章节列表。"""
    mode = _mode()
    uploads = _read_uploads()

    with TemporaryDirectory() as temp_dir:
        source = extract_epub(uploads[0][1], Path(temp_dir) / "src")
        metadata = book_metadata(source)
        values = {
            "title": (metadata.get("title") or "").strip() or Path(uploads[0][0]).stem,
            "author": (metadata.get("creator") or "").strip() or "未知",
            "language": (metadata.get("language") or "").strip() or "zh-Hans",
            "documents": [],
        }

        summary = f"已读取《{values['title']}》。"
        if mode == MODE_SPLIT:
            if len(uploads) != 1:
                raise ToolkitError(SPLIT_ONE_FILE_MESSAGE)
            titles = document_titles(source)
            documents = [
                {"value": relative, "label": titles.get(relative) or Path(relative).stem}
                for relative in spine_documents(source)
            ]
            values["documents"] = documents
            summary += f"共 {len(documents)} 个正文章节，勾选后导出。"
        else:
            names = "、".join(Path(name).stem for name, _ in uploads[:3])
            summary = f"已选中 {len(uploads)} 本（{names}{'…' if len(uploads) > 3 else ''}），"
            summary += "第一本的书名与作者已填入下方。"

    return ok(values=values, summary=summary)


@splitmerge_bp.post("/api/run")
def api_run():
    mode = _mode()
    uploads = _read_uploads()

    if mode == MODE_SPLIT:
        if len(uploads) != 1:
            raise ToolkitError(SPLIT_ONE_FILE_MESSAGE)
    elif len(uploads) < 2:
        raise ToolkitError(MERGE_NEED_TWO_MESSAGE)

    with TemporaryDirectory() as temp_dir:
        roots = []
        for index, (name, data) in enumerate(uploads):
            roots.append(SourceBook(extract_epub(data, Path(temp_dir) / f"src{index}"), name))

        if mode == MODE_SPLIT:
            selected = [item for item in request.form.getlist("documents") if item.strip()]
            if not selected:
                raise ToolkitError(NO_SELECTION_MESSAGE)
            available = set(spine_documents(roots[0].root))
            unknown = [item for item in selected if item not in available]
            if unknown:
                raise ToolkitError(f"勾选的章节不在该书正文里：{unknown[0]}")
            titles = document_titles(roots[0].root)
            selections = [Selection(0, item, titles.get(item, "")) for item in selected]
        else:
            selections = []
            for index, source in enumerate(roots):
                for relative in spine_documents(source.root):
                    selections.append(Selection(index, relative, ""))

        first_metadata = book_metadata(roots[0].root)
        meta = _meta_from_form(first_metadata.get("creator") or "未知")

        destination = Path(temp_dir) / "out"
        report = assemble(roots, selections, destination, meta)
        payload = write_epub(destination)

    download_name = safe_filename(f"{meta['title']}.epub", f"{upload_name_stem(uploads[0][0])}.epub")
    response = send_file(
        payload,
        mimetype=EPUB_MIMETYPE,
        as_attachment=True,
        download_name=download_name,
    )
    action = "拆分" if mode == MODE_SPLIT else "合并"
    message = (
        f"{action}完成，收录 {report.documents} 章、{report.resources} 个资源"
        f"（来自 {report.sources} 本）"
    )
    if report.broken_links_removed:
        message += f"，摘除指向未收录内容的链接 {report.broken_links_removed} 处"
    return attach_message(response, message + "。")


def upload_name_stem(filename):
    return Path(filename).stem or "book"
