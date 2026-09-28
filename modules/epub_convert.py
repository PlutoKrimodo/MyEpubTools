# -*- coding: utf-8 -*-
"""简繁转换公共层。

在简体 / 繁体之间转换整本书，分三块独立开关：

* **正文**——按 OPF 的 ``manifest`` + ``spine`` 定位，只改可见文本节点；
* **目录与导航**——NCX 的 ``<text>``（``navLabel`` / ``docTitle``）、EPUB3 导航文档、
  书内目录页；
* **元数据**——``dc:*`` 字段，并可同步 ``dc:language``。

与文本清洗的范围差异（刻意为之）
--------------------------------
:mod:`modules.epub_text` 把**所有** front matter（封面 / 目录 / 前言 / 序）都排除，
但简繁转换只排除**封面与目录页**：前言 / 序 / 附录是正文内容，理应一起转换。
差异的根源是两者目标不同——清洗是为了不破坏导航结构，转换是为了全书字形一致。

引擎与依赖
----------
转换由 OpenCC（``pip install opencc``）完成。简→繁并非字符一一对应，必须按词判定
（发→發/髮、干→幹/乾、后→後/后、里→裏/裡），纯字符映射表必然出错，因此不提供
「无依赖降级」。

``import opencc`` 放在函数内部**延迟导入**：若放在模块顶层，缺依赖时
``registry.discover()`` 会把导入失败当成「工具坏掉」而整个跳过，用户侧边栏里
根本看不到这个工具；延迟导入则能在页面上给出「该怎么装」的中文提示。

保真策略
--------
与 :mod:`modules.epub_text` 一致：**没有实际改动的文档不写回**，改过的文档才重写
并尽量还原原文的行尾风格与自闭合标签写法。
"""
from __future__ import annotations

import html
import posixpath
import re
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET

from bs4 import BeautifulSoup

from modules.epub_io import HTML_EXTS, EpubIOError, decode_text
from modules.epub_package import (
    FIELD_KEYS,
    NCX_MEDIA_TYPE,
    find_opf_path,
    read_metadata,
    rewrite_metadata_block,
    sync_titles,
)
from modules.epub_text import map_text_nodes

MISSING_ENGINE_MESSAGE = (
    "缺少简繁转换引擎 OpenCC。请先安装后重启服务："
    "pip install opencc（本项目虚拟环境用 ./.venv/bin/pip install opencc）"
)
UNKNOWN_DIRECTION_MESSAGE = "未知的转换方向"
EMPTY_SCOPE_MESSAGE = "请至少选择一项转换范围（正文 / 目录与导航 / 元数据）"
EMPTY_BOOK_MESSAGE = "按 OPF 未找到可转换的文档，请确认这是结构完整的 EPUB"

#: 预览最多列出多少处改动，避免大书把预览页撑爆。
PREVIEW_PAIR_LIMIT = 40

#: 字形检测的取样规模（文档数 / 字符数）与判定阈值。
DETECT_DOCUMENT_LIMIT = 6
DETECT_CHAR_LIMIT = 20000
DETECT_MIN_CHARS = 40
DETECT_MIN_RATIO = 0.01


class EpubConvertError(EpubIOError):
    """简繁转换失败。继承 ``EpubIOError``，便于工具统一兜底。"""


# --------------------------------------------------------------------------- #
# 转换方向
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Direction:
    """一个转换方向：表单值、显示名、OpenCC 配置名、目标语言标记。"""

    value: str
    label: str
    config: str
    language: str
    target: str

    @property
    def script_label(self):
        return "繁体" if self.target == "traditional" else "简体"

    def as_dict(self):
        return {"value": self.value, "label": self.label}


DIRECTIONS = (
    Direction("s2t", "简体 → 繁体（通用）", "s2t", "zh-Hant", "traditional"),
    Direction("s2tw", "简体 → 台湾正体（仅字形）", "s2tw", "zh-Hant", "traditional"),
    Direction("s2twp", "简体 → 台湾正体（含台湾用词）", "s2twp", "zh-Hant", "traditional"),
    Direction("s2hk", "简体 → 香港繁体", "s2hk", "zh-Hant", "traditional"),
    Direction("t2s", "繁体 → 简体（通用）", "t2s", "zh-Hans", "simplified"),
    Direction("tw2s", "台湾正体 → 简体", "tw2s", "zh-Hans", "simplified"),
    Direction("hk2s", "香港繁体 → 简体", "hk2s", "zh-Hans", "simplified"),
)

