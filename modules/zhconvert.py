# -*- coding: utf-8 -*-
"""简繁转换工具。

把 EPUB 全书在简体与繁体之间转换，三块范围分别开关：

* **正文**——spine 正文文档的可见文字
* **目录与导航**——NCX 的章节标题与书名、EPUB3 导航文档、书内目录页
* **元数据**——``dc:*`` 字段，并可把 ``dc:language`` 同步为 ``zh-Hant`` / ``zh-Hans``

选中 EPUB 后会自动检测原书字形并预选转换方向，避免转错方向导致「转了个寂寞」。
因为转换质量需要逐处核对，本工具提供 ``/api/preview``：并排列出「转换前 → 转换后」
的对照，确认无误再导出。文档级与归档级读写分别复用 :mod:`modules.epub_convert`
与 :mod:`modules.epub_io`。
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from flask import Blueprint, request, send_file, url_for

from modules.epub_convert import (
    DEFAULT_DIRECTION,
    DIRECTIONS,
    ConvertOptions,
    convert_book,
    detect_direction,
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

zhconvert_bp = Blueprint("zhconvert", __name__)

TOOL = {
    "key": "zhconvert",
    "title": "简繁转换",
    "url_prefix": "/zhconvert",
    "order": 80,
    "description": "把 EPUB 在简体与繁体之间转换，可选覆盖正文、目录导航与元数据。",
}

DIRECTION_OPTIONS = tuple(Option(item.value, item.label) for item in DIRECTIONS)

PAGE = PageConfig(
    key="zhconvert",
    title="简繁转换",
    subtitle="在简体与繁体之间转换全书文本；正文、目录导航、元数据可分别开关。",
    endpoint="/zhconvert/api/run",
    load_endpoint="/zhconvert/api/load",
    load_label="重新检测",
    preview_endpoint="/zhconvert/api/preview",
    preview_label="预览对照",
    upload_label="EPUB 文件",
    upload_accept=".epub,application/epub+zip",
    upload_hint="选择 EPUB 后会自动检测原书字形并预选方向。最大 50MB。",
    fields_title="转换设置",
    fields_hint="简繁转换不是逐字替换，建议先点「预览对照」逐处核对，确认无误再导出。",
    fields=(
        Field(
            "direction",
            "转换方向",
            type="select",
            options=DIRECTION_OPTIONS,
            default=DEFAULT_DIRECTION,
            hint=(
                "「通用」用「爲 / 裏」这类旧字形；想要现代「為」请选台湾正体或香港繁体"
                "（台湾用「裡」、香港用「裏」）。「含台湾用词」还会把"
                "「软件 / 鼠标 / 网络」换成「軟體 / 滑鼠 / 網路」。"
            ),
        ),
        Field(
            "scope_body",
            "正文",
            type="checkbox",
            default=True,
            hint="按 spine 定位正文文档，只改可见文字，不动标签、属性与 id / class / href",
        ),
        Field(
            "scope_navigation",
            "目录与导航",
            type="checkbox",
            default=True,
            hint="NCX 的章节标题与书名、EPUB3 导航文档、书内目录页",
        ),
        Field(
            "scope_metadata",
            "元数据",
            type="checkbox",
            default=True,
            hint="书名、作者、简介、主题标签、出版社等 dc:* 字段",
        ),
        Field(
            "update_language",
            "同步语言标记",
            type="checkbox",
            default=True,
            hint="把 dc:language 改为 zh-Hant / zh-Hans；语言不是中文的书籍不受影响",
        ),
    ),
    result_kind="download",
    result_hint="导出后会自动下载转换好的 EPUB，同时在上方给出下载链接。",
)

# EpubConvertError 继承自 EpubIOError，注册一次即可覆盖两者。
install_error_handlers(zhconvert_bp, extra_exceptions=(EpubIOError,))

_SCRIPT_SUFFIX = {"traditional": "繁体", "simplified": "简体"}


def _read_epub_upload():
    upload = read_upload("file", {".epub"}, label="EPUB 文件")
    if not is_epub_bytes(upload.data):
        raise ToolkitError(INVALID_EPUB_MESSAGE)
    check_zip_bomb(upload.data)
    return upload


def _bool_field(name):
    return (request.form.get(name) or "").lower() in {"1", "true", "yes", "on"}


def _options_from_form():
    return ConvertOptions(
        direction=request.form.get("direction") or DEFAULT_DIRECTION,
        scope_body=_bool_field("scope_body"),
        scope_navigation=_bool_field("scope_navigation"),
        scope_metadata=_bool_field("scope_metadata"),
        update_language=_bool_field("update_language"),
    )


@zhconvert_bp.route("/")
def index():
    return render_tool_page(
        replace(
            PAGE,
            endpoint=url_for("zhconvert.api_run"),
            load_endpoint=url_for("zhconvert.api_load"),
            preview_endpoint=url_for("zhconvert.api_preview"),
        )
    )


@zhconvert_bp.post("/api/load")
def api_load():
    """检测原书字形，供前端预选转换方向。"""
    upload = _read_epub_upload()

    with TemporaryDirectory() as temp_dir:
        book = extract_epub(upload.data, Path(temp_dir) / "book")
        direction, summary = detect_direction(book)

    # 判断不出来时不返回 direction，避免把用户的选项覆盖成空值。
    values = {"direction": direction} if direction else {}
    return ok(values=values, summary=summary, direction=direction)


@zhconvert_bp.post("/api/preview")
def api_preview():
    """试跑转换，返回逐处对照，不写盘、不产出文件。"""
    upload = _read_epub_upload()
    options = _options_from_form()

    with TemporaryDirectory() as temp_dir:
        book = extract_epub(upload.data, Path(temp_dir) / "book")
        stats, preview = convert_book(book, options, write=False)

    return ok(
        html=render_preview(stats, preview),
        summary=stats_text(stats),
        stats=stats.as_dict(),
    )


@zhconvert_bp.post("/api/run")
def api_run():
    """真正转换并导出新的 EPUB。"""
    upload = _read_epub_upload()
    options = _options_from_form()

    with TemporaryDirectory() as temp_dir:
        book = extract_epub(upload.data, Path(temp_dir) / "book")
        stats, preview = convert_book(book, options, write=True)
        payload = write_epub(book)

    suffix = _SCRIPT_SUFFIX.get(preview.direction.target, "转换")
    download_name = safe_filename(f"{upload.stem}_{suffix}.epub", f"{upload.stem}.epub")
    response = send_file(
        payload,
        mimetype=EPUB_MIMETYPE,
        as_attachment=True,
        download_name=download_name,
    )
    return attach_message(response, f"处理完成，{stats_text(stats)}")
