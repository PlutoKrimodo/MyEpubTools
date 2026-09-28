# -*- coding: utf-8 -*-
"""元数据编辑工具。

上传 EPUB 后自动读取现有 ``dc:*`` 元数据并填入表单，修改后导出为新的 EPUB：

* 托管字段：``title`` / ``creator`` / ``language`` / ``identifier`` /
  ``publisher`` / ``date`` / ``description`` / ``subject``
* 留空的字段会**删除**对应元素
* 其余未托管的 OPF 内容（``<meta>``、注释、``dc:contributor`` 等）逐字节保留
* 书名发生变化时，同步更新 NCX 的 ``docTitle`` 与 EPUB3 nav 文档标题

页面由 :mod:`modules.toolkit` 的通用框架渲染，文档级读写全部复用
:mod:`modules.epub_package`，归档级读写复用 :mod:`modules.epub_io`。
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from flask import Blueprint, request, send_file, url_for

from modules.epub_io import (
    EPUB_MIMETYPE,
    INVALID_EPUB_MESSAGE,
    EpubIOError,
    extract_epub,
    is_epub_bytes,
    write_epub,
)
from modules.epub_package import (
    FIELD_KEYS,
    read_metadata,
    sync_titles,
    write_metadata,
)
from modules.toolkit import (
    Field,
    Option,
    PageConfig,
    ToolkitError,
    attach_message,
    check_zip_bomb,
    install_error_handlers,
    ok,
    read_upload,
    render_tool_page,
    safe_filename,
)

metadata_bp = Blueprint("metadata", __name__)

TOOL = {
    "key": "metadata",
    "title": "元数据编辑",
    "url_prefix": "/metadata",
    "order": 40,
    "description": "读取并修改 EPUB 的书名、作者、语言、标识符与出版信息，另存为新文件。",
}

EMPTY_TITLE_MESSAGE = "书名不能为空"

LANGUAGE_OPTIONS = (
    Option("zh-Hans", "中文（简体）"),
    Option("zh-Hant", "中文（繁体）"),
    Option("en", "英语"),
    Option("ja", "日语"),
    Option("ko", "韩语"),
    Option("fr", "法语"),
    Option("de", "德语"),
    Option("es", "西班牙语"),
    Option("ru", "俄语"),
    Option("pt", "葡萄牙语"),
    Option("it", "意大利语"),
)

PAGE = PageConfig(
    key="metadata",
    title="元数据编辑",
    subtitle="读取 EPUB 现有元数据，修改后导出为新的 EPUB 文件。",
    endpoint="/metadata/api/run",
    load_endpoint="/metadata/api/load",
    load_label="重新读取",
    upload_label="EPUB 文件",
    upload_accept=".epub,application/epub+zip",
    upload_hint="选择 EPUB 后会自动读取现有元数据并填入下表。最大 50MB。",
    fields_title="元数据",
    fields_hint=(
        "留空表示删除该项元数据；表中未列出的内容（封面标记、注释、贡献者等）原样保留。"
    ),
    fields=(
        Field("title", "书名", type="text", required=True, placeholder="dc:title"),
        Field(
            "creator",
            "作者",
            type="text",
            hint="多位作者用 / 分隔（dc:creator，保留 opf:role 属性）",
        ),
        Field("language", "语言", type="select", options=LANGUAGE_OPTIONS, hint="dc:language"),
        Field(
            "identifier",
            "标识符",
            type="text",
            hint="默认沿用原值，可手动修改（dc:identifier，保留其 id 与 unique-identifier 关联）",
        ),
        Field("publisher", "出版社", type="text", hint="dc:publisher"),
        Field("date", "出版日期", type="text", placeholder="YYYY-MM-DD", hint="dc:date"),
        Field("description", "简介", type="textarea", rows=5, hint="dc:description"),
        Field("subject", "主题标签", type="textarea", rows=4, hint="每行一个（dc:subject）"),
    ),
    result_kind="download",
    result_hint="处理完成后会直接下载新的 EPUB，同时在上方给出下载链接。",
)

# EpubPackageError 继承自 EpubIOError，注册一次即可覆盖两者。
install_error_handlers(metadata_bp, extra_exceptions=(EpubIOError,))


def _read_epub_upload():
    """读取并校验 EPUB 上传，返回 :class:`modules.toolkit.Upload`。"""
    upload = read_upload("file", {".epub"}, label="EPUB 文件")
    if not is_epub_bytes(upload.data):
        raise ToolkitError(INVALID_EPUB_MESSAGE)
    check_zip_bomb(upload.data)
    return upload


def _form_values():
    return {key: (request.form.get(key) or "").strip() for key in FIELD_KEYS}


def _status_text(opf_path, opf_changed, synced):
    if not opf_changed and not synced:
        return "处理完成，元数据没有变化，已原样导出。"
    pieces = [f"已更新 {opf_path}" if opf_changed else f"{opf_path} 没有变化"]
    if synced:
        pieces.append("目录标题已同步至 " + "、".join(synced))
    return "处理完成，" + "；".join(pieces) + "。"


@metadata_bp.route("/")
def index():
    return render_tool_page(
        replace(
            PAGE,
            endpoint=url_for("metadata.api_run"),
            load_endpoint=url_for("metadata.api_load"),
        )
    )


@metadata_bp.post("/api/load")
def api_load():
    """读取现有元数据，供前端预填表单。"""
    upload = _read_epub_upload()

    with TemporaryDirectory() as temp_dir:
        book = extract_epub(upload.data, Path(temp_dir) / "book")
        snapshot = read_metadata(book)

    values = {key: snapshot.values.get(key, "") for key in FIELD_KEYS}
    if snapshot.filled:
        summary = f"已从 {snapshot.opf_path} 读取 {snapshot.filled} 项元数据，可修改后提交。"
    else:
        summary = f"{snapshot.opf_path} 中没有可识别的元数据，可直接填写后提交。"

    return ok(values=values, summary=summary, opf_path=snapshot.opf_path)


@metadata_bp.post("/api/run")
def api_run():
    """应用修改，导出新的 EPUB。"""
    upload = _read_epub_upload()
    values = _form_values()
    if not values["title"]:
        raise ToolkitError(EMPTY_TITLE_MESSAGE)

    with TemporaryDirectory() as temp_dir:
        book = extract_epub(upload.data, Path(temp_dir) / "book")

        before = read_metadata(book)
        opf_path, opf_changed = write_metadata(book, values)

        # 只在书名真的发生变化时才动目录，避免覆盖书籍原本刻意设置的目录标题。
        synced = []
        if values["title"] != (before.values.get("title") or "").strip():
            synced = sync_titles(book, values["title"])

        payload = write_epub(book)

    download_name = safe_filename(f"{values['title']}.epub", f"{upload.stem}.epub")
    response = send_file(
        payload,
        mimetype=EPUB_MIMETYPE,
        as_attachment=True,
        download_name=download_name,
    )
    return attach_message(response, _status_text(opf_path, opf_changed, synced))
