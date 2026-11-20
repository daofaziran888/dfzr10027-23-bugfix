"""数据管道接口：触发跨模块计算并查询产物。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from app.schemas import ActionResult
from app.services.pipeline_orchestrator import (
    list_pipeline_results,
    run_all_pipelines,
    run_attribution_compare_pipeline,
    run_cohort_extract_pipeline,
    run_cube_pipeline,
    run_experiment_stat_pipeline,
    run_forecast_pipeline,
    run_metric_agg_pipeline,
    run_quality_scan,
)

router = APIRouter(prefix="/api/pipeline", tags=["数据管道"])


class SampleCorrection(BaseModel):
    """同观测日的单组样本更正：按 (实验编号, 分组) 覆盖原发布记录。"""

    分组: str
    样本量: int | None = None
    转化人数: int | None = None
    gmv_sum: float | None = None


class ExperimentStatPayload(BaseModel):
    observation_date: str | None = None
    sample_corrections: list[SampleCorrection] = Field(default_factory=list)


@router.post("/run-all", response_model=ActionResult)
def trigger_all() -> ActionResult:
    """顺序执行全部 7 条跨模块数据管道。"""
    summary = run_all_pipelines()
    return ActionResult(
        ok=True,
        message="全部管道已执行",
        entry={"pipelines": list(summary.keys()), "count": len(summary)},
    )


@router.post("/metric-agg", response_model=ActionResult)
def trigger_metric_agg(
    metric_code: str | None = Query(default=None, description="指标编号，如 METR-GMV"),
) -> ActionResult:
    result = run_metric_agg_pipeline(metric_code)
    return ActionResult(ok=True, message="指标聚合管道已执行", entry=result)


@router.post("/forecast", response_model=ActionResult)
def trigger_forecast(
    horizon: int = Query(default=7, ge=1, le=30, description="预测天数"),
) -> ActionResult:
    result = run_forecast_pipeline(horizon)
    return ActionResult(ok=True, message="预测管道已执行", entry=result)


@router.post("/cube", response_model=ActionResult)
def trigger_cube(
    dim_codes: list[str] | None = Query(default=None, description="维度编号列表"),
) -> ActionResult:
    result = run_cube_pipeline(dim_codes)
    return ActionResult(ok=True, message="立方体构建管道已执行", entry=result)


@router.post("/cohort-extract", response_model=ActionResult)
def trigger_cohort_extract() -> ActionResult:
    result = run_cohort_extract_pipeline()
    return ActionResult(ok=True, message="分群提取管道已执行", entry=result)


@router.post("/experiment-stat", response_model=ActionResult)
def trigger_experiment_stat(
    experiment_code: str | None = Query(default=None, description="实验编号"),
    payload: ExperimentStatPayload | None = None,
) -> ActionResult:
    """执行实验统计管道。

    可在请求体里指定观测日与同观测日的样本更正；重跑同一观测日且结果不变时
    幂等复用原版本，样本更正则生成新版本并保留旧版本。
    """
    payload = payload or ExperimentStatPayload()
    corrections: list[dict[str, Any]] = [
        item.model_dump(exclude_none=True) for item in payload.sample_corrections
    ]
    result = run_experiment_stat_pipeline(
        experiment_code,
        observation_date=payload.observation_date,
        sample_corrections=corrections or None,
    )
    return ActionResult(ok=True, message="实验统计管道已执行", entry=result)


@router.post("/quality-scan", response_model=ActionResult)
def trigger_quality_scan() -> ActionResult:
    result = run_quality_scan()
    return ActionResult(ok=True, message="质量扫描管道已执行", entry=result)


@router.post("/attribution-compare", response_model=ActionResult)
def trigger_attribution_compare(
    link_code: str | None = Query(default=None, description="链路编号"),
) -> ActionResult:
    result = run_attribution_compare_pipeline(link_code)
    return ActionResult(ok=True, message="归因对比管道已执行", entry=result)


@router.get("/results")
def get_results() -> dict[str, object]:
    """查询全部管道产物。"""
    return {"items": list_pipeline_results()}
