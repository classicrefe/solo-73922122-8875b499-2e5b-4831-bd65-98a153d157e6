"""peaks 纯逻辑测试：校验、按时间比例分摊、汇总、并列取最早。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.peaks import (
    MeterCoverage,
    PeakUnavailable,
    Reading,
    compute_line_peak,
    pick_peak,
    validate_and_allocate,
)

UTC = timezone.utc
H = datetime(2026, 11, 15, 9, 0, tzinfo=UTC)
H_END = H + timedelta(hours=1)


def _r(hh: int, mm: int, wh: int) -> Reading:
    return Reading(datetime(2026, 11, 15, hh, mm, tzinfo=UTC), wh)


def test_quarter_hour_readings_allocated_whole_windows() -> None:
    # 每 15 分钟一条，每窗口增量固定 30 Wh
    readings = [_r(9, 0, 100), _r(9, 15, 130), _r(9, 30, 160),
                _r(9, 45, 190), _r(10, 0, 220)]
    alloc = validate_and_allocate(1, readings, True, True, H, H_END)
    assert alloc == pytest.approx([30.0, 30.0, 30.0, 30.0])


def test_cross_boundary_pair_split_proportionally() -> None:
    # 08:30 -> 09:30 一条跨小时边界的相邻对，差值 60 Wh，时长 60 分钟：
    # 与 [09:00,09:15) 相交 15 分钟 -> 15 Wh；与 [09:15,09:30) 相交 15 分钟 -> 15 Wh
    readings = [_r(8, 30, 40), _r(9, 30, 100), _r(10, 0, 100)]
    alloc = validate_and_allocate(1, readings, True, True, H, H_END)
    assert alloc == pytest.approx([15.0, 15.0, 0.0, 0.0])


def test_partial_overlap_within_window_uses_seconds() -> None:
    # 09:05 -> 09:20，差值 90 Wh / 900s = 0.1 Wh/s
    # w0 相交 600s -> 60 Wh；w1 相交 300s -> 30 Wh
    readings = [_r(9, 0, 0), _r(9, 5, 0), _r(9, 20, 90), _r(10, 0, 90)]
    alloc = validate_and_allocate(1, readings, True, True, H, H_END)
    assert alloc == pytest.approx([60.0, 30.0, 0.0, 0.0])


def test_flat_readings_produce_zero_energy() -> None:
    readings = [_r(9, 0, 500), _r(9, 30, 500), _r(10, 0, 500)]
    alloc = validate_and_allocate(1, readings, True, True, H, H_END)
    assert alloc == pytest.approx([0.0, 0.0, 0.0, 0.0])


def test_missing_start_boundary_unavailable() -> None:
    readings = [_r(9, 30, 100), _r(10, 0, 120)]
    with pytest.raises(PeakUnavailable) as ei:
        validate_and_allocate(7, readings, False, True, H, H_END)
    assert ei.value.reason_code == "missing_boundary_reading"


def test_missing_end_boundary_unavailable() -> None:
    readings = [_r(9, 0, 100), _r(9, 30, 120)]
    with pytest.raises(PeakUnavailable) as ei:
        validate_and_allocate(7, readings, True, False, H, H_END)
    assert ei.value.reason_code == "missing_boundary_reading"


def test_regression_unavailable() -> None:
    readings = [_r(9, 0, 100), _r(9, 30, 90), _r(10, 0, 120)]
    with pytest.raises(PeakUnavailable) as ei:
        validate_and_allocate(7, readings, True, True, H, H_END)
    assert ei.value.reason_code == "reading_regression"


def test_gap_exactly_two_hours_allowed_longer_rejected() -> None:
    # 恰好 2 小时：合法（该对在小时前，不参与分摊，但不能报错）
    ok = [_r(7, 0, 0), _r(9, 0, 10), _r(10, 0, 20)]
    validate_and_allocate(1, ok, True, True, H, H_END)

    bad = [_r(6, 59, 0), _r(9, 0, 10), _r(10, 0, 20)]
    with pytest.raises(PeakUnavailable) as ei:
        validate_and_allocate(1, bad, True, True, H, H_END)
    assert ei.value.reason_code == "reading_gap_too_large"


def test_pick_peak_tie_keeps_earliest_window() -> None:
    totals = [12.5, 12.5, 9.0, 3.0]
    wh, start, end = pick_peak(totals, H)
    assert wh == 12.5
    assert start == H  # 并列取最早
    assert end == H + timedelta(minutes=15)


def test_pick_peak_uses_unrounded_values() -> None:
    # 两个窗口四舍五入后相等，但未舍入值不同：必须按未舍入值选后者
    totals = [10.4999999999, 10.5000000001, 0.0, 0.0]
    wh, start, _end = pick_peak(totals, H)
    assert wh == 10.5000000001
    assert start == H + timedelta(minutes=15)


def test_compute_aggregates_meters_and_picks_peak() -> None:
    m1 = MeterCoverage(
        meter_id=1,
        readings=[_r(9, 0, 0), _r(9, 15, 10), _r(9, 30, 20),
                  _r(9, 45, 30), _r(10, 0, 40)],
        has_start=True, has_end=True,
    )
    m2 = MeterCoverage(
        meter_id=2,
        readings=[_r(9, 0, 0), _r(9, 15, 5), _r(9, 30, 15),
                  _r(9, 45, 20), _r(10, 0, 30)],
        has_start=True, has_end=True,
    )
    result = compute_line_peak("L", H, [m1, m2])
    # 窗口汇总：[15, 20, 15, 20] -> 峰值 20，并列取 09:15
    assert [w.wh for w in result.windows] == pytest.approx([15.0, 20.0, 15.0, 20.0])
    assert result.peak_wh == 20.0
    assert result.peak_window_start == H + timedelta(minutes=15)
    assert result.meter_count == 2
    assert result.meter_ids == [1, 2]


def test_compute_no_meters_unavailable() -> None:
    with pytest.raises(PeakUnavailable) as ei:
        compute_line_peak("空产线", H, [])
    assert ei.value.reason_code == "no_meters_on_line"


def test_compute_one_bad_meter_fails_whole_line_no_partial_result() -> None:
    good = MeterCoverage(
        meter_id=1,
        readings=[_r(9, 0, 0), _r(10, 0, 40)],
        has_start=True, has_end=True,
    )
    bad = MeterCoverage(meter_id=2, readings=[_r(9, 30, 10)],
                        has_start=False, has_end=True)
    with pytest.raises(PeakUnavailable):
        compute_line_peak("L", H, [good, bad])
