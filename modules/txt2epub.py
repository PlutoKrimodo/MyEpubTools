# -*- coding: utf-8 -*-
import os
import re
import shutil
import time
import uuid
import zipfile
import sys
from pathlib import Path

from flask import Blueprint, jsonify, request, send_file, send_from_directory
from werkzeug.utils import secure_filename

from build_epub import (
    EpubBuildError,
    analyze_txt,
    build_epub,
    generate_chapter_regex,
)

from modules.epub_io import EpubIOError
from modules.epub_to_txt import ExtractOptions, extract_from_epub, write_text_file


def get_resource_path(relative_path):
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)


BASE_DIR = Path(__file__).resolve().parent.parent
UPLOAD_DIR = BASE_DIR / "uploads"
OUTPUT_DIR = BASE_DIR / "outputs"
WORK_DIR = BASE_DIR / "work"
WEB_DIR = BASE_DIR / "web"
DEFAULT_TEMPLATE = Path(get_resource_path("sample_test.epub"))

txt2epub_bp = Blueprint("txt2epub", __name__)

TOOL = {
    "key": "txt2epub",
    "title": "EPUB ⇄ TXT",
    "url_prefix": "/txt2epub",
    "order": 10,
    "description": "在 EPUB 与纯文本之间双向转换：TXT 可按章节生成带模板样式的 EPUB，也可把 EPUB 正文按阅读顺序提取成 TXT。",
}

OUTPUT_DIRECTORY_TTL_SECONDS = 60 * 60
DOWNLOAD_TTL_SECONDS = 24 * 60 * 60
#: EPUB → TXT 的预览最多回传多少行，免得一本大书把响应撑爆。
PREVIEW_LINE_LIMIT = 400
selected_output_dirs = {}
download_jobs = {}


def ensure_dirs():
    for directory in (UPLOAD_DIR, OUTPUT_DIR, WORK_DIR):
        directory.mkdir(parents=True, exist_ok=True)


def json_error(message, status=400):
    return jsonify({"ok": False, "error": str(message)}), status


def require_file(name):
    file = request.files.get(name)
    if file is None or not file.filename:
        raise EpubBuildError(f"缺少上传文件：{name}")
    return file


def save_upload(file, dest_dir, allowed_suffixes):
    original_name = Path(file.filename).name
    suffix = Path(original_name).suffix.lower()
    if suffix not in allowed_suffixes:
        raise EpubBuildError(f"文件类型不支持：{original_name}")

    filename = secure_filename(original_name)
    if not filename:
        filename = f"upload{suffix}"
    path = dest_dir / filename
    file.save(path)
    return path


_UNSAFE_FILENAME_RE = re.compile(r'[\\/:*?"<>|\r\n]+')

_RESERVED_FILENAMES = {
    "CON", "PRN", "AUX", "NUL", "COM1", "COM2", "COM3", "COM4", "COM5",
    "COM6", "COM7", "COM8", "COM9", "LPT1", "LPT2", "LPT3", "LPT4", "LPT5",
    "LPT6", "LPT7", "LPT8", "LPT9",
}


def safe_output_filename(title, extension):
    """清洗书名生成安全的输出文件名（去掉非法字符与 Windows 保留名）。"""
    name = _UNSAFE_FILENAME_RE.sub("_", title).strip(" ._")
    if not name or name.upper() in _RESERVED_FILENAMES:
        name = "book"
    return f"{name}.{extension}"


def safe_epub_filename(title):
    return safe_output_filename(title, "epub")


def safe_txt_filename(title):
    return safe_output_filename(title, "txt")


def bool_field(name):
    return request.form.get(name, "").lower() in {"1", "true", "yes", "on"}


def purge_expired_records():
    now = time.monotonic()
    for token, (_, expires_at) in list(selected_output_dirs.items()):
        if expires_at <= now:
            del selected_output_dirs[token]
    for job_id, (_, expires_at) in list(download_jobs.items()):
        if expires_at <= now:
            del download_jobs[job_id]


def select_output_directory():
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        try:
            selected = filedialog.askdirectory(title="选择 EPUB 输出文件夹", mustexist=True)
        finally:
            root.destroy()
    except Exception as exc:
        raise EpubBuildError(f"无法打开文件夹选择窗口：{exc}") from exc

    if not selected:
        return None

    directory = Path(selected).expanduser().resolve(strict=True)
    if not directory.is_dir():
        raise EpubBuildError("所选路径不是文件夹")
    if not os.access(directory, os.W_OK):
        raise EpubBuildError("所选文件夹不可写")
    return directory


def resolve_output_directory():
    purge_expired_records()
    token = request.form.get("output_dir_token", "")
    if not token:
        return OUTPUT_DIR

    record = selected_output_dirs.get(token)
    if record is None:
        raise EpubBuildError("输出文件夹选择已失效，请重新选择")
    return record[0]


