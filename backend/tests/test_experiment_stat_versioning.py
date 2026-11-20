"""实验统计版本化与数据集市去重的端到端测试。

覆盖场景（对应用户描述的验收流程）：
1. 同一份实验统计重跑两次，集市列表不再重复出现 DATA-STAT-EXPE-001；
2. 样本观测日与原发布记录绑定，跨午夜补跑/同一样本重试仍是同一观测日、同一版本；
3. 同日修订生成新版本，旧版本保留并可按原版复算已发布报表；
4. 实验报表读取集市完整统计版本汇总转化率与提升量，重跑不会重复计入一个实验；
5. 日报先引用当日统计，随后收到同观测日样本更正：集市区分当前结果与更正，
   读者能看到新旧转化率各自使用的样本；
6. 完整流程：同一天一组样本生成报表 -> 重跑两次 -> 更正实验组转化人数
   -> 集市只有一条当前统计 -> 旧报表仍能回查更正前人数。
"""
from __future__ import annotations

from app.services.data_mart import DataMartService
from app.services.experiment_stat import experiment_stat_service
from app.services.pipeline_orchestrator import run_experiment_stat_pipeline
from app.services.report_gen import ReportGenService
from app.store import store

OBS_DATE = "2026-08-15"
EXPE = "EXPE-001"


def _correction(group: str, conversions: int) -> dict:
    return {"实验编号": EXPE, "分组": group, "转化人数": conversions}


class TestMartDedupAndObservationDay:
    """集市去重与观测日绑定。"""

    def test_rerun_does_not_duplicate_mart_theme(self):
        run_experiment_stat_pipeline(EXPE)
        run_experiment_stat_pipeline(EXPE)

        mart = DataMartService()
        items = mart.list_experiment_stats(EXPE)
        assert len(items) == 1
        assert items[0]["主题编号"] == "DATA-STAT-EXPE-001"
        # 物理表中同样只有一条投影行
        physical = [
            row for row in store.rows("data_mart")
            if row.get("主题编号") == "DATA-STAT-EXPE-001"
        ]
        assert len(physical) == 1

    def test_projection_status_survives_rerun_and_revision(self):
        run_experiment_stat_pipeline(EXPE)
        run_experiment_stat_pipeline(EXPE)
        run_experiment_stat_pipeline(
            EXPE, observation_date=OBS_DATE,
            sample_corrections=[_correction("treatment", 460)],
        )
        row = DataMartService().list_experiment_stats(EXPE)[0]
        assert row["status"] == "已上线"
        assert row["pending"] is False
        assert row["abnormal"] is False

    def test_observation_day_comes_from_samples_not_run_day(self):
        result = run_experiment_stat_pipeline(EXPE)
        assert result["observation_date"] == OBS_DATE
        current = experiment_stat_service.get_current(EXPE, OBS_DATE)
        assert current is not None
        assert current["observation_date"] == OBS_DATE

    def test_explicit_observation_date_binds_midnight_rerun(self):
        # 显式指定观测日，模拟跨午夜补跑：仍写回原观测日
        run_experiment_stat_pipeline(EXPE, observation_date=OBS_DATE)
        run_experiment_stat_pipeline(EXPE, observation_date=OBS_DATE)
        stats = experiment_stat_service.list_current_stats(EXPE)
        assert len(stats) == 1
        assert stats[0]["observation_date"] == OBS_DATE
        assert stats[0]["version"] == 1

    def test_identical_rerun_is_idempotent_same_version(self):
        first = run_experiment_stat_pipeline(EXPE)
        second = run_experiment_stat_pipeline(EXPE)
        assert first["version_id"] == second["version_id"]
        assert second["publish_status"] == "unchanged"
        assert experiment_stat_service.list_versions(EXPE, OBS_DATE).__len__() == 1

    def test_different_observation_days_are_separate_stats(self):
        run_experiment_stat_pipeline(EXPE, observation_date="2026-08-16")
        run_experiment_stat_pipeline(EXPE, observation_date=OBS_DATE)
        current = experiment_stat_service.list_current_stats(EXPE)
        assert {item["observation_date"] for item in current} == {OBS_DATE, "2026-08-16"}
        # 集市按 (主题编号, 观测日) 各有一条，不跨观测日去重
        mart = DataMartService()
        assert len(mart.list_experiment_stats(EXPE)) == 2


