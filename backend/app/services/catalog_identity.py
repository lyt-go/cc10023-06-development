"""租户训练字段的共享权限与列标识工具。

同一份训练配置里可能出现多个定义名称完全相同、但归属项目/计算口径不同的字段，
因此所有训练侧输出都不能只带字段名：列标识必须携带计算口径和定义版本。
"""
from __future__ import annotations

from typing import Any


def parse_projects(scope_text: Any) -> list[str]:
    """把「共享范围」字段按逗号/顿号/分号拆成项目名列表。"""
    if not scope_text:
        return []
    text = str(scope_text).replace("，", ",").replace("、", ",").replace(";", ",")
    return [part.strip() for part in text.split(",") if part.strip()]


def can_use_catalog_entry(entry: dict[str, Any], project: str) -> bool:
    """私有列只有归属项目能用；其他项目只能使用明确共享给自己的已准入字段。"""
    if entry.get("status") != "已准入":
        return False
    if entry.get("归属项目") == project:
        return True
    return project in parse_projects(entry.get("共享范围"))


def source_type_for(entry: dict[str, Any], project: str) -> str:
    """标注列来源：本项目私有 / 跨项目共享（并标明来源项目）。"""
    owner = entry.get("归属项目", "")
    if owner == project:
        return "本项目私有"
    return f"跨项目共享（来源：{owner}）"


def column_identity(entry: dict[str, Any]) -> dict[str, Any]:
    """生成带计算口径与定义版本的列标识，供数据集、模型登记等所有输出复用。"""
    return {
        "catalog_id": entry.get("id"),
        "目录编号": entry.get("目录编号"),
        "字段名称": entry.get("字段名称"),
        "归属项目": entry.get("归属项目"),
        "计算口径": entry.get("计算口径"),
        "定义版本": entry.get("定义版本"),
        "列标识": build_column_label(entry),
    }


def build_column_label(entry: dict[str, Any]) -> str:
    """字符串形式的唯一列标识：字段名|计算口径|定义版本|归属项目。

    同名定义在不同项目下口径和版本不同，拼入口径与版本即可区分；
    归属项目保留在尾部，便于项目负责人直接判断统计范围。
    """
    return "{name}|{caliber}|{version}|{owner}".format(
        name=entry.get("字段名称", ""),
        caliber=entry.get("计算口径", ""),
        version=entry.get("定义版本", ""),
        owner=entry.get("归属项目", ""),
    )


def entry_lineage(entry: dict[str, Any], project: str) -> dict[str, Any]:
    """训练时快照的列归属信息：后续对账与跨模型比较都以这份快照为准。"""
    return {
        **column_identity(entry),
        "数据来源": entry.get("数据来源"),
        "来源类型": source_type_for(entry, project),
        "字段类型": entry.get("字段类型"),
    }