def resolve_target_directory():
    """定位输出目录：表单里给了就用它（相对路径基于项目根目录），否则用先前选中的。

    TXT → EPUB 与 EPUB → TXT 两个方向共用，避免各写一份。
    """
    output_dir_param = request.form.get("output_dir", "").strip()
    if not output_dir_param:
        return resolve_output_directory()

    directory = Path(output_dir_param)
    if not directory.is_absolute():
        directory = BASE_DIR / directory
    directory = directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    if not os.access(directory, os.W_OK):
        raise EpubBuildError(f"输出目录不可写：{directory}")
    return directory


def unique_output_path(directory, filename):
    candidate = directory / filename
    if not candidate.exists():
        return candidate

    stem = Path(filename).stem
    suffix = Path(filename).suffix
    index = 1
    while True:
        candidate = directory / f"{stem} ({index}){suffix}"
        if not candidate.exists():
            return candidate
        index += 1


@txt2epub_bp.route("/")
def index():
    return send_from_directory(WEB_DIR, "index.html")


@txt2epub_bp.route("/web/<path:filename>")
def tool_static(filename):
    return send_from_directory(WEB_DIR, filename)


@txt2epub_bp.route("/api/generate-chapter-regex", methods=["POST"])
def api_generate_chapter_regex():
    """从多行章节标题示例生成自定义章节正则，供前端填入输入框。"""
    try:
        samples_text = request.form.get("samples") or ""
        pattern = generate_chapter_regex(samples_text)
        compiled = re.compile(pattern)
        lines = [line.strip() for line in samples_text.splitlines() if line.strip()]
        matched = sum(1 for line in lines if compiled.match(line))
        return jsonify({
            "ok": True,
            "regex": pattern,
            "matched": matched,
            "total": len(lines),
        })
    except (EpubBuildError, re.error) as exc:
        return json_error(exc)


@txt2epub_bp.route("/api/preview", methods=["POST"])
def preview():
    job_upload_dir = None
    try:
        ensure_dirs()
        job_id = uuid.uuid4().hex
        job_upload_dir = UPLOAD_DIR / job_id
        job_upload_dir.mkdir(parents=True)

        input_file = save_upload(require_file("input_file"), job_upload_dir, {".txt"})
        result = analyze_txt(
            src_txt=input_file,
            chapter_regex=request.form.get("chapter_regex") or None,
            chapter_mode=request.form.get("chapter_mode") or "append",
            encoding=request.form.get("encoding") or "utf-8",
        )
        return jsonify({"ok": True, **result})
    except (EpubBuildError, UnicodeDecodeError, re.error) as exc:
        return json_error(exc)
    finally:
        if job_upload_dir and job_upload_dir.exists():
            shutil.rmtree(job_upload_dir, ignore_errors=True)


@txt2epub_bp.route("/api/select-output-directory", methods=["POST"])
def choose_output_directory():
    try:
        ensure_dirs()
        directory = select_output_directory()
        if directory is None:
            return jsonify({"ok": True, "cancelled": True})

        purge_expired_records()
        token = uuid.uuid4().hex
        selected_output_dirs[token] = (
            directory,
            time.monotonic() + OUTPUT_DIRECTORY_TTL_SECONDS,
        )
        return jsonify({"ok": True, "path": str(directory), "token": token})
    except EpubBuildError as exc:
        return json_error(exc)


@txt2epub_bp.route("/api/build", methods=["POST"])
def build():
    job_upload_dir = None
    try:
        ensure_dirs()
        job_id = uuid.uuid4().hex
        job_upload_dir = UPLOAD_DIR / job_id
        job_upload_dir.mkdir(parents=True)
        job_work_dir = WORK_DIR / job_id

        input_upload = require_file("input_file")
        original_stem = Path(input_upload.filename).stem
        input_file = save_upload(input_upload, job_upload_dir, {".txt"})

        template_file = request.files.get("template_file")
        if template_file is None or not template_file.filename:
            if not DEFAULT_TEMPLATE.exists():
                raise EpubBuildError(f"默认模板文件不存在：{DEFAULT_TEMPLATE}")
            template_path = DEFAULT_TEMPLATE
        else:
            template_path = save_upload(template_file, job_upload_dir, {".epub"})

        title = (request.form.get("title") or original_stem).strip() or original_stem
        author = (request.form.get("author") or "未知").strip() or "未知"
        lang = (request.form.get("lang") or "zh-Hans").strip() or "zh-Hans"
        filename = safe_epub_filename(title)

        output_dir = resolve_target_directory()
        output_file = unique_output_path(output_dir, filename)
        logs = []

        build_epub(
            src_txt=input_file,
            template_epub=template_path,
            out_epub=output_file,
            title=title,
            author=author,
            lang=lang,
            build_dir=job_work_dir,
            chapter_regex=request.form.get("chapter_regex") or None,
            chapter_mode=request.form.get("chapter_mode") or "append",
            encoding=request.form.get("encoding") or "utf-8",
            toc_page=bool_field("toc_page"),
            subtitle=(request.form.get("subtitle") or "").strip(),
            log_func=lambda message: logs.append(str(message)),
        )

        download_jobs[job_id] = (
            output_file,
            time.monotonic() + DOWNLOAD_TTL_SECONDS,
        )
        return jsonify({
            "ok": True,
            "filename": output_file.name,
            "download_url": f"/txt2epub/download/{job_id}/{output_file.name}",
            "logs": logs,
        })
    except (EpubBuildError, UnicodeDecodeError, zipfile.BadZipFile, re.error) as exc:
        return json_error(exc)
    finally:
        if job_upload_dir and job_upload_dir.exists():
            shutil.rmtree(job_upload_dir, ignore_errors=True)


