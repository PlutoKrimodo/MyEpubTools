# -*- coding: utf-8 -*-
"""样式表（CSS）读写公共层。

与其它公共层的分工：``epub_io`` 管归档、``epub_package`` 管 OPF/NCX、
``epub_text`` 管正文、本模块管**样式表**。

本模块**没有** ``TOOL`` 字典，属于公共模块，``registry.discover()`` 会自动跳过。

两类能力
--------
1. **安全清理**——只做语义上零风险的删除：
   * 空规则块（``.kaiti { }``，无论选择器是什么）
   * 逐字重复的 ``@font-face``
   * 选择器与声明块都重复的规则

   故意**不做**的两件事：删除「未被 HTML 引用」的规则（误删风险高）、
   合并多个样式表（结构性改动）。

2. **统一排版覆盖**——把行距 / 段首缩进 / 段间距 / 字号作为一段带标记的
   覆盖规则追加到「最后一个被链接的样式表」末尾，保证在层叠中胜出；
   重复运行会就地替换该区块，不会越积越多。

保真度
------
与 :mod:`modules.epub_text` 同样的原则：**没有实际改动的样式表不写回**，
保持逐字节不变；只有改动过的才重写。清理过程保留原有注释、缩进与换行
（用户选择的是「保留可读性」，不做压缩）。
"""
from __future__ import annotations

import posixpath
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import escape

from modules.epub_io import EpubIOError, decode_text

CSS_SUFFIX = ".css"

OVERRIDE_START = "/* === EPUB 工具箱：统一排版（重复运行会覆盖本区块）=== */"
OVERRIDE_END = "/* === EPUB 工具箱：统一排版 结束 === */"

_NO_STYLESHEET_MESSAGE = "EPUB 中找不到样式表文件，无法追加排版覆盖规则"

#: 排版取值允许的字符集——顺带挡掉 `{` `}` `;` 这类能破坏样式表的注入。
_VALUE_RE = re.compile(r"^[A-Za-z0-9%.,\-+*/()# ]+$")

_PARAM_LABELS = {
    "font_size": "正文字号",
    "line_height": "行距",
    "text_indent": "段首缩进",
    "paragraph_spacing": "段间距",
}


class EpubCssError(EpubIOError):
    """样式表读写失败。继承 ``EpubIOError``，便于工具统一兜底。"""


@dataclass(frozen=True)
class TypographyOptions:
    """统一排版参数；空字符串表示该项不动。"""

    font_size: str = ""
    line_height: str = ""
    text_indent: str = ""
    paragraph_spacing: str = ""

    @property
    def enabled(self):
        return any((self.font_size, self.line_height, self.text_indent, self.paragraph_spacing))


@dataclass
class CssStats:
    """样式表处理统计。"""

    stylesheets: int = 0
    changed_stylesheets: int = 0
    empty_rules_removed: int = 0
    duplicate_font_faces_removed: int = 0
    duplicate_rules_removed: int = 0
    repeated_family_kept: int = 0
    override_applied: bool = False
    override_target: str = ""
    changed_files: list = field(default_factory=list)
    removed_samples: list = field(default_factory=list)

    def as_dict(self):
        return {
            "stylesheets": self.stylesheets,
            "changed_stylesheets": self.changed_stylesheets,
            "empty_rules_removed": self.empty_rules_removed,
            "duplicate_font_faces_removed": self.duplicate_font_faces_removed,
            "duplicate_rules_removed": self.duplicate_rules_removed,
            "repeated_family_kept": self.repeated_family_kept,
            "override_applied": self.override_applied,
            "override_target": self.override_target,
            "changed_files": list(self.changed_files),
            "removed_samples": list(self.removed_samples),
        }