class TestSameDayRevisionAndHistory:
    """同日修订：新版本替换当前，旧版本保留可回查。"""

    def test_same_day_correction_creates_new_version_keeps_old(self):
        first = run_experiment_stat_pipeline(EXPE)
        assert first["version"] == 1

        revised = run_experiment_stat_pipeline(
            EXPE,
            observation_date=OBS_DATE,
            sample_corrections=[_correction("treatment", 460)],
        )
        assert revised["publish_status"] == "revised"
        assert revised["version"] == 2
        assert revised["replaced_previous"] is True

        # 集市当前投影只有一条且指向 v2
        mart = DataMartService()
        items = mart.list_experiment_stats(EXPE)
        assert len(items) == 1
        assert items[0]["统计版本"] == 2

        # 历史 v1 仍可回查，且带有被替代指针
        old = experiment_stat_service.resolve_version(EXPE, OBS_DATE, 1)
        assert old is not None
        assert old["is_current"] is False
        assert old["current_version"] == 2
        assert old["superseded_by"] == revised["version_id"]

    def test_versions_expose_which_samples_each_rate_used(self):
        run_experiment_stat_pipeline(EXPE)
        run_experiment_stat_pipeline(
            EXPE,
            observation_date=OBS_DATE,
            sample_corrections=[_correction("treatment", 460)],
        )
        v1 = experiment_stat_service.resolve_version(EXPE, OBS_DATE, 1)
        v2 = experiment_stat_service.resolve_version(EXPE, OBS_DATE, 2)

        v1_treatment = next(s for s in v1["samples"] if s["分组"] == "treatment")
        v2_treatment = next(s for s in v2["samples"] if s["分组"] == "treatment")
        assert v1_treatment["转化人数"] == 412
        assert v2_treatment["转化人数"] == 460
        # 对照组未在更正中给出，保持原发布记录
        v2_control = next(s for s in v2["samples"] if s["分组"] == "control")
        assert v2_control["转化人数"] == 350

        # 新旧转化率各自可查
        assert v1["payload"]["treatment_rate"] == round(412 / 5000, 4)
        assert v2["payload"]["treatment_rate"] == round(460 / 5000, 4)

    def test_repeated_identical_correction_does_not_spawn_versions(self):
        run_experiment_stat_pipeline(EXPE)
        run_experiment_stat_pipeline(
            EXPE, observation_date=OBS_DATE,
            sample_corrections=[_correction("treatment", 460)],
        )
        # 同样的更正重试：幂等，不产生 v3
        again = run_experiment_stat_pipeline(
            EXPE, observation_date=OBS_DATE,
            sample_corrections=[_correction("treatment", 460)],
        )
        assert again["version"] == 2
        assert again["publish_status"] == "unchanged"
        assert len(experiment_stat_service.list_versions(EXPE, OBS_DATE)) == 2

    def test_rerun_without_correction_after_revision_does_not_mutate_fixtures(self):
        """更正生成 v2 后，不带更正重跑同一观测日：用原始样本重新计算。

        回归点：管道必须深拷贝 fixtures，不能让更正污染下一次重跑的原始样本。
        版本历史是 append-only 的，原始样本与当前 v2 内容不同，因此这是一次
        新的修订（v3），而不是回滚到不可变的 v1——已发布版本永远不会被改写。
        """
        first = run_experiment_stat_pipeline(EXPE)
        run_experiment_stat_pipeline(
            EXPE, observation_date=OBS_DATE,
            sample_corrections=[_correction("treatment", 460)],
        )
        # fixtures 里的原始样本未被污染，仍是 412
        from app.fixtures import EXPERIMENT_RESULTS

        fixture_treatment = next(
            r for r in EXPERIMENT_RESULTS
            if r["实验编号"] == EXPE and r["分组"] == "treatment"
        )
        assert fixture_treatment["转化人数"] == 412

        again = run_experiment_stat_pipeline(EXPE)
        assert again["observation_date"] == OBS_DATE
        assert again["version"] == 3
        assert again["publish_status"] == "revised"
        assert again["conversions_treatment"] == 412
        # v1 原样保留、未被回滚改写
        v1 = experiment_stat_service.resolve_version(EXPE, OBS_DATE, 1)
        v1_treatment = next(s for s in v1["samples"] if s["分组"] == "treatment")
        assert v1_treatment["转化人数"] == 412
        assert v1["is_current"] is False
        assert first["version_id"] == v1["version_id"]

    def test_mart_versions_endpoint_data(self):
        run_experiment_stat_pipeline(EXPE)
        run_experiment_stat_pipeline(
            EXPE, observation_date=OBS_DATE,
            sample_corrections=[_correction("treatment", 460)],
        )
        versions = DataMartService().list_experiment_stat_versions(EXPE, OBS_DATE)
        assert [v["version"] for v in versions] == [1, 2]
        assert versions[0]["is_current"] is False
        assert versions[1]["is_current"] is True