@txt2epub_bp.route("/download/<job_id>/<filename>")
def download(job_id, filename):
    if not re.fullmatch(r"[0-9a-f]{32}", job_id):
        return json_error("下载任务不存在", 404)

    purge_expired_records()
    record = download_jobs.get(job_id)
    if record is None:
        return json_error("下载文件不存在", 404)

    output_file = record[0]
    if filename != output_file.name or not output_file.is_file():
        return json_error("下载文件不存在", 404)
    return send_file(output_file, as_attachment=True, download_name=output_file.name)


# --------------------------------------------------------------------------- #
# EPUB → TXT
# --------------------------------------------------------------------------- #
def read_extract_options():
    """从表单读取导出选项。未勾选的复选框不会出现在表单里，取值即为 False。"""
    return ExtractOptions(
        keep_titles=bool_field("keep_titles"),
        blank_line=bool_field("blank_line"),
        line_ending=request.form.get("line_ending") or "lf",
        encoding=request.form.get("encoding") or "utf-8",
    )


@txt2epub_bp.route("/api/to-txt/preview", methods=["POST"])
def to_txt_preview():
    """试跑 EPUB → TXT：只回传统计与前若干行，不写盘。"""
    job_upload_dir = None
    try:
        ensure_dirs()
        job_id = uuid.uuid4().hex
        job_upload_dir = UPLOAD_DIR / job_id
        job_upload_dir.mkdir(parents=True)

        epub_file = save_upload(require_file("input_file"), job_upload_dir, {".epub"})
        result = extract_from_epub(
            epub_file,
            job_upload_dir / "unpacked",
            read_extract_options(),
        )
        return jsonify({
            "ok": True,
            "stats": result.stats.as_dict(),
            "lines": result.lines[:PREVIEW_LINE_LIMIT],
            "truncated": len(result.lines) > PREVIEW_LINE_LIMIT,
        })
    except (EpubBuildError, EpubIOError, UnicodeDecodeError, zipfile.BadZipFile, re.error) as exc:
        return json_error(exc)
    finally:
        if job_upload_dir and job_upload_dir.exists():
            shutil.rmtree(job_upload_dir, ignore_errors=True)


@txt2epub_bp.route("/api/to-txt", methods=["POST"])
def to_txt():
    """正式执行 EPUB → TXT：按 spine 顺序提取正文并写到输出目录。"""
    job_upload_dir = None
    try:
        ensure_dirs()
        job_id = uuid.uuid4().hex
        job_upload_dir = UPLOAD_DIR / job_id
        job_upload_dir.mkdir(parents=True)

        input_upload = require_file("input_file")
        original_stem = Path(input_upload.filename).stem
        epub_file = save_upload(input_upload, job_upload_dir, {".epub"})

        result = extract_from_epub(
            epub_file,
            job_upload_dir / "unpacked",
            read_extract_options(),
        )

        # 文件名取书里的书名（提取时已从 OPF 元数据读出），拿不到才用上传文件名。
        title = (result.stats.title or original_stem).strip() or original_stem
        filename = safe_txt_filename(title)

        output_dir = resolve_target_directory()
        output_file = unique_output_path(output_dir, filename)
        write_text_file(output_file, result)

        download_jobs[job_id] = (
            output_file,
            time.monotonic() + DOWNLOAD_TTL_SECONDS,
        )
        return jsonify({
            "ok": True,
            "filename": output_file.name,
            "download_url": f"/txt2epub/download/{job_id}/{output_file.name}",
            "stats": result.stats.as_dict(),
            "logs": [
                f"书名：{result.stats.title or '（书里未标注）'}",
                f"提取：{result.stats.summary()}",
                f"格式：{result.options.line_ending.upper()} 换行，{result.options.encoding} 编码",
                f"输出：{output_file}",
            ],
        })
    except (EpubBuildError, EpubIOError, UnicodeDecodeError, zipfile.BadZipFile, re.error) as exc:
        return json_error(exc)
    finally:
        if job_upload_dir and job_upload_dir.exists():
            shutil.rmtree(job_upload_dir, ignore_errors=True)