def stats_text(stats):
    """把统计压成一句中文说明。"""
    parts = []
    if stats.stylesheets:
        parts.append(f"检查 {stats.stylesheets} 个样式表，改了 {stats.changed_stylesheets} 个")
    if stats.empty_rules_removed:
        parts.append(f"删除空规则 {stats.empty_rules_removed} 条")
    if stats.duplicate_font_faces_removed:
        parts.append(f"删除重复 @font-face {stats.duplicate_font_faces_removed} 条")
    if stats.duplicate_rules_removed:
        parts.append(f"删除重复规则 {stats.duplicate_rules_removed} 条")
    if stats.repeated_family_kept:
        parts.append(
            f"保留同名字体 @font-face {stats.repeated_family_kept} 条（src 回退顺序有意义）"
        )
    if stats.override_applied:
        parts.append(f"排版覆盖规则已写入 {stats.override_target}")
    return "；".join(parts) if parts else "没有需要处理的内容。"


# --------------------------------------------------------------------------- #
# 扫描：切成「块」与「其余文本」，拼接可逐字节还原
# --------------------------------------------------------------------------- #
def _skip_string(text, index):
    quote = text[index]
    index += 1
    length = len(text)
    while index < length:
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char == quote:
            return index + 1
        index += 1
    return length


def _skip_comment(text, index):
    end = text.find("*/", index + 2)
    return len(text) if end < 0 else end + 2


def _find_matching_brace(text, open_index):
    """返回与 ``open_index`` 处 ``{`` 配对的 ``}`` 下标；未闭合返回 None。"""
    depth = 0
    index = open_index
    length = len(text)
    while index < length:
        char = text[index]
        if char == "/" and text.startswith("/*", index):
            index = _skip_comment(text, index)
            continue
        if char in "\"'":
            index = _skip_string(text, index)
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return None


def scan(text):
    """把样式表切成片段序列。

    片段有两种，拼接起来可逐字节还原原文：

    * ``("filler", 文本)``——空白、注释、``@import ...;`` 这类顶层语句
    * ``("block", prelude, body)``——``prelude`` 为 ``{`` 之前的原文，``body`` 为大括号内原文
    """
    items = []
    index = 0
    length = len(text)
    filler_start = 0

    while index < length:
        char = text[index]
        if char == "/" and text.startswith("/*", index):
            index = _skip_comment(text, index)
            continue
        if char in "\"'":
            index = _skip_string(text, index)
            continue
        if char == ";":
            # 顶层语句（@import / @charset 之类）到此结束
            items.append(("filler", text[filler_start:index + 1]))
            index += 1
            filler_start = index
            continue
        if char == "{":
            close = _find_matching_brace(text, index)
            if close is None:
                # 未闭合，当作普通字符，避免把文件截断
                index += 1
                continue
            items.append(("block", text[filler_start:index], text[index + 1:close]))
            index = close + 1
            filler_start = index
            continue
        index += 1

    if filler_start < length:
        items.append(("filler", text[filler_start:]))
    return items


def _split_prelude(prelude):
    """把 prelude 拆成 ``(前导空白与注释, 选择器本体)``。"""
    index = 0
    length = len(prelude)
    while index < length:
        char = prelude[index]
        if char.isspace():
            index += 1
            continue
        if prelude.startswith("/*", index):
            end = prelude.find("*/", index + 2)
            if end < 0:
                return prelude[:index], prelude[index:]
            index = end + 2
            continue
        break
    return prelude[:index], prelude[index:]


# --------------------------------------------------------------------------- #
# 归一化：用于判断「逐字重复」
# --------------------------------------------------------------------------- #
def _strip_comments(text):
    return re.sub(r"/\*.*?\*/", " ", text, flags=re.S)


def normalize_selector(text):
    return re.sub(r"\s+", " ", _strip_comments(text)).strip()


def normalize_body(text):
    """把声明块归一化：去注释、压空白、去标点两侧空白、去尾分号。"""
    text = _strip_comments(text)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s*([:;,])\s*", r"\1", text)
    return text.strip().rstrip(";")


