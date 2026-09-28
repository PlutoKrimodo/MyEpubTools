# -*- coding: utf-8 -*-
"""EPUB 格式优化公共层。

移植自 ``txt_to_epub/build_epub.py`` 的 ``optimize_epub``，改为对**已解包目录**
操作；归档读写交给 :mod:`modules.epub_io`，正文空段清理复用
:func:`modules.epub_text.clean_book`，不另写一套判定。

两类模式
--------
* **append**——在每个现有样式表末尾追加紧凑化规则（``COMPACT_CSS``）。
  已包含同样规则的文件跳过，幂等；不改 HTML class、不改 OPF。
  若整本没有任何样式表，则在正文目录新建只含紧凑规则的 CSS，
  并补 ``<link>`` 与 OPF manifest，保证规则一定生效。
* **replace**——用模板样式表覆盖 / 写入，并给无 class 的 ``<p>`` / ``<h1>``
  注入 ``bodyContent-1`` / ``chaptertitle-c``，确保 ``<head>`` 链到模板 CSS，
  必要时把新 CSS 补进 ``content.opf`` manifest。适合无样式或样式混乱的书。

两模式均可选：
    * 删除 spine 正文里的空 ``<p>``（判定与「文本清洗」「样式排版」一致）；
    * 删除 HTML 中的 ``<br>`` 标签（独立于空段开关，统计数量）。

本模块**没有** ``TOOL`` 字典，属于公共模块，``registry.discover()`` 会自动跳过。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import escape

from modules.epub_io import HTML_EXTS, EpubIOError, decode_text
from modules.epub_text import CleanOptions, clean_book

MODE_APPEND = "append"
MODE_REPLACE = "replace"
MODE_VALUES = (MODE_APPEND, MODE_REPLACE)

#: 「删除空段」的选项：只打开删空段这一项，其余清洗开关全部关闭。
#: 判定标准与「文本清洗」「样式排版」逐字一致，避免两套实现各自演化。
EMPTY_PARAGRAPH_OPTIONS = CleanOptions(
    normalize_whitespace=False,
    remove_empty_paragraphs=True,
    merge_broken_paragraphs=False,
    strip_inline=False,
)

COMPACT_CSS = """\
/* ===== EPUB 正文紧凑化：取消空段、收紧行高 ===== */
/* 1. 无 class 的段落也去掉默认空段 */
body p {
    margin-top: 0;
    margin-bottom: 0.15em;
}

/* 2. 核心正文段落：取消大段间距，行高略微收紧 */
p[class*="bodyContent"] {
    margin-top: 0 !important;
    margin-bottom: 0.15em !important;
    line-height: 1.4 !important;
}

/* 3. 相邻正文段之间不再额外留上间距，避免出现空段 */
p[class*="bodyContent"] + p[class*="bodyContent"] {
    margin-top: 0 !important;
}

/* 4. 标题后第一段保留一点呼吸感，避免标题和正文粘死 */
h1 + p[class*="bodyContent"],
h2 + p[class*="bodyContent"],
h3 + p[class*="bodyContent"],
h4 + p[class*="bodyContent"],
h5 + p[class*="bodyContent"],
h6 + p[class*="bodyContent"] {
    margin-top: 0.5em !important;
}

/* 5. 可选：引证、书信、版权、盒子等正文性段落也一起紧凑 */
p[class*="quoteContent"],
p[class*="letterContent"],
p[class*="copyContent"],
p[class*="boxContent"] {
    margin-top: 0 !important;
    margin-bottom: 0.15em !important;
    line-height: 1.4 !important;
}

