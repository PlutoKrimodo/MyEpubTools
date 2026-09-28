# -*- coding: utf-8 -*-
"""解包 / 重打包工具。

* **解包**：上传 EPUB，解包后原样打包成 ZIP 供下载，保留原始目录结构。
* **重打包**：上传 ZIP，校验结构后按 EPUB 规范重新打包
  （``mimetype`` 是不压缩的首个条目）。

页面由 :mod:`modules.toolkit` 的通用框架渲染，本模块只负责声明式配置与处理接口，
归档读写全部复用 :mod:`modules.epub_io`。
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
from modules.toolkit import (
    Mode,
    PageConfig,
    ToolkitError,
    check_zip_bomb,
    install_error_handlers,
    read_upload,
    render_tool_page,
    safe_filename,
    zip_directory,
)

repack_bp = Blueprint("repack", __name__)

TOOL = {
    "key": "repack",
    "title": "解包 / 重打包",
    "url_prefix": "/repack",
    "order": 30,
    "description": "把 EPUB 解包成 ZIP 查看内部结构，或把 ZIP 重新打包成规范 EPUB。",
}

MODE_UNPACK = "unpack"
MODE_REPACK = "repack"
MODE_VALUES = (MODE_UNPACK, MODE_REPACK)

MISSING_STRUCTURE_MESSAGE = "ZIP 中缺少 META-INF/container.xml，不是可重打包的 EPUB 结构"
MISSING_OPF_MESSAGE = "ZIP 中找不到 .opf 包文件，不是可重打包的 EPUB 结构"

PAGE = PageConfig(
    key="repack",
    title="解包 / 重打包",
    subtitle="解包 EPUB 成 ZIP 查看内部结构，或把符合 EPUB 结构的 ZIP 重新打包成规范 EPUB。",
    endpoint="/repack/api/run",
    modes=(
        Mode(
            value=MODE_UNPACK,
            label="解包 EPUB",
            upload_label="EPUB 文件",
            accept=".epub,application/epub+zip",
            hint="上传 EPUB 后会得到保留原始目录结构的 ZIP 压缩包。最大 50MB。",
            submit_label="解包并下载 ZIP",
        ),
        Mode(
            value=MODE_REPACK,
            label="重打包 ZIP",
            upload_label="ZIP 压缩包",
            accept=".zip,application/zip",
            hint=(
                "ZIP 需包含 mimetype 与 META-INF/container.xml。"
                "重打包时 mimetype 会被规范成不压缩的首个条目。最大 50MB。"
            ),
            submit_label="重打包并下载 EPUB",
        ),
    ),
    result_kind="download",
    result_hint="处理完成后会直接触发浏览器下载，同时在上方给出下载链接。",
)

install_error_handlers(repack_bp, extra_exceptions=(EpubIOError,))


# --------------------------------------------------------------------------- #
# 解包 / 重打包
# --------------------------------------------------------------------------- #
def _flatten_single_root(root):
    """ZIP 里若只有一个顶层文件夹且形似 EPUB，则下沉一层。

    很多压缩工具会多套一层同名目录，直接重打包会得到 ``mimetype`` 不在根目录的
    非法 EPUB，这里先行扁平化。
    """
    root = Path(root)
    entries = [entry for entry in root.iterdir()]
    if len(entries) != 1 or not entries[0].is_dir():
        return root

    inner = entries[0]
    if (inner / "mimetype").is_file() or (inner / "META-INF").is_dir():
        return inner
    return root


def _require_epub_structure(book):
    """确认解包目录具备最小 EPUB 结构，否则给出可操作的中文提示。"""
    book = Path(book)
    if not (book / "META-INF" / "container.xml").is_file():
        raise ToolkitError(MISSING_STRUCTURE_MESSAGE)
    if not any(book.rglob("*.opf")):
        raise ToolkitError(MISSING_OPF_MESSAGE)


def _normalize_mimetype(book):
    """把 ``mimetype`` 规范成标准值：缺失或内容异常都纠正，内容正确则原样保留。"""
    target = Path(book) / "mimetype"
    if target.is_file():
        try:
            if target.read_text(encoding="utf-8-sig").strip() == EPUB_MIMETYPE:
                return
        except (OSError, UnicodeDecodeError):
            pass
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(EPUB_MIMETYPE, encoding="ascii")


def _unpack():
    """EPUB → ZIP。返回 ``(下载文件名, MIME, BytesIO)``。"""
    upload = read_upload("file", {".epub"}, label="EPUB 文件")
    if not is_epub_bytes(upload.data):
        raise ToolkitError(INVALID_EPUB_MESSAGE)
    check_zip_bomb(upload.data)

    with TemporaryDirectory() as temp_dir:
        book = extract_epub(upload.data, Path(temp_dir) / "book")
        payload = zip_directory(book)

    return safe_filename(f"{upload.stem}.zip", "book.zip"), "application/zip", payload


def _repack():
    """ZIP → EPUB。返回 ``(下载文件名, MIME, BytesIO)``。"""
    upload = read_upload("file", {".zip"}, label="ZIP 压缩包")
    if not is_epub_bytes(upload.data):
        raise ToolkitError(INVALID_EPUB_MESSAGE)
    check_zip_bomb(upload.data)

    with TemporaryDirectory() as temp_dir:
        root = extract_epub(upload.data, Path(temp_dir) / "book")
        book = _flatten_single_root(root)
        _require_epub_structure(book)
        _normalize_mimetype(book)
        payload = write_epub(book)

    return safe_filename(f"{upload.stem}.epub", "book.epub"), EPUB_MIMETYPE, payload


# --------------------------------------------------------------------------- #
# 路由
# --------------------------------------------------------------------------- #
@repack_bp.route("/")
def index():
    return render_tool_page(replace(PAGE, endpoint=url_for("repack.api_run")))


@repack_bp.post("/api/run")
def api_run():
    mode = (request.form.get("mode") or "").strip()
    if mode not in MODE_VALUES:
        raise ToolkitError("未知的处理模式，请刷新页面后重试")

    if mode == MODE_UNPACK:
        download_name, mimetype, payload = _unpack()
    else:
        download_name, mimetype, payload = _repack()

    return send_file(
        payload,
        mimetype=mimetype,
        as_attachment=True,
        download_name=download_name,
    )