def _font_family_of(body):
    match = re.search(r"font-family\s*:\s*([^;]+)", _strip_comments(body), re.I)
    return match.group(1).strip().strip("\"'").lower() if match else ""


# --------------------------------------------------------------------------- #
# 安全清理
# --------------------------------------------------------------------------- #
def clean_stylesheet(text):
    """对单个样式表做安全清理，返回 ``(新文本, 计数, 被删样本)``。

    没有任何实际改动时**原样返回入参**，调用方据此跳过写盘。
    """
    counters = {
        "empty_rules_removed": 0,
        "duplicate_font_faces_removed": 0,
        "duplicate_rules_removed": 0,
        "repeated_family_kept": 0,
    }
    samples = []
    if not text.strip():
        return text, counters, samples

    # 预先统计「同名字体出现多次」的条目数，用于解释为何 @font-face 数量没怎么降。
    families = Counter()
    for item in scan(text):
        if item[0] == "block":
            _head, selector = _split_prelude(item[1])
            if normalize_selector(selector).lower().startswith("@font-face"):
                families[_font_family_of(item[2])] += 1
    counters["repeated_family_kept"] = sum(count for count in families.values() if count > 1)

    result = []

    # 第一遍：删除空块（保留其前导空白与注释，维持可读性）
    for item in scan(text):
        if item[0] != "block":
            result.append(item)
            continue
        head, selector = _split_prelude(item[1])
        if not item[2].strip():
            # 连选择器都是空的 `{ }` 属于无效语法、同样无任何效果，一并删除
            counters["empty_rules_removed"] += 1
            if len(samples) < 6:
                samples.append(f"{normalize_selector(selector) or '(无选择器)'} {{ }}")
            if head:
                result.append(("filler", head))
            continue
        result.append(item)

    # 第二遍：删除逐字重复的 @font-face 与重复规则
    seen_font_faces = set()
    seen_rules = set()
    final = []
    for item in result:
        if item[0] != "block":
            final.append(item)
            continue
        head, selector = _split_prelude(item[1])
        name = normalize_selector(selector)
        if not name:
            final.append(item)
            continue

        lowered = name.lower()
        if lowered.startswith("@font-face"):
            key = normalize_body(item[2])
            if key in seen_font_faces:
                counters["duplicate_font_faces_removed"] += 1
                if len(samples) < 6:
                    samples.append(f"@font-face（重复）{key[:60]}")
                if head:
                    final.append(("filler", head))
                continue
            seen_font_faces.add(key)
            final.append(item)
            continue

        if lowered.startswith("@"):
            final.append(item)
            continue

        key = (name, normalize_body(item[2]))
        if key in seen_rules:
            counters["duplicate_rules_removed"] += 1
            if len(samples) < 6:
                samples.append(f"{name} {{ {key[1][:60]} }}")
            if head:
                final.append(("filler", head))
            continue
        seen_rules.add(key)
        final.append(item)

    if not any(
        counters[key]
        for key in ("empty_rules_removed", "duplicate_font_faces_removed", "duplicate_rules_removed")
    ):
        return text, counters, samples

    rebuilt = "".join(
        item[1] if item[0] == "filler" else item[1] + "{" + item[2] + "}"
        for item in final
    )
    return rebuilt, counters, samples


# --------------------------------------------------------------------------- #
# 统一排版覆盖
# --------------------------------------------------------------------------- #
def validate_value(name, value):
    """校验排版取值，顺带挡掉能破坏样式表的注入字符。"""
    text = str(value or "").strip()
    if not text:
        return ""
    if "/*" in text or "*/" in text or not _VALUE_RE.match(text):
        label = _PARAM_LABELS.get(name, name)
        raise EpubCssError(
            f"{label}的取值不合法：{text}（只允许数字、单位与常见 CSS 关键字）"
        )
    return text


