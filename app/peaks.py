"""15 分钟用电峰值计算（纯业务口径）与同快照编排。

口径（需求）：
- 按同一已提交快照中的 meters.line_name 选取该产线全部表计；
- 读取覆盖目标 UTC 整点小时 [H, H+1h] 的 readings；
- 相邻累计 Wh 差值（允许 0，拒绝回退）按相交时长比例分摊到
  15 分钟窗口（H..H+1h 共 4 个窗口），再按窗口汇总各表；
- 用未舍入值比较窗口电量，并列时取最早窗口，返回峰值电量与窗口；
- 任一表计不可用（缺边界读数 / 回退 / 相邻间隔 > 2 小时）即整产线不可用，
  绝不发布部分结论。

数据修订（陈旧判定）：
- meters / readings 的任何独立 INSERT/UPDATE/DELETE/TRUNCATE 都会让
  data_revision 单调 +1（由 sql/init.sql 中的语句级触发器维护）；
- 结论记录其计算快照对应的 revision；GET 查询时 revision 落后即展示“已过期”；
- 计算与更正交错（发布前 revision 已变化）时，结论按过期口径保存并标记
  stale/interleaved，绝不冒充当前口径的新鲜结论。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from . import db as dbmod

WINDOW_MINUTES = 15
WINDOWS_PER_HOUR = 4
# 相邻读数最大允许间隔：恰好 2 小时合法，超过 2 小时不可用
MAX_GAP = timedelta(hours=2)


class PeakUnavailable(RuntimeError):
    """整产线峰值不可用；不发布任何部分结论。"""

    def __init__(self, reason_code: str, message: str):
        super().__init__(message)
        self.reason_code = reason_code
        self.message = message


@dataclass(frozen=True)
class Reading:
    read_time: datetime
    wh: int


@dataclass
class MeterCoverage:
    """单表在目标小时附近的读数（已按时刻升序）与边界覆盖情况。"""

    meter_id: int
    readings: list[Reading]
    has_start: bool
    has_end: bool


@dataclass
class PeakWindow:
    window_start: datetime
    window_end: datetime
    wh: float


@dataclass
class PeakResult:
    line_name: str
    target_hour: datetime
    window_minutes: int
    meter_count: int
    windows: list[PeakWindow]
    peak_wh: float
    peak_window_start: datetime
    peak_window_end: datetime
    meter_ids: list[int] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 纯函数：单表校验 + 按时间比例分摊
# ---------------------------------------------------------------------------

def validate_and_allocate(
    meter_id: int,
    readings: list[Reading],
    has_start: bool,
    has_end: bool,
    hour_start: datetime,
    hour_end: datetime,
) -> list[float]:
    """校验单表读数并返回该表在 4 个 15 分钟窗口上的分摊电量（Wh）。

    读数必须单调非降（回退即不可用），相邻间隔不得超过 2 小时；
    只有与目标小时相交的相邻读数对参与分摊。
    """
    if not has_start or not has_end:
        missing = "起始边界" if not has_start else "结束边界"
        raise PeakUnavailable(
            "missing_boundary_reading",
            f"表计 {meter_id} 缺少覆盖小时边界的读数（{missing}），无法分摊",
        )

    allocations = [0.0] * WINDOWS_PER_HOUR
    prev: Reading | None = None
    for cur in readings:
        if prev is not None:
            gap = cur.read_time - prev.read_time
            if gap > MAX_GAP:
                raise PeakUnavailable(
                    "reading_gap_too_large",
                    f"表计 {meter_id} 在 {prev.read_time.isoformat()} 与 "
                    f"{cur.read_time.isoformat()} 之间相邻读数间隔 "
                    f"{gap.total_seconds() / 3600:.2f} 小时，超过 2 小时上限",
                )
            delta = cur.wh - prev.wh
            if delta < 0:
                raise PeakUnavailable(
                    "reading_regression",
                    f"表计 {meter_id} 读数回退：{prev.read_time.isoformat()} 时 "
                    f"{prev.wh} Wh -> {cur.read_time.isoformat()} 时 {cur.wh} Wh",
                )
            # 只处理与目标小时相交的相邻对
            if cur.read_time > hour_start and prev.read_time < hour_end:
                _allocate_pair(
                    prev, cur, delta, hour_start, hour_end, allocations
                )
        prev = cur
    return allocations


def _allocate_pair(
    prev: Reading,
    cur: Reading,
    delta_wh: int,
    hour_start: datetime,
    hour_end: datetime,
    allocations: list[float],
) -> None:
    """把相邻读数对的 Wh 差值按相交时长比例分摊到 15 分钟窗口。"""
    pair_start = max(prev.read_time, hour_start)
    pair_end = min(cur.read_time, hour_end)
    # 防御：调用方已保证相交；全等时刻对（间隔 0）不分摊
    total_seconds = (cur.read_time - prev.read_time).total_seconds()
    if total_seconds <= 0:
        return
    rate = delta_wh / total_seconds  # Wh/秒，未舍入
    window = timedelta(minutes=WINDOW_MINUTES)
    for idx in range(WINDOWS_PER_HOUR):
        w_start = hour_start + idx * window
        w_end = w_start + window
        overlap_start = max(pair_start, w_start)
        overlap_end = min(pair_end, w_end)
        if overlap_end > overlap_start:
            overlap_seconds = (overlap_end - overlap_start).total_seconds()
            allocations[idx] += rate * overlap_seconds


def pick_peak(
    window_totals: list[float], hour_start: datetime
) -> tuple[float, datetime, datetime]:
    """用未舍入值选峰值窗口；并列取最早窗口（严格大于才替换）。"""
    window = timedelta(minutes=WINDOW_MINUTES)
    best_idx = 0
    best_wh = window_totals[0]
    for idx in range(1, WINDOWS_PER_HOUR):
        if window_totals[idx] > best_wh:
            best_wh = window_totals[idx]
            best_idx = idx
    peak_start = hour_start + best_idx * window
    return best_wh, peak_start, peak_start + window


def compute_line_peak(
    line_name: str,
    target_hour: datetime,
    coverages: list[MeterCoverage],
) -> PeakResult:
    """对一条产线的全部表计执行校验、分摊与汇总；任一表不可用即整体不可用。"""
    if not coverages:
        raise PeakUnavailable(
            "no_meters_on_line", f"产线 {line_name!r} 下没有任何表计"
        )
    hour_start = target_hour
    hour_end = target_hour + timedelta(hours=1)

    totals = [0.0] * WINDOWS_PER_HOUR
    meter_ids: list[int] = []
    for cov in coverages:
        meter_ids.append(cov.meter_id)
        per_meter = validate_and_allocate(
            cov.meter_id, cov.readings, cov.has_start, cov.has_end,
            hour_start, hour_end,
        )
        for idx, value in enumerate(per_meter):
            totals[idx] += value

    peak_wh, peak_start, peak_end = pick_peak(totals, hour_start)
    window = timedelta(minutes=WINDOW_MINUTES)
    return PeakResult(
        line_name=line_name,
        target_hour=hour_start,
        window_minutes=WINDOW_MINUTES,
        meter_count=len(coverages),
        windows=[
            PeakWindow(
                window_start=hour_start + idx * window,
                window_end=hour_start + (idx + 1) * window,
                wh=totals[idx],
            )
            for idx in range(WINDOWS_PER_HOUR)
        ],
        peak_wh=peak_wh,
        peak_window_start=peak_start,
        peak_window_end=peak_end,
        meter_ids=sorted(meter_ids),
    )


# ---------------------------------------------------------------------------
# 同快照编排：取数 -> 计算 -> 发布前再校验 revision -> 落库
# ---------------------------------------------------------------------------

# 测试钩子：快照取数完成后、发布前调用（用于注入“计算与更正交错”场景）
SnapshotHook = Callable[[str, datetime], None]

# 模块级测试钩子（生产环境恒为 None；测试可 monkeypatch 注入并发更正）
SNAPSHOT_HOOK: SnapshotHook | None = None


def _to_reading(row: Any) -> Reading:
    read_time, wh = row
    if read_time.tzinfo is None:
        read_time = read_time.replace(tzinfo=timezone.utc)
    else:
        read_time = read_time.astimezone(timezone.utc)
    return Reading(read_time=read_time, wh=int(wh))


def calculate_and_persist(
    line_name: str,
    target_hour: datetime,
    *,
    snapshot_hook: SnapshotHook | None = None,
) -> dict[str, Any]:
    """在同一个已提交快照上完成取数与计算，并把成功结论落库。

    返回可直接用于 API 响应的 dict。任何不可用原因都抛 PeakUnavailable，
    此时不写入结论、不发布部分结果。
    """
    hour_start = target_hour.astimezone(timezone.utc)
    hour_end = hour_start + timedelta(hours=1)
    hook = snapshot_hook if snapshot_hook is not None else SNAPSHOT_HOOK

    session = dbmod.PeakSnapshotSession()
    try:
        session.open()
        revision_at_snapshot = session.current_revision()
        meter_ids = session.list_meters(line_name)
        if not meter_ids:
            raise PeakUnavailable(
                "no_meters_on_line", f"产线 {line_name!r} 下没有任何表计"
            )

        coverages: list[MeterCoverage] = []
        for meter_id in meter_ids:
            has_start, has_end = session.boundary_coverage(
                meter_id, hour_start, hour_end
            )
            rows = session.readings_covering(meter_id, hour_start, hour_end)
            readings = [_to_reading(r) for r in rows]
            coverages.append(
                MeterCoverage(
                    meter_id=meter_id,
                    readings=readings,
                    has_start=has_start,
                    has_end=has_end,
                )
            )

        result = compute_line_peak(line_name, hour_start, coverages)

        if hook is not None:
            hook(line_name, hour_start)

        # 发布闸门：必须用【快照外的新事务】读取当前版本——
        # 快照会话内的 REPEATABLE READ 永远只能看到快照时刻的版本。
        # 计算期间源表若被独立 SQL 更正，本次结论按过期口径标记，
        # 绝不冒充当前口径的新鲜结论。
        revision_now = dbmod.current_revision()
        interleaved = revision_now != revision_at_snapshot
        # 保存的是本轮计算实际依据的快照版本；GET 时与最新版本比对即展示过期
        saved_revision = revision_at_snapshot

        conclusion_id = dbmod.insert_peak_conclusion(
            line_name=line_name,
            target_hour=hour_start,
            result=result,
            data_revision=saved_revision,
            interleaved=interleaved,
        )
    finally:
        session.close()

    # 重新走统一读取路径，保证 POST 与 GET 的响应口径完全一致
    conclusion = dbmod.get_peak_conclusion(conclusion_id)
    if conclusion is None:  # 理论上不可达：插入后同事务外立即可见
        raise dbmod.DbError("峰值结论写入后查询失败")
    return conclusion
