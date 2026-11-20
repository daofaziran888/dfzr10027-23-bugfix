"""数据集市接口：维护分析主题，覆盖启动建设、上线主题、下线主题等动作。"""
from __future__ import annotations

import csv
import io

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import ActionPayload, ActionResult, EntryPayload, PageResult
from app.services.data_mart import DataMartService

router = APIRouter(prefix="/api/data_mart", tags=["数据集市"])

service = DataMartService()

LIST_FIELDS = ["主题编号", "主题名称", "业务域", "事实表", "维度集合", "刷新周期", "数据量", "负责人"]
STATUSES = ["规划中", "建设中", "已上线", "已下线"]


def query_filters(request: Request) -> dict[str, str]:
    """只接受当前模块声明过的字段，忽略分页等保留参数。"""
    return {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in LIST_FIELDS and value.strip()
    }


@router.get("", response_model=PageResult[dict])
def list_entries(
    request: Request,
    keyword: str | None = Query(default=None, description="按主题编号检索"),
    status: str | None = Query(default=None, description="规划中、建设中、已上线、已下线"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    """按页面字段、主题编号关键字与状态过滤数据集市列表。"""
    items, total = service.list_entries(
        filters=query_filters(request), keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries(
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> StreamingResponse:
    """下载当前筛选条件下的 UTF-8 CSV；必须声明在 /{entry_id} 之前。"""
    items, _ = service.list_entries(
        filters=query_filters(request), keyword=keyword, status=status, page=1, size=1000000)
    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["id", *LIST_FIELDS, "status"])
    for item in items:
        row = []
        for value in [item.get("id"), *(item.get(field, "") for field in LIST_FIELDS), item.get("status", "")]:
            text = "" if value is None else str(value)
            row.append("'" + text if text.startswith(("=", "+", "-", "@")) else text)
        writer.writerow(row)
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="data_mart.csv"'})


@router.get("/experiment-stats", response_model=PageResult[dict])
def list_experiment_stats(
    experiment_code: str | None = Query(default=None, description="实验编号，如 EXPE-001"),
) -> PageResult[dict]:
    """实验统计分析主题：按实验与样本观测日返回唯一当前统计，重跑不重复。"""
    items = service.list_experiment_stats(experiment_code)
    return PageResult(items=items, total=len(items), page=1, size=len(items) or 1)


@router.get("/experiment-stats/{experiment_code}/{observation_date}/versions")
def list_experiment_stat_versions(experiment_code: str, observation_date: str) -> dict:
    """同一观测日的全部统计版本（当前版本与被同日修订替代的历史版本）。"""
    versions = service.list_experiment_stat_versions(experiment_code, observation_date)
    if not versions:
        raise HTTPException(
            status_code=404,
            detail=f"实验 {experiment_code} 在观测日 {observation_date} 没有统计版本",
        )
    return {"items": versions, "total": len(versions)}


@router.get("/experiment-stats/{experiment_code}/{observation_date}")
def get_experiment_stat(
    experiment_code: str,
    observation_date: str,
    version: int | None = Query(default=None, description="不传返回当前版本，传入则回查指定历史版本"),
) -> dict:
    """读取某实验某观测日的统计；带版本号时按原版回查（报表复算用）。"""
    if version is None:
        entry = service.get_experiment_stat(experiment_code, observation_date)
    else:
        entry = service.get_experiment_stat_version(
            experiment_code, observation_date, version
        )
    if entry is None:
        raise HTTPException(
            status_code=404,
            detail=f"实验 {experiment_code} 观测日 {observation_date} 的统计版本不存在",
        )
    return entry


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条分析主题明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"分析主题 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: dict[str, object]) -> ActionResult:
    """登记一条分析主题，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="分析主题已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    """对单条分析主题执行启动建设、上线主题、下线主题；不允许的动作会被拦下并说明原因。"""
    entry, message = service.run_action(entry_id, payload.action.strip())
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