def check_options(options):
    """提前校验所有排版参数，避免处理到一半才报错。"""
    return TypographyOptions(
        font_size=validate_value("font_size", options.font_size),
        line_height=validate_value("line_height", options.line_height),
        text_indent=validate_value("text_indent", options.text_indent),
        paragraph_spacing=validate_value("paragraph_spacing", options.paragraph_spacing),
    )


def build_override_block(options):
    """生成排版覆盖规则区块；所有参数都为空时返回空串。"""
    body_declarations = []
    paragraph_declarations = []
    if options.font_size:
        body_declarations.append(("font-size", options.font_size))
    if options.line_height:
        body_declarations.append(("line-height", options.line_height))
    if options.text_indent:
        paragraph_declarations.append(("text-indent", options.text_indent))
    if options.paragraph_spacing:
        paragraph_declarations.append(("margin-bottom", options.paragraph_spacing))

    if not body_declarations and not paragraph_declarations:
        return ""

    lines = [OVERRIDE_START]
    for selector, declarations in (("body", body_declarations), ("p", paragraph_declarations)):
        if not declarations:
            continue
        lines.append(f"{selector} {{")
        lines.extend(f"    {name}: {value};" for name, value in declarations)
        lines.append("}")
    lines.append(OVERRIDE_END)
    return "\n".join(lines) + "\n"


def find_override_block(text):
    """定位已存在的排版覆盖区块，返回 ``(起, 止)`` 或 None。

    结尾会一并吞掉紧跟区块的空白：区块自身以换行收尾，若不吞掉它，
    每替换一次就会多出一个空行。
    """
    start = text.find(OVERRIDE_START)
    if start < 0:
        return None
    end = text.find(OVERRIDE_END, start)
    if end < 0:
        return None
    end += len(OVERRIDE_END)
    while end < len(text) and text[end] in " \t\r\n":
        end += 1
    return start, end


def apply_override(text, options):
    """把排版覆盖规则写进样式表；重复运行就地替换，不会叠加。返回 ``(新文本, 是否变化)``。"""
    block = build_override_block(options)
    existing = find_override_block(text)

    if existing is not None:
        start, end = existing
        if not block:
            trimmed = text[:start].rstrip()
            new_text = f"{trimmed}\n" if trimmed else ""
        else:
            new_text = text[:start] + block + text[end:]
        return new_text, new_text != text

    if not block:
        return text, False

    separator = "" if not text or text.endswith("\n") else "\n"
    new_text = text + separator + block
    return new_text, True


# --------------------------------------------------------------------------- #
# 定位样式表
# --------------------------------------------------------------------------- #
_LINK_RE = re.compile(r"<link\b[^>]*>", re.I)
_HREF_RE = re.compile(r"""href\s*=\s*["']([^"']+)["']""", re.I)
_REL_RE = re.compile(r"""rel\s*=\s*["']([^"']*)["']""", re.I)
_CHARSET_RE = re.compile(r"""^(\s*)@charset\s+["'][^"']*["']\s*;""", re.I)


def all_stylesheets(book_root):
    """列出归档内所有 CSS 文件的相对路径（排序后）。"""
    book_root = Path(book_root)
    return sorted(
        path.relative_to(book_root).as_posix()
        for path in book_root.rglob("*")
        if path.is_file() and path.suffix.lower() == CSS_SUFFIX
    )