class TestExperimentReportReferences:
    """实验报表读取集市统计版本、保留引用并可按原版复算。"""

    def test_report_uses_current_version_and_keeps_reference(self):
        run_experiment_stat_pipeline(EXPE)
        report_resp = ReportGenService().generate_experiment_report([EXPE])
        assert report_resp["ok"] is True
        report = report_resp["report"]

        assert report["实验数"] == 1
        item = report["items"][0]
        assert item["引用版本"] == 1
        assert item["版本ID"].endswith(":v1")
        assert item["控制组转化率"] == round(350 / 5000, 4)
        assert item["实验组转化率"] == round(412 / 5000, 4)
        assert item["绝对提升"] == round(412 / 5000 - 350 / 5000, 4)

    def test_rerun_before_report_does_not_double_count(self):
        run_experiment_stat_pipeline(EXPE)
        run_experiment_stat_pipeline(EXPE)
        run_experiment_stat_pipeline(EXPE)
        report = ReportGenService().generate_experiment_report([EXPE])["report"]
        assert report["实验数"] == 1
        assert len(report["items"]) == 1

    def test_two_reports_then_correction_old_report_recomputes_old_numbers(self):
        svc = ReportGenService()
        run_experiment_stat_pipeline(EXPE)
        report_a = svc.generate_experiment_report([EXPE])["report"]
        report_b = svc.generate_experiment_report([EXPE])["report"]
        # 两次报表引用同一个当前版本
        assert report_a["references"][0]["version"] == 1
        assert report_b["references"][0]["version"] == 1

        # 随后收到同观测日的样本更正 -> v2 成为当前
        run_experiment_stat_pipeline(
            EXPE, observation_date=OBS_DATE,
            sample_corrections=[_correction("treatment", 460)],
        )

        # 旧报表按原版复算：仍是更正前的 412 人
        recomputed_a = svc.recompute_experiment_report(report_a["id"])
        item = recomputed_a["items"][0]
        assert item["引用版本"] == 1
        assert item["引用版本是否当前"] is False
        assert item["当前版本"] == 2
        old_treatment = next(s for s in item["引用样本"] if s["分组"] == "treatment")
        assert old_treatment["转化人数"] == 412
        assert item["实验组转化率"] == round(412 / 5000, 4)

        # 新报表读取集市当前版本 v2
        report_c = svc.generate_experiment_report([EXPE])["report"]
        assert report_c["items"][0]["引用版本"] == 2
        recomputed_c = svc.recompute_experiment_report(report_c["id"])
        new_treatment = next(
            s for s in recomputed_c["items"][0]["引用样本"] if s["分组"] == "treatment"
        )
        assert new_treatment["转化人数"] == 460

    def test_refresh_and_same_day_correction_concurrency_resolves_actual_version(self):
        """报表刷新与同日更正并发：复算始终识别报表实际引用的版本。"""
        svc = ReportGenService()
        run_experiment_stat_pipeline(EXPE)
        old_report = svc.generate_experiment_report([EXPE])["report"]

        # 集市在报表生成后被同日更正
        run_experiment_stat_pipeline(
            EXPE, observation_date=OBS_DATE,
            sample_corrections=[_correction("treatment", 388)],
        )
        # 刷新产生的新报表引用 v2
        new_report = svc.generate_experiment_report([EXPE])["report"]
        assert new_report["references"][0]["version"] == 2

        old_recompute = svc.recompute_experiment_report(old_report["id"])
        new_recompute = svc.recompute_experiment_report(new_report["id"])
        assert old_recompute["items"][0]["引用版本是否当前"] is False
        assert new_recompute["items"][0]["引用版本是否当前"] is True
        assert old_recompute["items"][0]["实验组转化率"] == round(412 / 5000, 4)
        assert new_recompute["items"][0]["实验组转化率"] == round(388 / 5000, 4)


