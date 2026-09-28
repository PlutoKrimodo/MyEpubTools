# -*- coding: utf-8 -*-
"""EPUB 读写公共层。

各工具 Blueprint 通过本模块复用同一套「解包 / 重打包 / 文档读取」逻辑，
避免每个工具各写一份。本模块只依赖标准库，不引入额外的第三方依赖。

约定：
    * 解包前先整体校验成员路径，拒绝绝对路径与 ``..`` 穿越，校验通过后才落盘，
      避免解压到一半才发现路径不安全。
    * 重打包时 ``mimetype`` 必须是不压缩的第一个条目（EPUB 规范要求），
      其余条目统一使用 DEFLATE。
"""
from __future__ import annotations

import io
import zipfile
from pathlib import Path

EPUB_MIMETYPE = "application/epub+zip"
HTML_EXTS = {".html", ".htm", ".xhtml"}

INVALID_EPUB_MESSAGE = "上传的文件不是有效的 EPUB/ZIP"
UNSAFE_PATH_MESSAGE = "EPUB 包含不安全的文件路径"

_ENCODINGS = ("utf-8-sig", "utf-8", "gb18030", "big5", "cp1252")


class EpubIOError(Exception):
    """EPUB 读取或写入失败。"""


def normalize_member_name(name):
    """校验 ZIP 成员路径。

    合法则返回规范化后的路径，否则抛出 ``EpubIOError``。
    """
    name = (name or "").replace("\\", "/")
    path = Path(name)
    if path.is_absolute() or ".." in path.parts:
        raise EpubIOError(UNSAFE_PATH_MESSAGE)
    return name


def is_epub_bytes(data):
    """判断字节串是否是可读的 ZIP（EPUB）容器。"""
    return zipfile.is_zipfile(io.BytesIO(data))


def decode_text(data):
    """按常见中文书籍编码依次尝试解码，全部失败则退化为 UTF-8 容错解码。"""
    for encoding in _ENCODINGS:
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _open_archive(source):
    if isinstance(source, (bytes, bytearray, memoryview)):
        return zipfile.ZipFile(io.BytesIO(bytes(source)), "r")
    return zipfile.ZipFile(Path(source), "r")


def extract_epub(source, dest_dir):
    """把 EPUB 解包到 ``dest_dir``。

    ``source`` 可以是 bytes，也可以是文件路径；返回解包后的目录 ``Path``。
    """
    try:
        archive = _open_archive(source)
    except zipfile.BadZipFile as exc:
        raise EpubIOError(INVALID_EPUB_MESSAGE) from exc

    dest = Path(dest_dir)
    with archive:
        for info in archive.infolist():
            normalize_member_name(info.filename)
        dest.mkdir(parents=True, exist_ok=True)
        archive.extractall(dest)
    return dest


def read_xhtml_documents(root, extensions=frozenset(HTML_EXTS)):
    """读取 ``root`` 下所有 XHTML/HTML 文档。

    返回 ``{相对 root 的 posix 路径: 文本内容}``，保持 ``rglob`` 的遍历顺序。
    """
    root = Path(root)
    documents = {}
    for path in root.rglob("*"):
        if path.is_file() and path.suffix.lower() in extensions:
            relative = path.relative_to(root).as_posix()
            documents[relative] = decode_text(path.read_bytes())
    return documents


def write_epub(source_dir, destination=None, mimetype=None):
    """把目录重新打包成符合规范的 EPUB。

    ``destination`` 为 None 时返回已就绪的 ``BytesIO``，否则写入该路径并返回 ``Path``。
    ``mimetype`` 为 None 时沿用目录内已有的 mimetype，缺失则使用标准值。
    """
    root = Path(source_dir)
    if mimetype is None:
        existing = root / "mimetype"
        payload = existing.read_bytes() if existing.is_file() else EPUB_MIMETYPE.encode("ascii")
    elif isinstance(mimetype, str):
        payload = mimetype.encode("ascii")
    else:
        payload = bytes(mimetype)

    target = io.BytesIO() if destination is None else Path(destination)
    with zipfile.ZipFile(target, "w") as archive:
        info = zipfile.ZipInfo("mimetype")
        info.compress_type = zipfile.ZIP_STORED
        archive.writestr(info, payload)

        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            arcname = normalize_member_name(path.relative_to(root).as_posix())
            if arcname == "mimetype":
                continue
            archive.write(path, arcname, compress_type=zipfile.ZIP_DEFLATED)

    if destination is None:
        target.seek(0)
        return target
    return Path(destination)
