"""数据集市业务规则：状态流转、字段校验与筛选口径都收在这里。"""
from __future__ import annotations

from typing import Any

from app import store as store_module
from app.services.experiment_stat import experiment_stat_service

MODULE = "data_mart"
LIST_FIELDS = ["主题编号", "主题名称", "业务域", "事实表", "维度集合", "刷新周期", "数据量", "负责人"]
REQUIRED_FIELDS = ["主题编号", "主题名称", "业务域"]
STATUS_ORDER = ["规划中", "建设中", "已上线", "已下线"]
ACTION_RULES = {"启动建设": "建设中", "上线主题": "已上线", "下线主题": "已下线"}
NEGATIVE_ACTIONS = ["下线主题"]


class DataMartService:
    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store_module.store.rows(MODULE)
        for field, expected in (filters or {}).items():
            rows = [row for row in rows if expected.casefold() in str(row.get(field, "")).casefold()]
        if keyword:
            rows = [row for row in rows if keyword.casefold() in str(row.get("主题编号", "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store_module.store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store_module.store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in LIST_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store_module.store.find(MODULE, entry_id)
        if entry is None:
            return None, f"分析主题 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于数据集市可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"分析主题已{action}"

    # -- 实验统计主题（版本化） -------------------------------------------

    def list_experiment_stats(
        self, experiment_code: str | None = None
    ) -> list[dict[str, Any]]:
        """实验统计分析主题列表：每个 (实验, 观测日) 仅一条当前统计。

        重跑多次也不会重复列出 DATA-STAT-EXPE-001——集市投影按
        (主题编号, 观测日) upsert，这里再按统计ID去重兜底。
        """
        rows = store_module.store.rows(MODULE)
        seen: set[str] = set()
        items: list[dict[str, Any]] = []
        for row in rows:
            stat_id_value = row.get("统计ID")
            if stat_id_value is None:
                continue
            if experiment_code and row.get("实验编号") != experiment_code:
                continue
            if stat_id_value in seen:
                continue
            seen.add(stat_id_value)
            items.append(dict(row))
        items.sort(key=lambda row: (str(row.get("实验编号", "")), str(row.get("观测日", ""))))
        return items

    def get_experiment_stat(
        self, experiment_code: str, observation_date: str
    ) -> dict[str, Any] | None:
        """读取某实验某观测日的当前统计（含完整版本引用）。"""
        return experiment_stat_service.get_current(experiment_code, observation_date)

    def get_experiment_stat_version(
        self, experiment_code: str, observation_date: str, version: int
    ) -> dict[str, Any] | None:
        """按版本号回查历史统计，供已发布报表按原版复算。"""
        return experiment_stat_service.resolve_version(
            experiment_code, observation_date, version
        )

    def list_experiment_stat_versions(
        self, experiment_code: str, observation_date: str
    ) -> list[dict[str, Any]]:
        """列出同一观测日的全部统计版本（含已被同日修订替代的旧版本）。"""
        return experiment_stat_service.list_versions(experiment_code, observation_date)
