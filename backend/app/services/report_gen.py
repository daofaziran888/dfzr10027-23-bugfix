"""报表生成业务规则：状态流转、字段校验与筛选口径都收在这里。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app import store as store_module
from app.services.experiment_stat import experiment_stat_service

MODULE = "report_gen"
LIST_FIELDS = ["报表编号", "报表类型", "数据范围", "包含图表", "生成周期", "输出格式", "分发渠道", "下次生成"]
REQUIRED_FIELDS = ["报表编号", "报表类型", "数据范围"]
STATUS_ORDER = ["草稿", "待生成", "已生成", "已停用"]
ACTION_RULES = {"提交生成": "待生成", "确认分发": "已生成", "停用报表": "已停用"}
NEGATIVE_ACTIONS = ["停用报表"]

EXPERIMENT_REPORT_TABLE = "_experiment_reports"


class ReportGenService:
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
            rows = [row for row in rows if keyword.casefold() in str(row.get("报表编号", "")).casefold()]
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
            return None, f"分析报表 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于报表生成可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"分析报表已{action}"

    # -- 实验报表：引用数据集市统计版本 -----------------------------------

    def _reports(self) -> list[dict[str, Any]]:
        return store_module.store.rows(EXPERIMENT_REPORT_TABLE)

    def generate_experiment_report(
        self, experiment_codes: list[str] | None = None
    ) -> dict[str, Any]:
        """读取数据集市接口的完整统计版本生成实验报表。

        每个实验只取其当前版本一次并保留版本引用，重跑导致的集市重复投影
        不会被重复计入；同一实验同一观测日无论生成多少次报表，引用口径都可追溯。
        """
        stats = experiment_stat_service.summarize_current_for_report(experiment_codes)
        if not stats:
            return {"ok": False, "message": "数据集市中没有可用的实验统计版本"}

        table = self._reports()
        seq = (
            max(
                (int(r.get("id", r.get("seq", 0))) for r in table),
                default=0,
            )
            + 1
        )
        now = datetime.now().isoformat()
        report = {
            "id": seq,
            "seq": seq,
            "报表编号": f"RPT-EXPE-{seq:03d}",
            "报表类型": "实验日报",
            "生成时间": now,
            "实验数": len(stats),
            "items": stats,
            "references": [
                {
                    "实验编号": item["实验编号"],
                    "观测日": item["观测日"],
                    "stat_id": item["统计ID"],
                    "version": item["引用版本"],
                    "version_id": item["版本ID"],
                }
                for item in stats
            ],
        }
        table.append(report)
        return {"ok": True, "message": "实验报表已生成", "report": report}

    def list_experiment_reports(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self._reports()]

    def get_experiment_report(self, report_id: int) -> dict[str, Any] | None:
        for row in self._reports():
            if int(row.get("id", 0)) == report_id:
                return dict(row)
        return None

    def recompute_experiment_report(self, report_id: int) -> dict[str, Any]:
        """按报表生成时引用的统计版本原样复算转化率与提升量。

        即使引用版本随后被同日修订替代，复算仍走旧版本的样本快照；
        返回里同时给出当前版本号，便于识别报表实际引用的是不是最新版本。
        """
        report = self.get_experiment_report(report_id)
        if report is None:
            return {"ok": False, "message": f"实验报表 {report_id} 不存在"}

        recomputed: list[dict[str, Any]] = []
        for ref in report.get("references", []):
            resolved = experiment_stat_service.resolve_version(
                ref["实验编号"], ref["观测日"], int(ref["version"])
            )
            if resolved is None:
                recomputed.append({
                    "实验编号": ref["实验编号"],
                    "观测日": ref["观测日"],
                    "引用版本": ref["version"],
                    "missing": True,
                })
                continue
            payload = resolved["payload"]
            recomputed.append({
                "实验编号": ref["实验编号"],
                "观测日": ref["观测日"],
                "引用版本": ref["version"],
                "版本ID": ref["version_id"],
                "引用版本是否当前": resolved["is_current"],
                "当前版本": resolved["current_version"],
                "控制组转化率": payload.get("control_rate"),
                "实验组转化率": payload.get("treatment_rate"),
                "绝对提升": payload.get("absolute_lift"),
                "相对提升": payload.get("relative_lift"),
                "显著性": payload.get("significant_at_05"),
                "引用样本": resolved["samples"],
            })
        return {
            "ok": True,
            "message": "已按报表引用的原始统计版本复算",
            "报表编号": report["报表编号"],
            "items": recomputed,
        }
