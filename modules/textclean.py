# -*- coding: utf-8 -*-
"""文本清洗工具。

按 OPF 的 spine 定位正文文档并清洗，**封面 / 目录 / 导航文档一律不动**：

* 空白与空段落规范
* 合并被硬换行拆断的段落
* 清理冗余内联标签（``<font>``、内联 ``style``、无意义 ``<span>``、空内联标签）
* 自定义查找替换（纯文本或正则，只作用于文本节点）

页面由 :mod:`modules.toolkit` 的通用框架渲染。因为清洗是有破坏性的操作，
本工具提供 ``/api/preview`` 接口，配合框架的「预览效果」按钮先看效果再导出。
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
from modules.epub_text import (
    CleanOptions,
    clean_book,
    parse_replacements,
    render_preview,
    stats_text,
)
from modules.toolkit import (
    Field,
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

textclean_bp = Blueprint("textclean", __name__)

TOOL = {
    "key": "textclean",
    "title": "文本清洗",
    "url_prefix": "/textclean",
    "order": 50,
    "description": "按 spine 清洗 EPUB 正文：规范空白、合并断行、清理冗余内联标签、自定义查找替换。",
}

PAGE = PageConfig(
    key="textclean",
    title="文本清洗",
    subtitle="清洗 EPUB 正文文本；只处理 spine 正文，封面、目录与导航文档不受影响。",
    endpoint="/textclean/api/run",
    preview_endpoint="/textclean/api/preview",
    preview_label="预览效果",
    upload_label="EPUB 文件",
    upload_accept=".epub,application/epub+zip",
    upload_hint="选择 EPUB 后可直接预览清洗效果，确认无误再导出。最大 50MB。",
    fields_title="清洗项",
    fields_hint="建议先点「预览效果」查看改动统计与正文效果，确认后再「导出 EPUB」。",
    fields=(
        Field(
            "normalize_whitespace",
            "空白与空段落规范",
            type="checkbox",
            default=True,
            hint="正文内的软换行按「中文紧贴、西文留一格」合并；同时删除纯空的段落。块与块之间的排版空白不动",
        ),
        Field(
            "merge_broken_paragraphs",
            "合并硬换行拆断的段落",
            type="checkbox",
            default=True,
            hint="相邻段落边界不像句末时合并；带 title 类名或注释类型的段落不会被合并",
        ),
        Field(
            "strip_inline",
            "清理冗余内联标签",
            type="checkbox",
            default=True,
            hint="去除 <font>、内联 style、无属性 <span>，并删除空的内联标签",
        ),
        Field(
            "replace_text",
            "自定义查找替换",
            type="textarea",
            rows=5,
            placeholder="查找 => 替换\n每行一条；省略 => 表示删除匹配到的内容",
            hint="规则只作用于正文文本节点，不会破坏标签结构",
        ),
        Field(
            "regex_replacements",
            "替换规则按正则解析",
            type="checkbox",
            default=False,
            hint="勾选后「查找」按正则处理，替换内容可用 \\1 反向引用",
        ),
    ),
    result_kind="download",
    result_hint="导出后会自动下载清洗好的 EPUB，同时在上方给出下载链接。",
)

# EpubTextError 继承自 EpubIOError，注册一次即可覆盖两者。
install_error_handlers(textclean_bp, extra_exceptions=(EpubIOError,))


def _read_epub_upload():
    upload = read_upload("file", {".epub"}, label="EPUB 文件")
    if not is_epub_bytes(upload.data):
        raise ToolkitError(INVALID_EPUB_MESSAGE)
    check_zip_bomb(upload.data)
    return upload


def _bool_field(name):
    return (request.form.get(name) or "").lower() in {"1", "true", "yes", "on"}


def _options_from_form():
    return CleanOptions(
        normalize_whitespace=_bool_field("normalize_whitespace"),
        remove_empty_paragraphs=_bool_field("normalize_whitespace"),
        merge_broken_paragraphs=_bool_field("merge_broken_paragraphs"),
        strip_inline=_bool_field("strip_inline"),
        replacements=parse_replacements(request.form.get("replace_text", "")),
        regex_replacements=_bool_field("regex_replacements"),
    )


@textclean_bp.route("/")
def index():
    return render_tool_page(
        replace(
            PAGE,
            endpoint=url_for("textclean.api_run"),
            preview_endpoint=url_for("textclean.api_preview"),
        )
    )


@textclean_bp.post("/api/preview")
def api_preview():
    """试跑清洗，返回预览 HTML 与统计，不写盘、不产出文件。"""
    upload = _read_epub_upload()
    options = _options_from_form()

    with TemporaryDirectory() as temp_dir:
        book = extract_epub(upload.data, Path(temp_dir) / "book")
        stats, samples = clean_book(book, options, write=False)

    return ok(
        html=render_preview(samples, stats),
        summary=stats_text(stats),
        stats=stats.as_dict(),
    )


@textclean_bp.post("/api/run")
def api_run():
    """真正清洗并导出新的 EPUB。"""
    upload = _read_epub_upload()
    options = _options_from_form()

    with TemporaryDirectory() as temp_dir:
        book = extract_epub(upload.data, Path(temp_dir) / "book")
        stats, _samples = clean_book(book, options, write=True)
        payload = write_epub(book)

    download_name = safe_filename(f"{upload.stem}_cleaned.epub", f"{upload.stem}.epub")
    response = send_file(
        payload,
        mimetype=EPUB_MIMETYPE,
        as_attachment=True,
        download_name=download_name,
    )
    return attach_message(response, f"处理完成，{stats_text(stats)}。")