DIRECTION_BY_VALUE = {item.value: item for item in DIRECTIONS}

DEFAULT_DIRECTION = "s2t"

METADATA_LABELS = {
    "title": "书名",
    "creator": "作者",
    "language": "语言",
    "identifier": "标识符",
    "publisher": "出版社",
    "date": "出版日期",
    "description": "简介",
    "subject": "主题标签",
}


# --------------------------------------------------------------------------- #
# 引擎
# --------------------------------------------------------------------------- #
#: OpenCC 构造词典有开销，按配置名缓存复用。
_ENGINES = {}


def _opencc():
    try:
        import opencc  # noqa: PLC0415 - 故意延迟导入，缺依赖时不影响应用启动
    except ImportError as exc:
        raise EpubConvertError(MISSING_ENGINE_MESSAGE) from exc
    return opencc


def get_engine(config):
    """按 OpenCC 配置名取转换器实例。

    ``_opencc()`` 在缺依赖时抛 ``EpubConvertError``，这里**不捕获**它，
    以免「缺依赖」被误包装成「初始化失败」。
    """
    engine = _ENGINES.get(config)
    if engine is None:
        module = _opencc()
        try:
            engine = module.OpenCC(config)
        except Exception as exc:  # noqa: BLE001 - 配置名非法等情况统一转成中文提示
            raise EpubConvertError(f"初始化 OpenCC（{config}）失败：{exc}") from exc
        _ENGINES[config] = engine
    return engine


def convert_text(text, config=DEFAULT_DIRECTION):
    """按配置名转换一段文本。"""
    return get_engine(config).convert(str(text))


def _changed_char_count(before, after):
    """估算变化的字符数：逐位比较的差异数 + 长度差。

    简繁转换绝大多数是等长替换，少数会因词组转换改变长度，两种都计入。
    """
    common = min(len(before), len(after))
    diff = sum(1 for index in range(common) if before[index] != after[index])
    return diff + abs(len(before) - len(after))


# --------------------------------------------------------------------------- #
# 选项与统计
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ConvertOptions:
    """一次转换的开关组合。"""

    direction: str = DEFAULT_DIRECTION
    scope_body: bool = True
    scope_navigation: bool = True
    scope_metadata: bool = True
    update_language: bool = True


@dataclass
class ConvertStats:
    """转换统计。"""

    direction: str = ""
    documents: int = 0
    changed_documents: int = 0
    changed_blocks: int = 0
    changed_chars: int = 0
    ncx_files: int = 0
    ncx_entries: int = 0
    cover_skipped: int = 0
    metadata_changes: int = 0
    synced_titles: list = field(default_factory=list)
    changed_files: list = field(default_factory=list)

    def as_dict(self):
        return {
            "direction": self.direction,
            "documents": self.documents,
            "changed_documents": self.changed_documents,
            "changed_blocks": self.changed_blocks,
            "changed_chars": self.changed_chars,
            "ncx_files": self.ncx_files,
            "ncx_entries": self.ncx_entries,
            "cover_skipped": self.cover_skipped,
            "metadata_changes": self.metadata_changes,
            "synced_titles": list(self.synced_titles),
            "changed_files": list(self.changed_files),
        }


@dataclass(frozen=True)
class MetadataChange:
    """一处元数据改动。"""

    key: str
    label: str
    before: str
    after: str


@dataclass(frozen=True)
class ConvertPreview:
    """预览数据：元数据改动 + 逐处文本对照。"""

    direction: Direction
    metadata_changes: tuple = ()
    documents: tuple = ()


class _Collector:
    """把引擎包一层，顺手统计字符数并采集预览样本。"""

    def __init__(self, engine):
        self._engine = engine
        self.chars = 0
        self.samples = []
        self._relative = ""

    def begin(self, relative):
        self._relative = relative
        return self

    def __call__(self, text):
        new_text = self._engine.convert(text)
        if new_text == text:
            return text
        self.chars += _changed_char_count(text, new_text)
        if len(self.samples) < PREVIEW_PAIR_LIMIT:
            self.samples.append((self._relative, text, new_text))
        return new_text


