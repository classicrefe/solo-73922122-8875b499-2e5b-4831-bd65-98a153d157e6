"""FastAPI 应用入口与 HTTP 路由。"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

from . import db as dbmod
from .manager import SlotBusyError, manager
from .models import (
    CreateJobRequest,
    CreatePeakRequest,
    JobOut,
    PeakConclusionOut,
)
from .peaks import PeakUnavailable, calculate_and_persist
from .sqlvalidation import SqlValidationError

app = FastAPI(
    title="电表日结 SQL 核对 / 15 分钟用电峰值 API",
    description=(
        "核对任务：提交两条只读 SELECT，基于同一已提交快照并行读取，"
        "按行内容与出现次数做有序多重集核对（忽略行序，NULL≠空串），"
        "差异最多展示 20 种。单轮 30 秒，全局同时只允许一个任务。\n\n"
        "峰值任务：按产线选取同一快照中的表计，把相邻累计 Wh 差值按时间比例"
        "分摊到目标 UTC 整点小时的 4 个 15 分钟窗口并汇总，返回未舍入的峰值"
        "电量与峰值窗口（并列取最早）。源表更正后旧结论自动展示为过期。"
    ),
    version="1.1.0",
)


@app.get("/health", tags=["运维"])
def health() -> dict:
    """存活探针；不触碰数据库。"""
    return {"status": "ok"}


@app.get("/jobs", response_model=list[JobOut], tags=["核对任务"])
def list_jobs() -> list[JobOut]:
    """列出近期任务（含终态），状态查询始终可用。"""
    return manager.list_jobs()


@app.post("/jobs", response_model=JobOut, status_code=201, tags=["核对任务"])
def create_job(payload: CreateJobRequest) -> JobOut:
    try:
        job = manager.create_job(payload.left_sql, payload.right_sql, payload.name)
    except SqlValidationError as exc:
        raise HTTPException(status_code=400, detail=f"SQL 校验失败：{exc}") from exc
    except SlotBusyError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "当前已有核对任务在运行，一次只允许一组",
                "active_job_id": exc.active_job_id,
            },
        ) from exc
    return job.snapshot()


@app.get("/jobs/{job_id}", response_model=JobOut, tags=["核对任务"])
def get_job(job_id: str) -> JobOut:
    job = manager.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return job.snapshot()


@app.post("/jobs/{job_id}/cancel", response_model=JobOut, tags=["核对任务"])
def cancel_job(job_id: str) -> JobOut:
    try:
        job = manager.request_cancel(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc
    return job.snapshot()


@app.exception_handler(dbmod.DbError)
def _db_error_handler(_request, exc: dbmod.DbError) -> JSONResponse:
    return JSONResponse(status_code=422, detail={"message": str(exc)})


# ===========================================================================
# 15 分钟用电峰值
# ===========================================================================

def _validate_utc_hour(value: datetime) -> datetime:
    """目标时刻必须带时区且为 UTC 整点；统一转为 UTC。"""
    if value.tzinfo is None or value.utcoffset() is None:
        raise HTTPException(
            status_code=400,
            detail={"message": "target_hour 必须带时区信息（如 ...Z 或 +00:00）"},
        )
    utc_value = value.astimezone(timezone.utc)
    if (
        utc_value.minute != 0
        or utc_value.second != 0
        or utc_value.microsecond != 0
    ):
        raise HTTPException(
            status_code=400,
            detail={"message": "target_hour 必须是 UTC 整点小时（分/秒/微秒为 0）"},
        )
    return utc_value


@app.post(
    "/peaks",
    response_model=PeakConclusionOut,
    status_code=201,
    tags=["15分钟用电峰值"],
)
def create_peak(payload: CreatePeakRequest) -> PeakConclusionOut:
    """在同一已提交快照上计算并保存指定 UTC 整点小时的 15 分钟峰值。

    产线无表计、缺边界读数、读数回退、相邻间隔超 2 小时均返回 422 并给出
    reason_code，不发布部分结论、不写结论记录。
    """
    target_hour = _validate_utc_hour(payload.target_hour)
    try:
        conclusion = calculate_and_persist(payload.line_name, target_hour)
    except PeakUnavailable as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "message": exc.message,
                "reason_code": exc.reason_code,
                "line_name": payload.line_name,
                "target_hour": target_hour.isoformat(),
            },
        ) from exc
    return conclusion


@app.get(
    "/peaks",
    response_model=list[PeakConclusionOut],
    tags=["15分钟用电峰值"],
)
def list_peaks(
    line_name: str | None = Query(None, max_length=128, description="按产线过滤"),
    target_hour: datetime | None = Query(None, description="按 UTC 整点小时过滤"),
) -> list[PeakConclusionOut]:
    """列出已保存的峰值结论（含过期结论）；过期状态按当前数据版本实时判定。"""
    hour = _validate_utc_hour(target_hour) if target_hour is not None else None
    return dbmod.list_peak_conclusions(line_name=line_name, target_hour=hour)


@app.get(
    "/peaks/{conclusion_id}",
    response_model=PeakConclusionOut,
    tags=["15分钟用电峰值"],
)
def get_peak(conclusion_id: str) -> PeakConclusionOut:
    conclusion = dbmod.get_peak_conclusion(conclusion_id)
    if conclusion is None:
        raise HTTPException(status_code=404, detail="峰值结论不存在")
    return conclusion


@app.exception_handler(PeakUnavailable)
def _peak_unavailable_handler(_request, exc: PeakUnavailable) -> JSONResponse:
    # 兜底：领域层其他入口直接抛出时同样不发布部分结论
    return JSONResponse(
        status_code=422,
        detail={"message": exc.message, "reason_code": exc.reason_code},
    )
