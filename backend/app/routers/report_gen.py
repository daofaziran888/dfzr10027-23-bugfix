"""报表生成接口：维护分析报表，覆盖提交生成、确认分发、停用报表等动作。"""
from __future__ import annotations

import csv
import io

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import ActionPayload, ActionResult, EntryPayload, PageResult
from app.services.report_gen import ReportGenService

router = APIRouter(prefix="/api/report_gen", tags=["报表生成"])

service = ReportGenService()

LIST_FIELDS = ["报表编号", "报表类型", "数据范围", "包含图表", "生成周期", "输出格式", "分发渠道", "下次生成"]
STATUSES = ["草稿", "待生成", "已生成", "已停用"]


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
    keyword: str | None = Query(default=None, description="按报表编号检索"),
    status: str | None = Query(default=None, description="草稿、待生成、已生成、已停用"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    """按页面字段、报表编号关键字与状态过滤报表生成列表。"""
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
        headers={"Content-Disposition": 'attachment; filename="report_gen.csv"'})


@router.post("/experiment-reports/generate", response_model=ActionResult)
def generate_experiment_report(payload: dict | None = None) -> ActionResult:
    """基于数据集市当前统计版本生成实验报表，逐实验保留版本引用。"""
    experiment_codes = (payload or {}).get("experiment_codes")
    result = service.generate_experiment_report(experiment_codes)
    return ActionResult(
        ok=bool(result.get("ok")),
        message=result.get("message", ""),
        entry=result.get("report"),
    )


@router.get("/experiment-reports")
def list_experiment_reports() -> dict:
    """列出全部已生成的实验报表。"""
    items = service.list_experiment_reports()
    return {"items": items, "total": len(items)}


@router.get("/experiment-reports/{report_id}/recompute")
def recompute_experiment_report(report_id: int) -> dict:
    """按报表引用的原始统计版本复算；旧版本被同日修订替代后仍可回查。"""
    result = service.recompute_experiment_report(report_id)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("message", "报表不存在"))
    return result


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条分析报表明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"分析报表 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: dict[str, object]) -> ActionResult:
    """登记一条分析报表，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="分析报表已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    """对单条分析报表执行提交生成、确认分发、停用报表；不允许的动作会被拦下并说明原因。"""
    entry, message = service.run_action(entry_id, payload.action.strip())
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