# --------------------------------------------------------------------------- #
# 文档分类
# --------------------------------------------------------------------------- #
#: 文件名 / manifest id 里出现这些「整词」时视为目录或导航文档。
_NAVIGATION_TOKENS = frozenset({"toc", "contents", "nav", "navigation"})
#: 同上，视为封面。
_COVER_TOKENS = frozenset({"cover"})
_TOKEN_SPLIT_RE = re.compile(r"[-_.\s]+")


@dataclass(frozen=True)
class DocumentGroups:
    """书籍文档分类结果，元素均为相对 ``book_root`` 的 posix 路径。"""

    body: tuple = ()
    navigation: tuple = ()
    cover: tuple = ()
    ncx: tuple = ()


def _local_name(tag):
    """取 ``{uri}item`` / ``opf:item`` 的本地名 ``item``。"""
    name = str(tag)
    if "}" in name:
        name = name.rsplit("}", 1)[-1]
    if ":" in name:
        name = name.rsplit(":", 1)[-1]
    return name


def classify_documents(book_root):
    """把书里的 XHTML 与 NCX 分成「正文 / 目录导航 / 封面」。

    分类口径与 :func:`modules.epub_package.spine_documents` 一致，只看 ``manifest``
    的 ``properties``、``<guide>`` 的 ``type``，以及 id / 文件名里的**整词**。
    """
    book_root = Path(book_root)
    opf_path = find_opf_path(book_root)
    raw = decode_text((book_root / opf_path).read_bytes())
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise EpubConvertError(f"OPF 不是合法的 XML：{exc}") from exc
    opf_dir = posixpath.dirname(opf_path)

    manifest = {}
    ncx = []
    for element in root.iter():
        if _local_name(element.tag) != "item":
            continue
        item_id = (element.get("id") or "").strip()
        href = (element.get("href") or "").strip()
        if not item_id or not href:
            continue
        media_type = (element.get("media-type") or "").strip().lower()
        manifest[item_id] = {
            "id": item_id,
            "href": href,
            "properties": (element.get("properties") or "").lower().split(),
            "media_type": media_type,
        }
        if media_type == NCX_MEDIA_TYPE:
            ncx.append(posixpath.normpath(posixpath.join(opf_dir, href)).lstrip("./"))

    # NCX 也可能没有在 manifest 里声明，回落为扫描 *.ncx。
    if not ncx:
        ncx = [path.relative_to(book_root).as_posix() for path in sorted(book_root.rglob("*.ncx"))]

    guide_types = {}
    for element in root.iter():
        if _local_name(element.tag) != "reference":
            continue
        href = (element.get("href") or "").strip()
        if href:
            guide_types[posixpath.normpath(href.split("#")[0])] = (element.get("type") or "").lower()

    body, navigation, cover = [], [], []
    seen = set()
    for element in root.iter():
        if _local_name(element.tag) != "itemref":
            continue
        item = manifest.get((element.get("idref") or "").strip())
        if item is None:
            continue

        relative = posixpath.normpath(posixpath.join(opf_dir, item["href"])).lstrip("./")
        target = book_root / relative
        if relative in seen or not target.is_file() or target.suffix.lower() not in HTML_EXTS:
            continue
        seen.add(relative)

        stem = posixpath.splitext(posixpath.basename(item["href"]))[0]
        tokens = set(_TOKEN_SPLIT_RE.split(f"{item['id']} {stem}".lower()))
        reference_type = guide_types.get(posixpath.normpath(item["href"]), "")

        if "nav" in item["properties"] or tokens & _NAVIGATION_TOKENS or "toc" in reference_type:
            navigation.append(relative)
        elif tokens & _COVER_TOKENS or "cover" in reference_type:
            cover.append(relative)
        else:
            body.append(relative)

    return DocumentGroups(
        body=tuple(body),
        navigation=tuple(navigation),
        cover=tuple(cover),
        ncx=tuple(ncx),
    )