p[class*="quoteContent"] + p[class*="quoteContent"],
p[class*="letterContent"] + p[class*="letterContent"],
p[class*="copyContent"] + p[class*="copyContent"],
p[class*="boxContent"] + p[class*="boxContent"] {
    margin-top: 0 !important;
}
"""

_BR_RE = re.compile(r"<br\s*/?\s*>", re.IGNORECASE)
_BARE_P_RE = re.compile(r"<p(?!\s[^>]*class=)(\s|>)", re.IGNORECASE)
_BARE_H1_RE = re.compile(r"<h1(?!\s[^>]*class=)(\s|>)", re.IGNORECASE)
_CSS_SUFFIX = ".css"
#: append 模式在「完全没有样式表」时新建的文件名（固定名，便于幂等与 OPF 去重）。
_FALLBACK_CSS_NAME = "epub-compact.css"


class EpubOptimizeError(EpubIOError):
    """格式优化失败。继承 ``EpubIOError``，便于工具统一兜底。"""


@dataclass
class OptimizeStats:
    """一次格式优化的统计与告警。"""

    mode: str = MODE_APPEND
    stylesheets: int = 0
    css_appended: int = 0
    css_skipped: int = 0
    css_written: int = 0
    css_created: int = 0
    html_total: int = 0
    html_updated: int = 0
    opf_updated: bool = False
    empty_paragraphs_removed: int = 0
    empty_documents_changed: int = 0
    spine_documents: int = 0
    br_removed: int = 0
    warnings: list = field(default_factory=list)

    def as_dict(self):
        return {
            "mode": self.mode,
            "stylesheets": self.stylesheets,
            "css_appended": self.css_appended,
            "css_skipped": self.css_skipped,
            "css_written": self.css_written,
            "css_created": self.css_created,
            "html_total": self.html_total,
            "html_updated": self.html_updated,
            "opf_updated": self.opf_updated,
            "empty_paragraphs_removed": self.empty_paragraphs_removed,
            "empty_documents_changed": self.empty_documents_changed,
            "spine_documents": self.spine_documents,
            "br_removed": self.br_removed,
            "warnings": list(self.warnings),
        }

    def summary(self):
        """一句话中文摘要，供状态条 / 下载提示使用。"""
        mode_label = "追加紧凑规则" if self.mode == MODE_APPEND else "替换模板样式"
        parts = [mode_label]

        if self.mode == MODE_APPEND:
            if self.css_created:
                parts.append(
                    f"新建样式表 {_FALLBACK_CSS_NAME} 并写入紧凑规则（{self.css_created} 个）"
                )
            elif self.css_appended:
                parts.append(f"写入样式表 {self.css_appended} 个")
            elif self.stylesheets:
                parts.append(f"样式表已含规则（跳过 {self.css_skipped} 个）")
            else:
                parts.append("未找到样式表")
            if self.css_created and self.html_updated:
                parts.append(f"链接到正文 {self.html_updated} 处")
            if self.css_created and self.opf_updated:
                parts.append("已更新 OPF 清单")
        else:
            if self.css_written:
                parts.append(f"应用模板样式表 {self.css_written} 个")
            else:
                parts.append("模板未提供样式表")
            if self.html_updated:
                parts.append(f"更新正文 {self.html_updated} 处")
            if self.opf_updated:
                parts.append("已更新 OPF 清单")

        if self.empty_paragraphs_removed:
            parts.append(
                f"删除空段 {self.empty_paragraphs_removed} 个"
                f"（涉及 {self.empty_documents_changed} 个正文文档）"
            )
        if self.br_removed:
            parts.append(f"删除 <br> {self.br_removed} 个")

        if len(parts) == 1:
            parts.append("没有需要处理的内容")
        return "；".join(parts) + "。"


def extract_template_css_map(template_data):
    """从模板 EPUB 字节流里抽出全部样式表，返回 ``{文件名: 字节内容}``。"""
    import io
    import zipfile

    if not template_data:
        return {}
    try:
        archive = zipfile.ZipFile(io.BytesIO(template_data))
    except zipfile.BadZipFile as exc:
        raise EpubOptimizeError("模板不是有效的 EPUB/ZIP") from exc

    mapping = {}
    with archive:
        for name in archive.namelist():
            if name.lower().endswith(_CSS_SUFFIX):
                mapping[Path(name).name] = archive.read(name)
    return mapping


def _iter_html_files(book_root):
    for path in sorted(Path(book_root).rglob("*")):
        if path.is_file() and path.suffix.lower() in HTML_EXTS:
            yield path


def _iter_css_files(book_root):
    for path in sorted(Path(book_root).rglob("*")):
        if path.is_file() and path.suffix.lower() == _CSS_SUFFIX:
            yield path


def _read_text(path):
    try:
        return decode_text(Path(path).read_bytes())
    except OSError:
        return None


def _write_text(path, content):
    # newline=""：Windows 下禁止 \n → \r\n 翻译，保证写入内容与内存一致，
    # 否则幂等标记 COMPACT_CSS 无法在回读时命中。
    Path(path).write_text(content, encoding="utf-8", newline="")


def _normalized_text(content):
    return content.replace("\r\n", "\n").replace("\r", "\n") if content else content


def _find_content_root(book_root, *, write=True):
    """定位样式表与正文所在的目录（优先 OEBPS / oebps，否则找含 HTML 的目录）。"""
    book_root = Path(book_root)
    for candidate in (book_root / "OEBPS", book_root / "oebps"):
        if candidate.is_dir():
            return candidate
    for directory in sorted(book_root.rglob("*")):
        if not directory.is_dir() or directory.name == "META-INF":
            continue
        if any(
            child.is_file() and child.suffix.lower() in HTML_EXTS
            for child in directory.iterdir()
        ):
            return directory
    fallback = book_root / "OEBPS"
    if write:
        fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def _find_opf(book_root, content_root):
    book_root = Path(book_root)
    content_root = Path(content_root)
    for candidate in (book_root / "content.opf", content_root / "content.opf"):
        if candidate.is_file():
            return candidate
    for path in sorted(book_root.rglob("*.opf")):
        if path.is_file():
            return path
    return None


def _append_compact_css(css_files, *, write, stats):
    for css_path in css_files:
        stats.stylesheets += 1
        content = _read_text(css_path)
        if content is None:
            stats.warnings.append(f"无法读取样式表：{css_path.name}")
            continue
        if COMPACT_CSS.strip() in _normalized_text(content):
            stats.css_skipped += 1
            continue
        # 预览不落盘，但仍计入「将追加」的样式表数量
        stats.css_appended += 1
        if write:
            _write_text(css_path, content.rstrip("\r\n") + "\n" + COMPACT_CSS)
            stats.css_written += 1


def _link_stylesheet_in_html(html_files, css_file, *, write, stats):
    """给 HTML 的 ``</head>`` 前补一条指向 ``css_file`` 的 link。"""
    if not html_files:
        return

    css_file = Path(css_file)
    link_template = '<link href="{href}" rel="stylesheet" type="text/css" />'

    for html_path in html_files:
        stats.html_total += 1
        content = _read_text(html_path)
        if content is None:
            stats.warnings.append(f"无法读取文档：{html_path.name}")
            continue
        if css_file.name in content:
            continue
        if "</head>" not in content:
            stats.warnings.append(f"文档缺少 </head>，未链接紧凑样式表：{html_path.name}")
            continue
        try:
            href = os.path.relpath(str(css_file), str(html_path.parent)).replace("\\", "/")
        except ValueError:
            href = css_file.name
        link_tag = link_template.format(href=href)
        new_content = content.replace("</head>", f"{link_tag}\n</head>", 1)
        stats.html_updated += 1
        if write:
            _write_text(html_path, new_content)


def _create_fallback_compact_css(book_root, content_root, html_files, *, write, stats):
    """完全没有样式表时：新建只含紧凑规则的 CSS，并挂到 HTML / OPF。"""
    target = Path(content_root) / _FALLBACK_CSS_NAME
    already = False
    if target.is_file():
        existing = _read_text(target)
        if existing is not None and COMPACT_CSS.strip() in _normalized_text(existing):
            already = True

    if already:
        stats.stylesheets += 1
        stats.css_skipped += 1
    else:
        stats.stylesheets += 1
        stats.css_created += 1
        stats.css_appended += 1
        if write:
            target.parent.mkdir(parents=True, exist_ok=True)
            _write_text(target, COMPACT_CSS)
            stats.css_written += 1

    _link_stylesheet_in_html(html_files, target, write=write, stats=stats)
    _update_opf_manifest(book_root, content_root, [_FALLBACK_CSS_NAME], write=write, stats=stats)


def _replace_css_and_inject_html(book_root, content_root, html_files, template_css_map, *, write, stats):
    injected_names = []
    for css_name, css_bytes in template_css_map.items():
        stats.stylesheets += 1
        if write:
            target_path = content_root / css_name
            # 模板字节原样落盘，不做文本换行翻译
            target_path.write_bytes(css_bytes)
        # 预览不落盘，但仍计入「将应用」的样式表数量
        stats.css_written += 1
        injected_names.append(css_name)

    if not html_files:
        if injected_names:
            stats.warnings.append("未找到 HTML 正文，仅更新了样式表")
        return injected_names

    first_html_dir = html_files[0].parent
    try:
        css_rel = os.path.relpath(content_root, first_html_dir).replace("\\", "/")
        css_rel = "" if css_rel == "." else css_rel + "/"
    except ValueError:
        css_rel = ""

    link_tags = "\n".join(
        f'  <link href="{css_rel}{name}" rel="stylesheet" type="text/css" />'
        for name in injected_names
    )

    for html_path in html_files:
        stats.html_total += 1
        content = _read_text(html_path)
        if content is None:
            stats.warnings.append(f"无法读取文档：{html_path.name}")
            continue

        original = content

        if injected_names and "</head>" in content:
            already_linked = any(name in content for name in injected_names)
            if not already_linked:
                content = content.replace("</head>", f"{link_tags}\n</head>")

        content = _BARE_P_RE.sub(r'<p class="bodyContent-1"\1', content)
        content = _BARE_H1_RE.sub(r'<h1 class="chaptertitle-c"\1', content)

        if content != original:
            stats.html_updated += 1
            if write:
                _write_text(html_path, content)

    _update_opf_manifest(book_root, content_root, injected_names, write=write, stats=stats)
    return injected_names


def _update_opf_manifest(book_root, content_root, injected_names, *, write, stats):
    if not injected_names:
        return
    opf_path = _find_opf(book_root, content_root)
    if opf_path is None:
        stats.warnings.append("未找到 OPF，跳过更新样式表清单")
        return

    content = _read_text(opf_path)
    if content is None:
        stats.warnings.append("无法读取 OPF，跳过更新样式表清单")
        return

    original = content
    for css_name in injected_names:
        if css_name in content:
            continue
        item_id = f"css_{css_name.replace('.', '_').replace('-', '_')}"
        try:
            css_href = os.path.relpath(content_root / css_name, opf_path.parent).replace("\\", "/")
        except ValueError:
            css_href = f"OEBPS/{css_name}"
        manifest_item = f'    <item id="{item_id}" href="{css_href}" media-type="text/css"/>'
        if "</manifest>" in content:
            content = content.replace("</manifest>", f"{manifest_item}\n  </manifest>", 1)

    if content != original:
        stats.opf_updated = True
        if write:
            _write_text(opf_path, content)


def _remove_br_tags(html_files, *, write, stats):
    for html_path in html_files:
        content = _read_text(html_path)
        if content is None:
            continue
        new_content, count = _BR_RE.subn("", content)
        if not count:
            continue
        stats.br_removed += count
        if write and new_content != content:
            _write_text(html_path, new_content)


def _clear_empty_paragraphs(book_root, *, write, stats):
    text_stats, _samples = clean_book(book_root, EMPTY_PARAGRAPH_OPTIONS, write=write)
    stats.empty_paragraphs_removed = text_stats.empty_paragraphs_removed
    stats.empty_documents_changed = text_stats.changed_documents
    stats.spine_documents = text_stats.documents


def optimize_book(
    book_root,
    *,
    mode=MODE_APPEND,
    template_css_map=None,
    remove_empty_paragraphs=True,
    remove_br=True,
    write=True,
):
    """对已解包的 EPUB 目录执行格式优化，返回 :class:`OptimizeStats`。

    Args:
        book_root: 已解包的 EPUB 根目录。
        mode: ``append`` 或 ``replace``。
        template_css_map: replace 模式所需的 ``{文件名: 字节}``；append 忽略。
        remove_empty_paragraphs: 是否删除 spine 正文空段（复用 clean_book）。
        remove_br: 是否删除 HTML 中的 ``<br>``。
        write: False 时只统计不落盘，用于预览。
    """
    if mode not in MODE_VALUES:
        raise EpubOptimizeError(f"未知的优化模式：{mode}")

    book_root = Path(book_root)
    if not book_root.is_dir():
        raise EpubOptimizeError(f"解包目录不存在：{book_root}")

    if mode == MODE_REPLACE and template_css_map is None:
        raise EpubOptimizeError("替换模式需要模板样式表")

    stats = OptimizeStats(mode=mode)
    css_files = list(_iter_css_files(book_root))
    html_files = list(_iter_html_files(book_root))

    if mode == MODE_APPEND:
        if css_files:
            _append_compact_css(css_files, write=write, stats=stats)
        else:
            content_root = _find_content_root(book_root, write=write)
            _create_fallback_compact_css(
                book_root, content_root, html_files, write=write, stats=stats
            )
            if not html_files:
                stats.warnings.append("未找到 HTML 正文，仅新建了样式表")
    else:
        content_root = _find_content_root(book_root, write=write)
        if template_css_map is not None and not template_css_map:
            stats.warnings.append("模板 EPUB 中没有样式表")
        _replace_css_and_inject_html(
            book_root,
            content_root,
            html_files,
            template_css_map or {},
            write=write,
            stats=stats,
        )
        if not html_files:
            stats.warnings.append("未找到 HTML 文档")

    if remove_empty_paragraphs:
        try:
            _clear_empty_paragraphs(book_root, write=write, stats=stats)
        except EpubIOError as exc:
            stats.warnings.append(str(exc))

    if remove_br and html_files:
        _remove_br_tags(html_files, write=write, stats=stats)

    return stats


def render_preview_html(stats):
    """把统计渲染成一段简单 HTML，供「预览效果」接口返回。"""

    def row(label, value):
        return f"<tr><td>{escape(label)}</td><td>{escape(str(value))}</td></tr>"

    mode_label = "追加紧凑规则" if stats.mode == MODE_APPEND else "替换模板样式"
    rows = [
        row("模式", mode_label),
        row("样式表", stats.stylesheets),
    ]
    if stats.mode == MODE_APPEND:
        if stats.css_created:
            rows.extend(
                [
                    row("新建样式表", stats.css_created),
                    row("链接到正文", stats.html_updated),
                    row("更新 OPF 清单", "是" if stats.opf_updated else "否"),
                ]
            )
        else:
            rows.extend(
                [
                    row("将写入 / 已写入", stats.css_appended),
                    row("已含规则（跳过）", stats.css_skipped),
                ]
            )
    else:
        rows.extend(
            [
                row("应用模板样式表", stats.css_written),
                row("更新的正文文档", stats.html_updated),
                row("更新 OPF 清单", "是" if stats.opf_updated else "否"),
            ]
        )
    rows.extend(
        [
            row("删除空段", stats.empty_paragraphs_removed),
            row("涉及正文文档", stats.empty_documents_changed),
            row("删除 &lt;br&gt;", stats.br_removed),
        ]
    )
    warning_html = ""
    if stats.warnings:
        items = "".join(f"<li>{escape(item)}</li>" for item in stats.warnings)
        warning_html = f"<p><strong>提示</strong></p><ul>{items}</ul>"

    return (
        "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        "<title>格式优化预览</title>"
        "<style>"
        "body{font-family:system-ui,sans-serif;margin:16px;color:#20242a}"
        "table{border-collapse:collapse;width:100%;max-width:480px}"
        "td{border:1px solid #e1e5ea;padding:6px 10px;font-size:14px}"
        "td:first-child{background:#fafbfc;width:40%}"
        "</style></head><body>"
        f"<p>{escape(stats.summary())}</p>"
        f"<table>{''.join(rows)}</table>"
        f"{warning_html}"
        "</body></html>"
    )
