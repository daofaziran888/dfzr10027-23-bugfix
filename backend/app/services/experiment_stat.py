"""实验统计版本管理：按「实验 + 样本观测日」维护唯一当前统计，并保留全部历史版本。

设计要点
--------
- 统计主键是 (实验编号, 样本观测日)，而不是运行时刻。跨午夜补跑或同一样本重试，
  只要观测日不变，就仍归属同一条统计，不会因为重跑产生第二条集市行。
- 同一观测日的修订不会覆盖旧版本：每次内容变化都追加一个新版本，
  老版本转为「已被替代」并记录替代它的新版本号；已经引用老版本的实验报表
  仍可按原版本复算。
- 数据集市列表里的统计主题行按 (主题编号, 观测日) 做 upsert，
  永远只有一条「当前」投影，重跑不会让 DATA-STAT-EXPE-001 重复出现。
"""
from __future__ import annotations

import threading
from datetime import date
from typing import Any

from app import store as store_module

# 独立命名空间，避免与 data_mart 主题 CRUD 行混在一起
VERSION_TABLE = "_experiment_stat_versions"
# 数据集市里的统计主题投影行（当前版本），重跑时原地更新而非追加
MART_TABLE = "data_mart"

_lock = threading.RLock()


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------

def _versions() -> list[dict[str, Any]]:
    # 经模块属性访问，保证测试里 reset_store() 替换单例后仍指向当前仓库
    return store_module.store.rows(VERSION_TABLE)


def stat_id(experiment_code: str, observation_date: str) -> str:
    """一条统计的稳定业务主键：实验 + 观测日。"""
    return f"STAT:{experiment_code}:{observation_date}"


def theme_code(experiment_code: str) -> str:
    return f"DATA-STAT-{experiment_code}"


def version_id(experiment_code: str, observation_date: str, version: int) -> str:
    return f"VER:{experiment_code}:{observation_date}:v{version}"


def _snapshot_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """深拷贝一份发布载荷，避免调用方之后再改同一引用影响已发布版本。"""
    import copy

    return copy.deepcopy(payload)


def _build_mart_projection(stat: dict[str, Any], version: dict[str, Any]) -> dict[str, Any]:
    """把当前版本投影成数据集市列表可展示的统计主题行。"""
    payload = version["payload"]
    return {
        "主题编号": theme_code(stat["experiment_code"]),
        "主题名称": f"实验统计主题-{stat['experiment_code']}",
        "业务域": "增长域",
        "事实表": "fact_experiment_stat",
        "维度集合": "实验编号,观测日,分组",
        "刷新周期": "日",
        "数据量": str(
            int(payload.get("sample_size_control", 0))
            + int(payload.get("sample_size_treatment", 0))
        ),
        "负责人": "统计平台",
        "region": "all",
        "channel": "all",
        "日期": stat["observation_date"],
        "观测日": stat["observation_date"],
        "实验编号": stat["experiment_code"],
        "统计ID": stat["stat_id"],
        "统计版本": version["version"],
        "版本ID": version["version_id"],
        "是否当前": True,
        "控制组转化率": payload.get("control_rate"),
        "实验组转化率": payload.get("treatment_rate"),
        "绝对提升": payload.get("absolute_lift"),
        "相对提升": payload.get("relative_lift"),
        "显著性": payload.get("significant_at_05"),
        "发布时间": version.get("published_at"),
        "status": "已上线",
        "pending": False,
        "abnormal": False,
    }


def _upsert_mart_projection(stat: dict[str, Any], version: dict[str, Any]) -> dict[str, Any]:
    """集市投影按 (主题编号, 观测日) 唯一，重跑/修订都原地更新。"""
    rows = store_module.store.rows(MART_TABLE)
    target_code = theme_code(stat["experiment_code"])
    observation_date = stat["observation_date"]
    projection = _build_mart_projection(stat, version)

    for row in rows:
        row_obs = row.get("观测日") or row.get("日期")
        if (
            row.get("主题编号") == target_code
            and str(row_obs or "") == observation_date
            and row.get("统计ID") is not None
        ):
            kept_id = row.get("id")
            row.clear()
            row.update(projection)
            row["id"] = kept_id
            return row

    projection["id"] = max((int(row.get("id", 0)) for row in rows), default=0) + 1
    rows.append(projection)
    return projection


def _group_key(row: dict[str, Any]) -> tuple[str, str]:
    return str(row.get("实验编号", "")), str(row.get("分组", ""))


# ---------------------------------------------------------------------------
# 发布（幂等）与查询
# ---------------------------------------------------------------------------