# --------------------------------------------------------------------------- #
# NCX
# --------------------------------------------------------------------------- #
_NCX_TEXT_RE = re.compile(r"(<text\b[^>]*>)(.*?)(</text>)", re.S | re.I)


def convert_ncx_text(raw, engine):
    """转换 NCX 里所有 ``<text>`` 的内容（``navLabel`` / ``docTitle``）。

    刻意**不做 unescape / escape 往返**：NCX 文本里的实体（``&amp;`` 之类）都是
    ASCII，OpenCC 本来就不会改写它们；而往返一次反而可能改变原文的实体写法。
    """
    changed = 0

    def replace(match):
        nonlocal changed
        original = match.group(2)
        converted = engine.convert(original)
        if converted == original:
            return match.group(0)
        changed += 1
        return match.group(1) + converted + match.group(3)

    return _NCX_TEXT_RE.sub(replace, raw), changed


def _convert_ncx_files(book_root, ncx_paths, engine, write):
    files = 0
    entries = 0
    for relative in ncx_paths:
        path = book_root / relative
        if not path.is_file():
            continue
        raw = decode_text(path.read_bytes())
        new_raw, changed = convert_ncx_text(raw, engine)
        if not changed:
            continue
        files += 1
        entries += changed
        if write:
            path.write_text(new_raw, encoding="utf-8")
    return files, entries


# --------------------------------------------------------------------------- #
# 元数据
# --------------------------------------------------------------------------- #
def _target_language(current, direction):
    """目标 ``dc:language``：非中文书籍不动，中文书籍按目标字形改写。"""
    if current and not current.lower().startswith("zh"):
        return ""
    return direction.language


def _apply_metadata(book_root, engine, options, direction, write):
    """转换 ``dc:*`` 元数据，返回 ``(opf 路径, 改动元组, 新的书名)``。"""
    snapshot = read_metadata(book_root)
    values = dict(snapshot.values)
    changes = []

    for key in FIELD_KEYS:
        value = str(values.get(key) or "").strip()
        if not value:
            continue
        converted = engine.convert(value)
        if converted == value:
            continue
        changes.append(MetadataChange(key, METADATA_LABELS.get(key, key), value, converted))
        values[key] = converted

    if options.update_language:
        current = str(values.get("language") or "").strip()
        target = _target_language(current, direction)
        if target and target != current:
            changes.append(
                MetadataChange("language", METADATA_LABELS["language"], current or "（未设置）", target)
            )
            values["language"] = target

    if not changes:
        return snapshot.opf_path, (), ""

    # 用 rewrite_metadata_block 而不是 write_metadata，才能支持「预览不落盘」。
    opf_file = book_root / snapshot.opf_path
    raw = decode_text(opf_file.read_bytes())
    new_raw = rewrite_metadata_block(raw, values)
    if write and new_raw != raw:
        opf_file.write_text(new_raw, encoding="utf-8")

    title_changed = any(change.key == "title" for change in changes)
    return snapshot.opf_path, tuple(changes), str(values.get("title") or "") if title_changed else ""


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def convert_book(book_root, options, write=True):
    """按 ``options`` 转换整本书，返回 ``(stats, preview)``。

    ``write=False`` 时只做试跑，不落盘，用于预览。
    """
    book_root = Path(book_root)
    direction = DIRECTION_BY_VALUE.get(options.direction)
    if direction is None:
        raise EpubConvertError(f"{UNKNOWN_DIRECTION_MESSAGE}：{options.direction}")
    if not (options.scope_body or options.scope_navigation or options.scope_metadata):
        raise EpubConvertError(EMPTY_SCOPE_MESSAGE)

    engine = get_engine(direction.config)
    groups = classify_documents(book_root)
    if not (groups.body or groups.navigation or groups.ncx):
        raise EpubConvertError(EMPTY_BOOK_MESSAGE)

    stats = ConvertStats(direction=direction.value, cover_skipped=len(groups.cover))
    collector = _Collector(engine)

    targets = []
    if options.scope_body:
        targets.extend(groups.body)
    if options.scope_navigation:
        targets.extend(groups.navigation)

    for relative in targets:
        path = book_root / relative
        if not path.is_file():
            continue
        source = decode_text(path.read_bytes())
        output, blocks = map_text_nodes(source, collector.begin(relative))

        stats.documents += 1
        if output == source:
            continue

        stats.changed_documents += 1
        stats.changed_blocks += blocks
        stats.changed_files.append(relative)
        if write:
            path.write_text(output, encoding="utf-8")

    stats.changed_chars = collector.chars

    if options.scope_navigation:
        stats.ncx_files, stats.ncx_entries = _convert_ncx_files(
            book_root, groups.ncx, engine, write
        )

    metadata_changes = ()
    if options.scope_metadata:
        _opf_path, metadata_changes, new_title = _apply_metadata(
            book_root, engine, options, direction, write
        )
        stats.metadata_changes = len(metadata_changes)
        if write and new_title:
            # 书名变了就把目录标题同步过去，避免阅读器目录与书名不一致。
            stats.synced_titles = sync_titles(book_root, new_title)

    preview = ConvertPreview(
        direction=direction,
        metadata_changes=metadata_changes,
        documents=tuple(collector.samples),
    )
    return stats, preview


