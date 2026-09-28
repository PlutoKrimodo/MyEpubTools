# -*- coding: utf-8 -*-
"""工具（Blueprint）自动发现与注册。

新增一个工具只需在 ``modules/`` 下放一个 ``.py`` 文件，并在其中提供两部分内容：

1. 工具元信息::

       TOOL = {
           "key": "notes",          # 必填，唯一标识，同时用于查找 blueprint
           "title": "注释处理",      # 必填，侧边栏显示名
           "url_prefix": "/notes",  # 必填，挂载路径
           "order": 20,             # 选填，侧边栏排序，默认 100
           "description": "...",    # 选填
       }

2. 一个 Blueprint，变量名可以是 ``bp``、``blueprint`` 或 ``<key>_bp``
   （模块内只有一个 Blueprint 时也可以省略命名约定）::

       bp = Blueprint("notes", __name__)

约定与保证：
    * 没有 ``TOOL`` 的模块会被跳过，因此 ``epub_io`` 这类公共模块不会被注册。
    * 单个模块导入失败、或声明了 TOOL 却找不到 Blueprint，都不会影响应用启动，
      只会在返回的 ``warnings`` 中记录一条说明。
"""
from __future__ import annotations

import importlib
import pkgutil
from dataclasses import dataclass

from flask import Blueprint

DEFAULT_ORDER = 100
BLUEPRINT_ATTRS = ("bp", "blueprint")


@dataclass(frozen=True)
class Tool:
    """一个可挂载到应用上的工具。"""

    key: str
    title: str
    url_prefix: str
    order: int
    description: str
    blueprint: Blueprint

    @property
    def url(self):
        """工具首页地址，供侧边栏使用。"""
        prefix = self.url_prefix.rstrip("/")
        return f"{prefix}/" if prefix else "/"

    def as_dict(self):
        return {
            "key": self.key,
            "title": self.title,
            "url": self.url,
            "order": self.order,
            "description": self.description,
        }


def _find_blueprint(module, key):
    for attr in BLUEPRINT_ATTRS + (f"{key}_bp",):
        candidate = getattr(module, attr, None)
        if isinstance(candidate, Blueprint):
            return candidate

    found = []
    seen = set()
    for value in vars(module).values():
        if isinstance(value, Blueprint) and id(value) not in seen:
            seen.add(id(value))
            found.append(value)
    return found[0] if len(found) == 1 else None


def _build_tool(module_name, module):
    """从模块中解析出 Tool；返回 (tool, warning)。"""
    meta = getattr(module, "TOOL", None)
    if not isinstance(meta, dict):
        return None, None

    key = str(meta.get("key") or module_name).strip()
    if not key:
        return None, f"模块 {module_name} 的 TOOL 缺少 key，已跳过"

    blueprint = _find_blueprint(module, key)
    if blueprint is None:
        return None, (
            f"模块 {module_name} 声明了 TOOL 但未找到 Blueprint"
            f"（可用变量名：bp / blueprint / {key}_bp），已跳过"
        )

    url_prefix = str(meta.get("url_prefix") or f"/{key}").strip()
    if not url_prefix.startswith("/"):
        url_prefix = f"/{url_prefix}"

    try:
        order = int(meta.get("order", DEFAULT_ORDER))
    except (TypeError, ValueError):
        order = DEFAULT_ORDER

    return Tool(
        key=key,
        title=str(meta.get("title") or key),
        url_prefix=url_prefix,
        order=order,
        description=str(meta.get("description") or ""),
        blueprint=blueprint,
    ), None


def discover(package="modules"):
    """扫描 ``package`` 并返回 ``(tools, warnings)``。

    ``tools`` 已按 ``order``、``key`` 排序。
    """
    tools = []
    warnings = []

    try:
        package_module = importlib.import_module(package)
    except ImportError as exc:
        return tools, [f"无法导入工具包 {package}：{exc}"]

    search_path = list(getattr(package_module, "__path__", None) or [])
    registered = {}

    for info in pkgutil.iter_modules(search_path):
        if info.name.startswith("_"):
            continue

        module_name = f"{package}.{info.name}"
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:  # noqa: BLE001 - 单个工具失败不应拖垮整个应用
            warnings.append(f"模块 {module_name} 导入失败，已跳过：{type(exc).__name__}: {exc}")
            continue

        tool, warning = _build_tool(info.name, module)
        if warning:
            warnings.append(warning)
        if tool is None:
            continue

        if tool.key in registered:
            warnings.append(
                f"工具 key 重复：{tool.key}（{module_name} 与 {registered[tool.key]}），已跳过"
            )
            continue

        registered[tool.key] = module_name
        tools.append(tool)

    tools.sort(key=lambda item: (item.order, item.key))
    return tools, warnings