class ExperimentStatService:
    """实验统计的版本仓库：发布、当前值、历史版本与集市投影都收敛到这里。"""

    def publish_stat(
        self,
        *,
        experiment_code: str,
        observation_date: str,
        payload: dict[str, Any],
        samples: list[dict[str, Any]] | None = None,
        source_run: str | None = None,
    ) -> dict[str, Any]:
        """发布一条实验统计。

        返回 ``{stat_id, version_id, version, status, replaced, current}``：

        - ``status="unchanged"``：同观测日重跑且内容没变，沿用原版本（幂等）；
        - ``status="revised"``：同观测日内容发生变化，追加新版本并把老版本标记替代；
        - ``status="created"``：该实验/观测日第一次发布。
        """
        observation_date = str(observation_date)
        sid = stat_id(experiment_code, observation_date)
        frozen_payload = _snapshot_payload(payload)
        frozen_samples = _snapshot_payload(list(samples or []))

        with _lock:
            stat = next(
                (row for row in _versions() if row.get("stat_id") == sid), None
            )
            now = datetime_now()

            if stat is None:
                version = self._make_version(
                    experiment_code, observation_date, 1,
                    frozen_payload, frozen_samples, now, source_run,
                )
                stat = {
                    "stat_id": sid,
                    "experiment_code": experiment_code,
                    "observation_date": observation_date,
                    "current_version": 1,
                    "created_at": now,
                    "updated_at": now,
                    "versions": [version],
                }
                _versions().append(stat)
                projection = _upsert_mart_projection(stat, version)
                return self._publish_result(stat, version, "created", False, projection)

            current = self._live_version_record(
                stat, stat["current_version"]
            )
            if current is not None and self._same_content(current["payload"], frozen_payload):
                # 同观测日、同内容：重跑/重试不产生新版本，也不产生新集市行
                current["last_run_at"] = now
                if source_run:
                    current.setdefault("run_hits", []).append(source_run)
                stat["updated_at"] = now
                projection = _upsert_mart_projection(stat, current)
                return self._publish_result(stat, current, "unchanged", False, projection)

            next_version = int(stat["current_version"]) + 1
            new_record = self._make_version(
                experiment_code, observation_date, next_version,
                frozen_payload, frozen_samples, now, source_run,
            )
            if current is not None:
                current["is_current"] = False
                current["superseded_by"] = new_record["version_id"]
                current["superseded_at"] = now
            stat["versions"].append(new_record)
            stat["current_version"] = next_version
            stat["updated_at"] = now
            projection = _upsert_mart_projection(stat, new_record)
            return self._publish_result(stat, new_record, "revised", True, projection)

    # -- 查询 -------------------------------------------------------------

    def get_stat(self, experiment_code: str, observation_date: str) -> dict[str, Any] | None:
        sid = stat_id(experiment_code, str(observation_date))
        return next(
            (dict(row) for row in _versions() if row.get("stat_id") == sid), None
        )

    def get_current(
        self, experiment_code: str, observation_date: str | None = None
    ) -> dict[str, Any] | None:
        """取当前版本；不传观测日时取该实验最近一个观测日的当前统计。"""
        if observation_date is not None:
            stat = self.get_stat(experiment_code, str(observation_date))
            if stat is None:
                return None
            record = self.get_version_record(
                experiment_code, stat["observation_date"], stat["current_version"]
            )
            return self._envelope(stat, record)

        candidates = [
            row for row in _versions() if row.get("experiment_code") == experiment_code
        ]
        if not candidates:
            return None
        stat = max(candidates, key=lambda row: row["observation_date"])
        record = self.get_version_record(
            experiment_code, stat["observation_date"], stat["current_version"]
        )
        return self._envelope(stat, record)

    def get_version_record(
        self, experiment_code: str, observation_date: str, version: int
    ) -> dict[str, Any] | None:
        stat = self.get_stat(experiment_code, str(observation_date))
        if stat is None:
            return None
        record = next(
            (v for v in stat["versions"] if int(v.get("version")) == int(version)), None
        )
        return dict(record) if record is not None else None

    def resolve_version(
        self, experiment_code: str, observation_date: str, version: int
    ) -> dict[str, Any] | None:
        """按版本号回查任意历史版本（报表复算入口），附带当前版本指针。"""
        record = self.get_version_record(experiment_code, observation_date, version)
        if record is None:
            return None
        stat = self.get_stat(experiment_code, observation_date)
        envelope = self._envelope(stat, record)
        envelope["is_current"] = int(version) == int(stat["current_version"])
        envelope["current_version"] = stat["current_version"]
        return envelope

    def list_current_stats(self, experiment_code: str | None = None) -> list[dict[str, Any]]:
        """每个 (实验, 观测日) 只返回一条当前统计——数据集市接口的去重口径。"""
        result: list[dict[str, Any]] = []
        for stat in _versions():
            if experiment_code and stat.get("experiment_code") != experiment_code:
                continue
            record = self.get_version_record(
                stat["experiment_code"], stat["observation_date"], stat["current_version"]
            )
            if record is not None:
                result.append(self._envelope(stat, record))
        result.sort(key=lambda item: (item["experiment_code"], item["observation_date"]))
        return result

    def list_versions(
        self, experiment_code: str, observation_date: str
    ) -> list[dict[str, Any]]:
        stat = self.get_stat(experiment_code, str(observation_date))
        if stat is None:
            return []
        return [dict(v) for v in stat["versions"]]

    def count_mart_projections(self, experiment_code: str | None = None) -> int:
        """集市中当前统计投影行数，用于断言重跑不产生重复主题行。"""
        target = theme_code(experiment_code) if experiment_code else None
        return sum(
            1
            for row in store_module.store.rows(MART_TABLE)
            if row.get("统计ID") is not None
            and (target is None or row.get("主题编号") == target)
        )

    # -- 供报表聚合 -------------------------------------------------------

    def summarize_current_for_report(
        self, experiment_codes: list[str] | None = None
    ) -> list[dict[str, Any]]:
        """报表入口读取「完整统计版本」汇总转化率与提升量，并保留版本引用。

        同一实验无论重跑多少次，这里只出现一次（当前版本），不会重复计入。
        """
        wanted = set(experiment_codes or [])
        summary: list[dict[str, Any]] = []
        for item in self.list_current_stats():
            if wanted and item["experiment_code"] not in wanted:
                continue
            payload = item["payload"]
            summary.append({
                "实验编号": item["experiment_code"],
                "观测日": item["observation_date"],
                "统计ID": item["stat_id"],
                "引用版本": item["version"],
                "版本ID": item["version_id"],
                "控制组转化率": payload.get("control_rate"),
                "实验组转化率": payload.get("treatment_rate"),
                "绝对提升": payload.get("absolute_lift"),
                "相对提升": payload.get("relative_lift"),
                "显著性": payload.get("significant_at_05"),
            })
        return summary

    def merge_sample_corrections(
        self,
        base_samples: list[dict[str, Any]],
        overrides: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """用同观测日的样本更正覆盖原发布记录；未提供更正的分组保持原样本。

        匹配键是 (实验编号, 分组)，因此同一样本重试仍对应同一观测日的同一分组，
        更正只替换该分组的人数/样本量，不改变观测归属。
        """
        merged: dict[tuple[str, str], dict[str, Any]] = {
            _group_key(row): dict(row) for row in base_samples
        }
        for override in overrides:
            key = _group_key(override)
            target = merged.setdefault(key, {})
            # 仅覆盖更正里显式给出的字段，None 不能清掉原发布记录的数值
            for field, value in override.items():
                if value is not None:
                    target[field] = value
        return [merged[key] for key in sorted(merged)]

    # -- 内部 -------------------------------------------------------------

    @staticmethod
    def _live_version_record(
        stat: dict[str, Any], version: int
    ) -> dict[str, Any] | None:
        """返回 stat 内部持有的真实版本记录（非副本），供发布时原地标记替代。"""
        return next(
            (v for v in stat["versions"] if int(v.get("version")) == int(version)),
            None,
        )

    @staticmethod
    def _make_version(
        experiment_code: str,
        observation_date: str,
        version: int,
        payload: dict[str, Any],
        samples: list[dict[str, Any]],
        now: str,
        source_run: str | None,
    ) -> dict[str, Any]:
        return {
            "version_id": version_id(experiment_code, observation_date, version),
            "version": version,
            "published_at": now,
            "last_run_at": now,
            "run_hits": [source_run] if source_run else [],
            "is_current": True,
            "superseded_by": None,
            "superseded_at": None,
            "payload": payload,
            "samples": samples,
        }

    @staticmethod
    def _same_content(left: dict[str, Any], right: dict[str, Any]) -> bool:
        ignore = {"published_at", "last_run_at", "run_hits"}
        keys = (set(left) | set(right)) - ignore
        return all(left.get(key) == right.get(key) for key in keys)

    @staticmethod
    def _envelope(stat: dict[str, Any], record: dict[str, Any] | None) -> dict[str, Any]:
        if record is None:
            return {}
        return {
            "stat_id": stat["stat_id"],
            "experiment_code": stat["experiment_code"],
            "observation_date": stat["observation_date"],
            "version": record["version"],
            "version_id": record["version_id"],
            "is_current": record.get("is_current", True),
            "superseded_by": record.get("superseded_by"),
            "published_at": record.get("published_at"),
            "payload": dict(record.get("payload", {})),
            "samples": [dict(s) for s in record.get("samples", [])],
        }

    @staticmethod
    def _publish_result(
        stat: dict[str, Any],
        record: dict[str, Any],
        status: str,
        replaced: bool,
        projection: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "stat_id": stat["stat_id"],
            "experiment_code": stat["experiment_code"],
            "observation_date": stat["observation_date"],
            "version_id": record["version_id"],
            "version": record["version"],
            "current_version": stat["current_version"],
            "status": status,
            "replaced": replaced,
            "is_current": True,
            "current": ExperimentStatService._envelope(stat, record),
            "mart_row": {
                "主题编号": projection.get("主题编号"),
                "观测日": projection.get("观测日"),
                "统计版本": projection.get("统计版本"),
                "版本ID": projection.get("版本ID"),
            },
        }


def datetime_now() -> str:
    from datetime import datetime

    return datetime.now().isoformat()


# 便捷的模块级单例，与各模块 service = XxxService() 的写法保持一致
experiment_stat_service = ExperimentStatService()
