"""API 请求 / 响应模型。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

JobStatus = Literal[
    "pending",      # 已受理，等待运行名额
    "running",      # 运行中
    "cancelling",   # 已收到取消请求，正在中止本轮 SQL
    "succeeded",    # 终态：核对完成（可能一致，也可能有差异）
    "failed",       # 终态：任一侧失败
    "cancelled",    # 终态：用户取消
    "timeout",      # 终态：超过单轮时限
]

TERMINAL_STATUSES = {"succeeded", "failed", "cancelled", "timeout"}
ACTIVE_STATUSES = {"pending", "running", "cancelling"}


class CreateJobRequest(BaseModel):
    left_sql: str = Field(..., min_length=1, description="左侧只读 SELECT（如改写后的日结 SQL）")
    right_sql: str = Field(..., min_length=1, description="右侧只读 SELECT（基准 SQL）")
    name: str | None = Field(None, max_length=128, description="可选备注名")


class DiffRow(BaseModel):
    # 与结果集列顺序对齐；NULL 用 null 表示，空串为 ""，二者不同
    values: list[Any]
    left_count: int
    right_count: int


class ReconcileResult(BaseModel):
    identical: bool
    columns: list[str]
    column_types: list[str]          # bigint | text
    left_row_total: int
    right_row_total: int
    matched_group_count: int         # 行内容与次数都相同的去重行数
    diff_group_total: int            # 存在差异的去重行种类总数（不受 20 条展示上限影响）
    diffs_shown: int
    diffs: list[DiffRow]             # 最多 20 种


class JobOut(BaseModel):
    job_id: str
    name: str | None
    status: JobStatus
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    deadline: datetime | None
    cancel_requested: bool
    error: str | None
    result: ReconcileResult | None

    @staticmethod
    def utcnow() -> datetime:
        return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# 15 分钟用电峰值 API
# ---------------------------------------------------------------------------

class CreatePeakRequest(BaseModel):
    line_name: str = Field(..., min_length=1, max_length=128, description="产线名（meters.line_name）")
    target_hour: datetime = Field(
        ...,
        description=(
            "目标 UTC 整点小时，必须是带时区的整点时刻（tzinfo 必填）；"
            "如 2026-11-15T09:00:00Z 表示 [09:00, 10:00) UTC"
        ),
    )
    name: str | None = Field(None, max_length=128, description="可选备注名（保留字段）")


class PeakWindowOut(BaseModel):
    window_start: datetime
    window_end: datetime
    wh: float                 # 未舍入的分摊电量


class PeakConclusionOut(BaseModel):
    conclusion_id: str
    line_name: str
    target_hour: datetime
    status: Literal["available", "stale"]   # 旧口径（版本落后/被取代/交错）即 stale
    stale: bool
    superseded: bool                        # 已被同产线同小时的新结论取代
    interleaved: bool                       # 计算与源表更正交错，发布时即按过期口径标记
    data_revision: int                      # 结论对应的数据修订版本
    latest_revision: int | None = None      # 查询时的最新版本（POST 响应同样回填）
    meter_count: int
    meter_ids: list[int]
    window_minutes: int
    windows: list[PeakWindowOut]
    peak_wh: float                          # 未舍入值
    peak_window_start: datetime
    peak_window_end: datetime
    created_at: datetime | None
