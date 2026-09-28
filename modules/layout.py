# -*- coding: utf-8 -*-
"""样式排版工具。

三类互不干扰的能力：

* **安全清理样式表**——删除空规则块、重复的 ``@font-face``、选择器与声明块都重复的规则。
  刻意不做「删除未被引用的规则」和「合并多个样式表」。
* **清除空段**——删除 spine 正文里没有文字、也没有插图 / 表格的 ``<p>``。
  实现上**不另写一套规则**：把 :class:`~modules.epub_text.CleanOptions` 只打开
  ``remove_empty_paragraphs`` 一项、其余开关全部关闭，交由
  :func:`~modules.epub_text.clean_book` 处理，因此判定标准、保真策略与
  「文本清洗」工具逐字一致，不会出现两套实现各自演化的情况。
* **统一排版参数**——把行距 / 段首缩进 / 段间距 / 正文字号作为一段带标记的覆盖规则，
  追加到「最后一个被链接的样式表」末尾，保证在层叠中胜出；重复运行就地替换。

页面由 :mod:`modules.toolkit` 的通用框架渲染，沿用上一轮的「预览效果」能力。
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from flask import Blueprint, request, send_file, url_for

from modules.epub_css import (
    TypographyOptions,
    check_options,
    process_book,
    render_preview,
    stats_text,
)
from modules.epub_io import (
    EPUB_MIMETYPE,
    INVALID_EPUB_MESSAGE,
    EpubIOError,
    extract_epub,
    is_epub_bytes,
    write_epub,
)
from modules.epub_text import CleanOptions, clean_book
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

layout_bp = Blueprint("layout", __name__)

TOOL = {
    "key": "layout",
    "title": "样式排版",
    "url_prefix": "/layout",
    "order": 60,
    "description": "安全清理样式表与正文空段，并统一行距、缩进等排版参数。",
}

PAGE = PageConfig(
    key="layout",
    title="样式排版",
    subtitle="清理样式表里的冗余内容与正文空段，并按需统一行距、段首缩进、段间距与字号。",
    endpoint="/layout/api/run",
    preview_endpoint="/layout/api/preview",
    preview_label="预览效果",
    upload_label="EPUB 文件",
    upload_accept=".epub,application/epub+zip",
    upload_hint="选择 EPUB 后可直接预览将要改动的内容与排版效果，确认后再导出。最大 50MB。",
    fields_title="处理项",
    fields_hint=(
        "清理只做语义上零风险的删除：样式表层面不会删除「未被引用的规则」、也不会合并样式表；"
        "「清除空段」只作用于 spine 正文，封面与目录页不受影响。"
        "排版参数以覆盖规则写在样式表末尾，重复运行会就地替换。"
    ),
    fields=(
        Field(
            "clean_css",
            "安全清理样式表",
            type="checkbox",
            default=True,
            hint="删除空规则块、逐字重复的 @font-face、以及选择器与声明都重复的规则；注释与缩进原样保留",
        ),
        Field(
            "clear_empty_paragraphs",
            "清除空段",
            type="checkbox",
            default=True,
            hint="按 spine 删除正文里没有文字、也没有插图 / 表格的 <p>；只含 &nbsp; 或空白的段落同样算空段",
        ),
        Field(
            "apply_typography",
            "统一排版参数",
            type="checkbox",
            default=True,
            hint="勾选后才会写入下面的排版覆盖规则；取消勾选则不追加任何规则",
        ),
        Field(
            "line_height",
            "行距",
            type="text",
            default="1.6",
            placeholder="1.6",
            hint="写入 body 的 line-height；留空表示不改",
        ),
        Field(
            "text_indent",
            "段首缩进",
            type="text",
            default="2em",
            placeholder="2em",
            hint="写入 p 的 text-indent；留空表示不改",
        ),
        Field(
            "paragraph_spacing",
            "段间距",
            type="text",
            default="0.6em",
            placeholder="0.6em",
            hint="写入 p 的 margin-bottom；留空表示不改",
        ),
        Field(
            "font_size",
            "正文字号",
            type="text",
            default="",
            placeholder="留空表示不改，例如 1em",
            hint="写入 body 的 font-size；留空表示不改",
        ),
    ),
    result_kind="download",
    result_hint="导出后会自动下载处理好的 EPUB，同时在上方给出下载链接。",
)

install_error_handlers(layout_bp, extra_exceptions=(EpubIOError,))


def _read_epub_upload():
    upload = read_upload("file", {".epub"}, label="EPUB 文件")
    if not is_epub_bytes(upload.data):
        raise ToolkitError(INVALID_EPUB_MESSAGE)
    check_zip_bomb(upload.data)
    return upload


def _bool_field(name):
    return (request.form.get(name) or "").lower() in {"1", "true", "yes", "on"}


#: 「清除空段」的选项：只打开删空段这一项，其余清洗开关全部关闭。
#: 这样判定标准与文本清洗工具完全一致，不会出现两套实现各自演化的情况。
EMPTY_PARAGRAPH_OPTIONS = CleanOptions(
    normalize_whitespace=False,
    remove_empty_paragraphs=True,
    merge_broken_paragraphs=False,
    strip_inline=False,
)


def _options_from_form():
    typography = TypographyOptions(
        font_size=request.form.get("font_size", ""),
        line_height=request.form.get("line_height", ""),
        text_indent=request.form.get("text_indent", ""),
        paragraph_spacing=request.form.get("paragraph_spacing", ""),
    )
    if not _bool_field("apply_typography"):
        typography = TypographyOptions()
    # 先校验取值，避免处理到一半才因为非法字符失败
    return (
        _bool_field("clean_css"),
        check_options(typography),
        _bool_field("clear_empty_paragraphs"),
    )


def _clear_empty_paragraphs(book, enabled, write):
    """按 spine 删除正文空段；未勾选时返回 None。

    直接复用 :func:`modules.epub_text.clean_book`，所以「未改动的文档不写回」
    这条保真约定同样成立。
    """
    if not enabled:
        return None
    stats, _samples = clean_book(book, EMPTY_PARAGRAPH_OPTIONS, write=write)
    return stats


def _combined_summary(css_stats, text_stats):
    """合并「样式表」与「空段」两部分的统计文案。"""
    parts = []
    if any(
        (
            css_stats.empty_rules_removed,
            css_stats.duplicate_font_faces_removed,
            css_stats.duplicate_rules_removed,
            css_stats.override_applied,
        )
    ):
        parts.append(stats_text(css_stats).rstrip("。"))

    if text_stats is not None:
        if text_stats.empty_paragraphs_removed:
            parts.append(
                f"删除空段 {text_stats.empty_paragraphs_removed} 个"
                f"（涉及 {text_stats.changed_documents} 个正文文档）"
            )
        else:
            parts.append("没有发现空段")

    return "；".join(parts) + "。" if parts else "没有需要处理的内容。"


def _merged_stats(css_stats, text_stats):
    """把两部分统计合并成一个 dict，供接口返回。"""
    data = css_stats.as_dict()
    if text_stats is not None:
        data["text_documents"] = text_stats.documents
        data["empty_paragraphs_removed"] = text_stats.empty_paragraphs_removed
    return data


@layout_bp.route("/")
def index():
    return render_tool_page(
        replace(
            PAGE,
            endpoint=url_for("layout.api_run"),
            preview_endpoint=url_for("layout.api_preview"),
        )
    )


@layout_bp.post("/api/preview")
def api_preview():
    """试跑，返回预览 HTML 与统计，不写盘。"""
    upload = _read_epub_upload()
    clean, typography, clear_empty = _options_from_form()

    with TemporaryDirectory() as temp_dir:
        book = extract_epub(upload.data, Path(temp_dir) / "book")
        stats, samples, override_block = process_book(
            book, clean=clean, typography=typography, write=False
        )
        text_stats = _clear_empty_paragraphs(book, clear_empty, write=False)

    return ok(
        html=render_preview(stats, samples, override_block, typography, text_stats),
        summary=_combined_summary(stats, text_stats),
        stats=_merged_stats(stats, text_stats),
    )


@layout_bp.post("/api/run")
def api_run():
    """真正处理并导出新的 EPUB。"""
    upload = _read_epub_upload()
    clean, typography, clear_empty = _options_from_form()

    with TemporaryDirectory() as temp_dir:
        book = extract_epub(upload.data, Path(temp_dir) / "book")
        stats, _samples, _block = process_book(
            book, clean=clean, typography=typography, write=True
        )
        text_stats = _clear_empty_paragraphs(book, clear_empty, write=True)
        payload = write_epub(book)

    download_name = safe_filename(f"{upload.stem}_styled.epub", f"{upload.stem}.epub")
    response = send_file(
        payload,
        mimetype=EPUB_MIMETYPE,
        as_attachment=True,
        download_name=download_name,
    )
    return attach_message(response, f"处理完成，{_combined_summary(stats, text_stats)}")