def stats_text(stats):
    """把统计压成一句中文说明。"""
    parts = []
    if stats.documents:
        parts.append(f"扫描 {stats.documents} 个文档，其中 {stats.changed_documents} 个有改动")
    if stats.changed_blocks:
        parts.append(f"转换 {stats.changed_blocks} 处文本、约 {stats.changed_chars} 字")
    if stats.ncx_files:
        parts.append(f"目录更新 {stats.ncx_files} 个文件、{stats.ncx_entries} 条标题")
    if stats.metadata_changes:
        parts.append(f"元数据更新 {stats.metadata_changes} 项")
    if stats.synced_titles:
        parts.append("书名已同步至 " + "、".join(stats.synced_titles))
    if stats.cover_skipped:
        parts.append(f"跳过封面页 {stats.cover_skipped} 个")
    return "；".join(parts) + "。" if parts else "没有需要转换的内容。"


# --------------------------------------------------------------------------- #
# 字形检测
# --------------------------------------------------------------------------- #
def _sample_text(book_root, documents):
    """取若干正文文档的纯文本，用于字形检测。"""
    chunks = []
    total = 0
    for relative in documents[:DETECT_DOCUMENT_LIMIT]:
        path = book_root / relative
        if not path.is_file():
            continue
        soup = BeautifulSoup(decode_text(path.read_bytes()), "html.parser")
        for tag in soup(["script", "style"]):
            tag.decompose()
        text = soup.get_text("", strip=True)
        if not text:
            continue
        chunks.append(text)
        total += len(text)
        if total >= DETECT_CHAR_LIMIT:
            break
    return "".join(chunks)[:DETECT_CHAR_LIMIT]


def _change_ratio(engine, sample):
    return _changed_char_count(sample, engine.convert(sample)) / max(len(sample), 1)


def detect_direction(book_root):
    """粗判原书字形，返回 ``(推荐方向值, 中文说明)``；判断不了时方向为空串。

    做法是拿正文样本做双向试转：简体文本用 ``s2t`` 改动明显、用 ``t2s`` 几乎不变，
    繁体文本正好相反。这是**启发式**，只用于给页面做默认预选，不参与转换本身。
    """
    book_root = Path(book_root)
    groups = classify_documents(book_root)
    sample = _sample_text(book_root, groups.body + groups.navigation)
    if len(sample) < DETECT_MIN_CHARS:
        return "", "正文文本太少，无法自动判断字形，请手动选择转换方向。"

    to_traditional = _change_ratio(get_engine("s2t"), sample)
    to_simplified = _change_ratio(get_engine("t2s"), sample)

    if to_traditional - to_simplified > DETECT_MIN_RATIO:
        return "s2t", (
            f"检测到正文以简体为主（试转繁体改动 {to_traditional:.1%}，反向仅 "
            f"{to_simplified:.1%}），已预选「简体 → 繁体（通用）」。这是自动判断，可手动更改。"
        )
    if to_simplified - to_traditional > DETECT_MIN_RATIO:
        return "t2s", (
            f"检测到正文以繁体为主（试转简体改动 {to_simplified:.1%}，反向仅 "
            f"{to_traditional:.1%}），已预选「繁体 → 简体（通用）」。这是自动判断，可手动更改。"
        )
    return "", (
        f"简体与繁体特征都不明显（双向试转改动分别为 {to_traditional:.1%} / "
        f"{to_simplified:.1%}），可能已是目标字形或非中文书籍，请手动选择转换方向。"
    )