def linked_stylesheets(book_root):
    """按文档顺序收集被 ``<link>`` 引用的样式表，返回 ``(每文档的最后一张, 全部)``。"""
    book_root = Path(book_root)
    lasts = []
    everything = []
    for path in sorted(book_root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in (".html", ".htm", ".xhtml"):
            continue
        text = decode_text(path.read_bytes())
        directory = path.parent.relative_to(book_root).as_posix()
        hrefs = []
        for tag in _LINK_RE.findall(text):
            href_match = _HREF_RE.search(tag)
            if not href_match:
                continue
            rel_match = _REL_RE.search(tag)
            rel = rel_match.group(1).lower() if rel_match else ""
            href = href_match.group(1).strip()
            if "stylesheet" not in rel and not href.lower().split("?")[0].endswith(CSS_SUFFIX):
                continue
            target = posixpath.normpath(posixpath.join(directory, href.split("?")[0])).lstrip("./")
            if (book_root / target).is_file():
                hrefs.append(target)
        if hrefs:
            everything.extend(hrefs)
            lasts.append(hrefs[-1])  # 层叠中最后链接的优先级最高
    return lasts, everything


def resolve_override_target(book_root):
    """挑出「追加排版覆盖规则」的目标样式表。

    选所有文档里被**最后链接**次数最多的那一个——它在这本书的层叠中优先级最高，
    覆盖规则放这里最稳。找不到时回落到归档内任意一个样式表。
    """
    book_root = Path(book_root)
    lasts, everything = linked_stylesheets(book_root)

    if lasts:
        counter = Counter(lasts)
        best = max(counter.items(), key=lambda pair: (pair[1], -lasts.index(pair[0])))
        return best[0]

    candidates = all_stylesheets(book_root)
    if candidates:
        return candidates[-1]
    raise EpubCssError(_NO_STYLESHEET_MESSAGE)


def _fix_charset(text):
    """把 ``@charset`` 声明统一成 UTF-8，与实际写出的编码保持一致。"""
    match = _CHARSET_RE.match(text)
    if not match or "utf-8" in match.group(0).lower():
        return text
    return '@charset "UTF-8";' + text[match.end():]


# --------------------------------------------------------------------------- #
# 对外接口
# --------------------------------------------------------------------------- #
def process_book(book_root, clean=True, typography=None, write=True):
    """处理整本书的样式表，返回 ``(stats, samples, override_block)``。

    ``write=False`` 时只做「试用」，不落盘，用于预览。
    """
    book_root = Path(book_root)
    typography = typography or TypographyOptions()
    stats = CssStats()

    stylesheets = all_stylesheets(book_root)
    stats.stylesheets = len(stylesheets)

    samples = []
    for relative in stylesheets:
        path = book_root / relative
        source = decode_text(path.read_bytes())
        text = source

        if clean:
            text, counters, removed = clean_stylesheet(text)
            stats.empty_rules_removed += counters["empty_rules_removed"]
            stats.duplicate_font_faces_removed += counters["duplicate_font_faces_removed"]
            stats.duplicate_rules_removed += counters["duplicate_rules_removed"]
            stats.repeated_family_kept += counters["repeated_family_kept"]
            for sample in removed:
                if len(samples) < 8:
                    samples.append(f"{relative}: {sample}")

        if text != source:
            stats.changed_stylesheets += 1
            stats.changed_files.append(relative)
            if write:
                path.write_text(_fix_charset(text), encoding="utf-8")

    override_block = ""
    if typography.enabled:
        target = resolve_override_target(book_root)
        stats.override_target = target
        path = book_root / target
        source = decode_text(path.read_bytes())
        text, changed = apply_override(source, typography)
        override_block = build_override_block(typography)
        if changed:
            # 目标文件若已统计过改动，不要重复计数
            if target not in stats.changed_files:
                stats.changed_stylesheets += 1
                stats.changed_files.append(target)
            if write:
                path.write_text(_fix_charset(text), encoding="utf-8")
        stats.override_applied = bool(override_block)

    return stats, samples, override_block


# --------------------------------------------------------------------------- #
# 预览
# --------------------------------------------------------------------------- #
_PREVIEW_STYLE = """
body{margin:0;padding:20px;background:#fafbfc;color:#20242a;
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;
  font-size:15px;line-height:1.7}
h1{font-size:19px;margin:0 0 8px}
.summary{color:#68717d;font-size:13px;margin:0 0 18px}
.card{border:1px solid #e1e5ea;border-radius:8px;background:#fff;padding:16px;margin-bottom:14px}
.card h2{font-size:13px;margin:0 0 10px;color:#68717d;font-weight:600}
pre{margin:0;padding:12px;background:#fafbfc;border:1px solid #e1e5ea;border-radius:6px;
  font-family:Consolas,monospace;font-size:12px;line-height:1.6;overflow:auto;white-space:pre-wrap}
.mock{border:1px dashed #ccd2d9;border-radius:6px;padding:14px 18px}
.mock p{margin-top:0}
.empty{color:#77818c;font-size:13px}
"""


def render_preview(stats, samples, override_block, typography=None, text_stats=None):
    """把处理结果渲染成可直接塞进 iframe 的预览文档。

    ``text_stats`` 是 :func:`modules.epub_text.clean_book` 的统计结果，用于展示
    「清除空段」那一节；为 ``None`` 表示本次没有启用该能力。这里只读取它的
    ``documents`` / ``changed_documents`` / ``empty_paragraphs_removed`` /
    ``changed_files`` 四个属性，不依赖该类型本身。
    """
    blocks = [f'<p class="summary">{stats_text(stats)}</p>']

    if typography is not None and typography.enabled:
        style = []
        if typography.font_size:
            style.append(f"font-size:{typography.font_size}")
        if typography.line_height:
            style.append(f"line-height:{typography.line_height}")
        paragraph_style = []
        if typography.text_indent:
            paragraph_style.append(f"text-indent:{typography.text_indent}")
        if typography.paragraph_spacing:
            paragraph_style.append(f"margin-bottom:{typography.paragraph_spacing}")
        mock = (
            f'<div class="mock" style="{";".join(style)}">'
            f'<p style="{";".join(paragraph_style)}">第一段：用来示意行距、段首缩进与段间距的实际观感。'
            "这一段足够长，可以看到首行缩进与行与行之间的疏密。</p>"
            f'<p style="{";".join(paragraph_style)}">第二段：段间距体现在两段之间，缩进只作用于每段首行。</p>'
            "</div>"
        )
        blocks.append(
            '<section class="card"><h2>排版效果示意（仅示意参数，不含原书字体）</h2>'
            f"{mock}</section>"
        )

    if override_block:
        blocks.append(
            '<section class="card"><h2>将追加到 '
            f"{escape(stats.override_target)} 的覆盖规则</h2>"
            f"<pre>{escape(override_block)}</pre></section>"
        )

    if text_stats is not None:
        if text_stats.empty_paragraphs_removed:
            files = list(text_stats.changed_files[:8])
            more = "" if len(text_stats.changed_files) <= 8 else "及以上"
            blocks.append(
                '<section class="card"><h2>空段清除</h2>'
                f'<p class="empty">扫描 {text_stats.documents} 个正文文档，其中 '
                f"{text_stats.changed_documents} 个有改动，共删除空段 "
                f"{text_stats.empty_paragraphs_removed} 个{more}。</p>"
                f"<pre>{escape(chr(10).join(files))}</pre></section>"
            )
        else:
            blocks.append(
                '<section class="card"><h2>空段清除</h2>'
                f'<p class="empty">扫描 {text_stats.documents} 个正文文档，没有发现空段。</p>'
                "</section>"
            )

    if samples:
        blocks.append(
            '<section class="card"><h2>被清理的规则（最多列出 8 条）</h2>'
            f"<pre>{escape(chr(10).join(samples))}</pre></section>"
        )
    else:
        blocks.append(
            '<section class="card"><h2>被清理的规则</h2>'
            '<p class="empty">没有发现可安全清理的内容（空规则 / 重复 @font-face / 重复声明块）。</p>'
            "</section>"
        )

    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        f"<title>样式表处理预览</title><style>{_PREVIEW_STYLE}</style></head><body>"
        "<h1>样式表处理预览</h1>" + "".join(blocks) + "</body></html>"
    )