class TestEndToEndAcceptanceFlow:
    """对应用户描述的完整验收流程。"""

    def test_full_flow_single_current_stat_and_history_lookup(self):
        svc_report = ReportGenService()
        svc_mart = DataMartService()

        # 1. 同一天用一组样本生成报表
        run_experiment_stat_pipeline(EXPE)
        first_report = svc_report.generate_experiment_report([EXPE])["report"]

        # 2. 重跑两次（幂等，不新增版本、不新增集市行）
        run_experiment_stat_pipeline(EXPE)
        run_experiment_stat_pipeline(EXPE)

        # 3. 更正实验组转化人数（同观测日修订 -> v2）
        run_experiment_stat_pipeline(
            EXPE, observation_date=OBS_DATE,
            sample_corrections=[_correction("treatment", 460)],
        )

        # 4. 查数据集市：该观测日只有一条当前统计，指向 v2
        mart_items = svc_mart.list_experiment_stats(EXPE)
        assert len(mart_items) == 1
        assert mart_items[0]["观测日"] == OBS_DATE
        assert mart_items[0]["统计版本"] == 2
        assert mart_items[0]["实验组转化率"] == round(460 / 5000, 4)

        current = svc_mart.get_experiment_stat(EXPE, OBS_DATE)
        assert current["version"] == 2

        # 5. 查两次报表：旧报表回查更正前人数，新报表反映更正
        old_recompute = svc_report.recompute_experiment_report(first_report["id"])
        old_item = old_recompute["items"][0]
        assert old_item["引用版本"] == 1
        old_treatment = next(s for s in old_item["引用样本"] if s["分组"] == "treatment")
        assert old_treatment["转化人数"] == 412

        second_report = svc_report.generate_experiment_report([EXPE])["report"]
        new_recompute = svc_report.recompute_experiment_report(second_report["id"])
        new_treatment = next(
            s for s in new_recompute["items"][0]["引用样本"] if s["分组"] == "treatment"
        )
        assert new_treatment["转化人数"] == 460

        # 6. 版本历史完整：v1（已替代）+ v2（当前）
        versions = svc_mart.list_experiment_stat_versions(EXPE, OBS_DATE)
        assert [v["version"] for v in versions] == [1, 2]
        assert sum(1 for v in versions if v["is_current"]) == 1

        # 7. 报表列表里两份报表都在
        all_reports = svc_report.list_experiment_reports()
        assert len(all_reports) == 2