# --------------------------------------------------------------------------- #
# 预览
# --------------------------------------------------------------------------- #
_PREVIEW_STYLE = """
body{margin:0;padding:20px;background:#fafbfc;color:#20242a;
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;
  font-size:15px;line-height:1.7}
h1{font-size:19px;margin:0 0 8px}
h2{font-size:14px;margin:22px 0 10px;color:#3b4149}
.summary{color:#68717d;font-size:13px;margin:0 0 6px}
.hint{color:#8a929c;font-size:12px;margin:8px 0 0}
.empty{color:#8a929c;font-size:13px;background:#fff;border:1px solid #e1e5ea;
  border-radius:8px;padding:14px;margin:0}
table{border-collapse:collapse;width:100%;background:#fff;
  border:1px solid #e1e5ea;border-radius:8px;overflow:hidden;font-size:14px}
th,td{padding:8px 12px;text-align:left;border-bottom:1px solid #f0f2f4}
th{background:#f6f8fa;font-size:12px;color:#68717d;font-weight:600}
td.k{color:#68717d;white-space:nowrap}
td.b{color:#8a929c}
td.a{color:#1c6b3a}
.doc{border:1px solid #e1e5ea;border-radius:8px;background:#fff;margin-bottom:12px;overflow:hidden}
.doc>h3{font-size:12px;margin:0;padding:8px 12px;background:#f6f8fa;color:#68717d;
  font-weight:600;word-break:break-all;border-bottom:1px solid #e1e5ea}
.pair{display:grid;grid-template-columns:1fr 1fr;border-bottom:1px solid #f0f2f4}
.pair:last-child{border-bottom:none}
.pair>div{padding:8px 12px;font-size:14px}
.pair .old{color:#8a929c;border-right:1px solid #f0f2f4}
.pair .new{color:#1c6b3a;background:#fbfefc}
"""


def render_preview(stats, preview):
    """把转换结果渲染成可直接塞进 iframe 的对照预览。"""
    parts = [
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>简繁转换预览</title>',
        f"<style>{_PREVIEW_STYLE}</style></head><body>",
        f"<h1>简繁转换预览 · {html.escape(preview.direction.label)}</h1>",
        f'<p class="summary">{html.escape(stats_text(stats))}</p>',
    ]

    if preview.metadata_changes:
        rows = "".join(
            "<tr><td class='k'>{}</td><td class='b'>{}</td><td class='a'>{}</td></tr>".format(
                html.escape(change.label),
                html.escape(change.before),
                html.escape(change.after),
            )
            for change in preview.metadata_changes
        )
        parts.append(
            "<h2>元数据</h2><table>"
            "<tr><th>字段</th><th>转换前</th><th>转换后</th></tr>" + rows + "</table>"
        )

    grouped = {}
    for relative, before, after in preview.documents:
        grouped.setdefault(relative, []).append((before, after))

    if grouped:
        parts.append("<h2>正文与目录（逐处对照）</h2>")
        for relative, pairs in grouped.items():
            rows = "".join(
                f'<div class="pair"><div class="old">{html.escape(before)}</div>'
                f'<div class="new">{html.escape(after)}</div></div>'
                for before, after in pairs
            )
            parts.append(
                f'<section class="doc"><h3>{html.escape(relative)}</h3>{rows}</section>'
            )
        parts.append('<p class="hint">仅列出前若干处改动；导出时会转换全书。</p>')
    elif not preview.metadata_changes:
        parts.append(
            '<p class="empty">没有检测到需要转换的内容——这本书可能已是目标字形，'
            "或所选范围内没有可转换的文本。</p>"
        )

    parts.append("</body></html>")
    return "".join(parts)
