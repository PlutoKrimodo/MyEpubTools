# -*- coding: utf-8 -*-
"""EPUB 格式优化工具。

* **追加紧凑规则**——在现有样式表末尾追加紧凑化 CSS，保留原有排版，适合已有样式的书；
  若整本没有任何 CSS，则自动新建 ``epub-compact.css`` 并链接到正文、写入 OPF。
* **替换为模板样式**——用模板样式表覆盖并给裸 ``<p>`` / ``<h1>`` 注入正文样式类，
  适合无样式或样式混乱的书；模板缺省使用项目根目录的 ``sample_test.epub``。

两模式均可选删除 spine 正文空段（复用 ``clean_book``，与「文本清洗」「样式排版」
判定一致）以及删除 HTML 中的 ``<br>``。

页面由 :mod:`modules.toolkit` 的通用框架渲染；处理逻辑在公共层
:mod:`modules.epub_optimize`，归档读写复用 :mod:`modules.epub_io`。
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
from modules.epub_optimize import (
    MODE_APPEND,
    MODE_REPLACE,
    MODE_VALUES,
    extract_template_css_map,
    optimize_book,
    render_preview_html,
)
from modules.toolkit import (
    Field,
    Mode,
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

optimize_bp = Blueprint("optimize", __name__)

TOOL = {
    "key": "optimize",
    "title": "EPUB格式优化",
    "url_prefix": "/optimize",
    "order": 65,
    "description": "在现有 CSS 末尾追加紧凑化规则（无 CSS 时自动新建并挂接），或用模板样式替换并注入正文样式类，可选清理空段与 <br>。",
}

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_TEMPLATE = BASE_DIR / "sample_test.epub"

PAGE = PageConfig(
    key="optimize",
    title="EPUB格式优化",
    subtitle="追加紧凑化样式规则或整套替换为模板样式，可顺带清理正文空段与 <br> 换行标签。",
    endpoint="/optimize/api/run",
    preview_endpoint="/optimize/api/preview",
    preview_label="预览效果",
    upload_label="EPUB 文件",
    upload_accept=".epub,application/epub+zip",
    upload_hint="选择 EPUB 后可先预览将要改动的统计，确认后再导出。最大 50MB。",
    modes=(
        Mode(
            value=MODE_APPEND,
            label="追加紧凑规则",
            upload_label="EPUB 文件",
            accept=".epub,application/epub+zip",
            hint="在每个现有样式表末尾追加紧凑化规则，保留原有排版；已含规则的文件会跳过。若 EPUB 没有任何 CSS，会自动新建 epub-compact.css 并挂到正文与 OPF。最大 50MB。",
            submit_label="优化并下载",
        ),
        Mode(
            value=MODE_REPLACE,
            label="替换为模板样式",
            upload_label="EPUB 文件",
            accept=".epub,application/epub+zip",
            hint="用模板样式表整套替换，并给无 class 的段落 / 一级标题注入正文样式类；适合无样式或样式混乱的 EPUB。最大 50MB。",
            submit_label="优化并下载",
        ),
    ),
    fields_title="处理项",
    fields_hint=(
        "追加模式默认不改 HTML 结构；仅当 EPUB 完全没有样式表时，会新建样式表并补写 link / OPF。"
        "替换模式会更新样式表、必要时补写 OPF 清单。"
        "「删除空段」按 spine 正文处理，判定与「文本清洗」一致；"
        "「删除 <br>」会移除正文与封面等 HTML 里的全部换行标签，可能影响诗节或对话排版，请先预览。"
    ),
    fields=(
        Field(
            "template_file",
            "模板 EPUB（替换模式）",
            type="file",
            hint="仅替换模式使用；留空则用项目默认模板 sample_test.epub。追加模式忽略本项。",
        ),
        Field(
            "remove_empty_paragraphs",
            "删除空段",
            type="checkbox",
            default=True,
            hint="按 spine 删除正文里没有文字、也没有插图 / 表格的 <p>；判定与「文本清洗」「样式排版」一致",
        ),
        Field(
            "remove_br",
            "删除 <br> 换行标签",
            type="checkbox",
            default=True,
            hint="移除 HTML 中的 <br>；若正文依赖硬换行（诗节、对话），请取消勾选",
        ),
        Field(
            "same_filename",
            "输出与源文件同名",
            type="checkbox",
            default=False,
            hint="勾选后下载文件名不带 _optimized 后缀，便于覆盖本地同名文件",
        ),
    ),
    result_kind="download",
    result_hint="导出后会自动下载优化后的 EPUB，同时在上方给出下载链接。",
)

install_error_handlers(optimize_bp, extra_exceptions=(EpubIOError,))


def _read_epub_upload():
    upload = read_upload("file", {".epub"}, label="EPUB 文件")
    if not is_epub_bytes(upload.data):
        raise ToolkitError(INVALID_EPUB_MESSAGE)
    check_zip_bomb(upload.data)
    return upload


def _bool_field(name):
    return (request.form.get(name) or "").lower() in {"1", "true", "yes", "on"}


def _mode_from_form():
    mode = (request.form.get("mode") or MODE_APPEND).strip()
    if mode not in MODE_VALUES:
        raise ToolkitError("未知的处理模式，请刷新页面后重试")
    return mode


def _template_css_map(mode):
    """替换模式：读取上传模板或回落到默认 sample_test.epub；追加模式返回 None。"""
    if mode != MODE_REPLACE:
        return None

    template = request.files.get("template_file")
    if template is not None and template.filename:
        from werkzeug.utils import secure_filename

        suffix = Path(template.filename).suffix.lower()
        if suffix != ".epub":
            raise ToolkitError("模板文件类型不支持（仅支持 .epub）")
        data = template.read()
        if not data:
            raise ToolkitError("上传的模板文件为空")
        if not is_epub_bytes(data):
            raise ToolkitError("模板不是有效的 EPUB/ZIP")
        check_zip_bomb(data)
        return extract_template_css_map(data)

    if not DEFAULT_TEMPLATE.exists():
        raise ToolkitError(
            f"替换模式需要模板 EPUB：请上传模板，或确认默认模板存在（{DEFAULT_TEMPLATE.name}）"
        )
    return extract_template_css_map(DEFAULT_TEMPLATE.read_bytes())


def _options_from_form():
    return {
        "remove_empty_paragraphs": _bool_field("remove_empty_paragraphs"),
        "remove_br": _bool_field("remove_br"),
        "same_filename": _bool_field("same_filename"),
    }


@optimize_bp.route("/")
def index():
    return render_tool_page(
        replace(
            PAGE,
            endpoint=url_for("optimize.api_run"),
            preview_endpoint=url_for("optimize.api_preview"),
        )
    )


@optimize_bp.post("/api/preview")
def api_preview():
    """试跑，返回预览 HTML 与统计，不写盘。"""
    upload = _read_epub_upload()
    mode = _mode_from_form()
    options = _options_from_form()
    template_css_map = _template_css_map(mode)

    with TemporaryDirectory() as temp_dir:
        book = extract_epub(upload.data, Path(temp_dir) / "book")
        stats = optimize_book(
            book,
            mode=mode,
            template_css_map=template_css_map,
            remove_empty_paragraphs=options["remove_empty_paragraphs"],
            remove_br=options["remove_br"],
            write=False,
        )

    return ok(
        html=render_preview_html(stats),
        summary=stats.summary(),
        stats=stats.as_dict(),
    )


@optimize_bp.post("/api/run")
def api_run():
    """真正处理并导出新的 EPUB。"""
    upload = _read_epub_upload()
    mode = _mode_from_form()
    options = _options_from_form()
    template_css_map = _template_css_map(mode)

    with TemporaryDirectory() as temp_dir:
        book = extract_epub(upload.data, Path(temp_dir) / "book")
        stats = optimize_book(
            book,
            mode=mode,
            template_css_map=template_css_map,
            remove_empty_paragraphs=options["remove_empty_paragraphs"],
            remove_br=options["remove_br"],
            write=True,
        )
        payload = write_epub(book)

    if options["same_filename"]:
        download_name = safe_filename(f"{upload.stem}.epub", "book.epub")
    else:
        download_name = safe_filename(f"{upload.stem}_optimized.epub", "book.epub")

    response = send_file(
        payload,
        mimetype=EPUB_MIMETYPE,
        as_attachment=True,
        download_name=download_name,
    )
    return attach_message(response, stats.summary())
