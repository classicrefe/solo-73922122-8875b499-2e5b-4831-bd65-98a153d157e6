"""15 分钟峰值 API 端到端集成测试（FastAPI + 真实 PostgreSQL）。"""
from __future__ import annotations

from datetime import datetime, timezone

import psycopg
import pytest
from fastapi.testclient import TestClient

UTC = timezone.utc
HOUR_09 = "2026-11-15T09:00:00Z"

# 测试专用表计/产线，避免与 init.sql 示例数据互相影响
TEST_METER = 9901
TEST_METER_2 = 9902
TEST_LINE = "测试产线Z"
_TEST_METERS = (TEST_METER, TEST_METER_2)


def _admin(dsn: str):
    return psycopg.connect(dsn, autocommit=True)


@pytest.fixture()
def test_line_meters(pg_dsn: str):
    """建测试表计并登记读数；测试结束清理（含结论与版本表不动结构）。"""
    conn = _admin(pg_dsn)
    try:
        # 先清掉可能残留的同 ID 数据，保证每个用例从干净状态开始
        conn.execute(
            "DELETE FROM peak_conclusions WHERE line_name = %s", (TEST_LINE,)
        )
        conn.execute(
            "DELETE FROM readings WHERE meter_id = ANY(%s)", (list(_TEST_METERS),)
        )
        conn.execute(
            "DELETE FROM meters WHERE meter_id = ANY(%s)", (list(_TEST_METERS),)
        )
        conn.execute(
            "INSERT INTO meters (meter_id, line_name) VALUES (%s, %s) "
            "ON CONFLICT DO NOTHING",
            (TEST_METER, TEST_LINE),
        )
        yield conn
    finally:
        conn.execute(
            "DELETE FROM peak_conclusions WHERE line_name = %s", (TEST_LINE,)
        )
        conn.execute(
            "DELETE FROM readings WHERE meter_id = ANY(%s)", (list(_TEST_METERS),)
        )
        conn.execute(
            "DELETE FROM meters WHERE meter_id = ANY(%s)", (list(_TEST_METERS),)
        )
        conn.close()


def _insert_readings(dsn: str, rows: list[tuple]) -> None:
    conn = _admin(dsn)
    try:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO readings (meter_id, read_time, wh) "
                "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                rows,
            )
    finally:
        conn.close()


def _ts(h: int, m: int = 0) -> datetime:
    return datetime(2026, 11, 15, h, m, tzinfo=UTC)


class _ReadingRestorer:
    """更正示例读数前记住原值，测试结束精确还原，避免污染共享数据。"""

    def __init__(self, dsn: str):
        self._dsn = dsn
        self._saved: list[tuple[int, datetime, int]] = []

    def update(self, meter_id: int, ts: datetime, new_wh: int) -> None:
        conn = _admin(self._dsn)
        try:
            row = conn.execute(
                "SELECT wh FROM readings WHERE meter_id = %s AND read_time = %s",
                (meter_id, ts),
            ).fetchone()
            if row is not None:
                self._saved.append((meter_id, ts, int(row[0])))
            conn.execute(
                "UPDATE readings SET wh = %s WHERE meter_id = %s AND read_time = %s",
                (new_wh, meter_id, ts),
            )
        finally:
            conn.close()

    def restore(self) -> None:
        conn = _admin(self._dsn)
        try:
            for meter_id, ts, wh in self._saved:
                conn.execute(
                    "UPDATE readings SET wh = %s "
                    "WHERE meter_id = %s AND read_time = %s",
                    (wh, meter_id, ts),
                )
        finally:
            conn.close()
        self._saved.clear()


def test_demo_line_a_peak_end_to_end(
    client: TestClient, test_line_meters, pg_dsn: str
) -> None:
    # 用测试专用表计复刻 init.sql 中 A线两块表的 09:00-10:00 读数，
    # 断言不受其他用例对示例数据的更正影响
    # 第一块表：窗口增量 [30, 30, 110, 0]
    wh1 = [(9, 0, 0), (9, 15, 30), (9, 30, 60), (9, 45, 170), (10, 0, 170)]
    # 第二块表：窗口增量 [30, 30, 30, 30]
    wh2 = [(9, 0, 0), (9, 15, 30), (9, 30, 60), (9, 45, 90), (10, 0, 120)]
    rows = []
    for (h, m, wh) in wh1:
        rows.append((TEST_METER, datetime(2026, 11, 15, h, m, tzinfo=UTC), wh))
    conn = _admin(pg_dsn)
    try:
        conn.execute(
            "INSERT INTO meters (meter_id, line_name) VALUES (%s, %s) "
            "ON CONFLICT DO NOTHING",
            (TEST_METER_2, TEST_LINE),
        )
        for (h, m, wh) in wh2:
            rows.append(
                (TEST_METER_2, datetime(2026, 11, 15, h, m, tzinfo=UTC), wh)
            )
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO readings (meter_id, read_time, wh) "
                "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                rows,
            )
    finally:
        conn.close()

    resp = client.post("/peaks", json={"line_name": TEST_LINE, "target_hour": HOUR_09})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "available"
    assert body["stale"] is False
    assert body["interleaved"] is False
    assert body["meter_count"] == 2
    assert body["meter_ids"] == [TEST_METER, TEST_METER_2]
    assert body["window_minutes"] == 15
    # 未舍入窗口电量：[30,30,110,0] + [30,30,30,30] = [60,60,140,30]
    assert [round(w["wh"], 6) for w in body["windows"]] == [60.0, 60.0, 140.0, 30.0]
    assert body["peak_wh"] == 140.0
    assert body["peak_window_start"] == "2026-11-15T09:30:00Z"
    assert body["peak_window_end"] == "2026-11-15T09:45:00Z"
    # 结论可查询
    cid = body["conclusion_id"]
    got = client.get(f"/peaks/{cid}")
    assert got.status_code == 200
    assert got.json()["peak_wh"] == 140.0
    listed = client.get("/peaks", params={"line_name": TEST_LINE}).json()
    assert any(c["conclusion_id"] == cid for c in listed)


def test_no_meters_on_line_is_unavailable_without_persisting(
    client: TestClient,
) -> None:
    resp = client.post(
        "/peaks", json={"line_name": "不存在的产线XYZ", "target_hour": HOUR_09}
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["reason_code"] == "no_meters_on_line"
    # 不发布任何结论
    assert (
        client.get("/peaks", params={"line_name": "不存在的产线XYZ"}).json() == []
    )


def test_missing_boundary_reading_unavailable(
    client: TestClient, test_line_meters, pg_dsn: str
) -> None:
    # 只有小时内读数，缺少 <= 09:00 的起始边界读数
    _insert_readings(pg_dsn, [
        (TEST_METER, _ts(9, 15), 100),
        (TEST_METER, _ts(9, 45), 130),
        (TEST_METER, _ts(10, 0), 140),
    ])
    resp = client.post("/peaks", json={"line_name": TEST_LINE, "target_hour": HOUR_09})
    assert resp.status_code == 422
    assert resp.json()["detail"]["reason_code"] == "missing_boundary_reading"
    assert client.get("/peaks", params={"line_name": TEST_LINE}).json() == []


def test_reading_regression_unavailable(
    client: TestClient, test_line_meters, pg_dsn: str
) -> None:
    _insert_readings(pg_dsn, [
        (TEST_METER, _ts(9, 0), 100),
        (TEST_METER, _ts(9, 30), 80),   # 回退
        (TEST_METER, _ts(10, 0), 120),
    ])
    resp = client.post("/peaks", json={"line_name": TEST_LINE, "target_hour": HOUR_09})
    assert resp.status_code == 422
    assert resp.json()["detail"]["reason_code"] == "reading_regression"


def test_gap_over_two_hours_unavailable(
    client: TestClient, test_line_meters, pg_dsn: str
) -> None:
    # 起始边界读数 09:00、结束边界读数 10:00，间隔 1 小时，合法
    _insert_readings(pg_dsn, [
        (TEST_METER, _ts(9, 0), 10),
        (TEST_METER, _ts(10, 0), 40),
    ])
    ok = client.post("/peaks", json={"line_name": TEST_LINE, "target_hour": HOUR_09})
    assert ok.status_code == 201

    # 插入一条更早的读数并移除 09:00：小时前最近读数变为 06:59，
    # 它与下一条 10:00 的相邻对跨越整个目标小时，间隔 3 小时 1 分，必须拒绝
    _insert_readings(pg_dsn, [(TEST_METER, _ts(6, 59), 0)])
    conn = _admin(pg_dsn)
    try:
        conn.execute(
            "DELETE FROM readings WHERE meter_id = %s AND read_time = %s",
            (TEST_METER, _ts(9, 0)),
        )
    finally:
        conn.close()
    bad = client.post("/peaks", json={"line_name": TEST_LINE, "target_hour": HOUR_09})
    assert bad.status_code == 422
    assert bad.json()["detail"]["reason_code"] == "reading_gap_too_large"


def test_proportional_split_across_window_boundaries(
    client: TestClient, test_line_meters, pg_dsn: str
) -> None:
    # 整点读数：每小时 120 Wh，线性速率 2 Wh/分钟 -> 每 15 分钟窗口 30 Wh
    _insert_readings(pg_dsn, [
        (TEST_METER, _ts(8, 0), 0),
        (TEST_METER, _ts(9, 0), 120),
        (TEST_METER, _ts(10, 0), 240),
        (TEST_METER, _ts(11, 0), 360),
    ])
    body = client.post(
        "/peaks", json={"line_name": TEST_LINE, "target_hour": HOUR_09}
    ).json()
    assert [round(w["wh"], 6) for w in body["windows"]] == [30.0, 30.0, 30.0, 30.0]
    # 全并列 -> 最早窗口
    assert body["peak_window_start"] == "2026-11-15T09:00:00Z"


def test_naive_and_non_integral_hour_rejected(client: TestClient) -> None:
    # 无时区：pydantic 能解析，但路由层强制要求带时区
    r1 = client.post(
        "/peaks",
        json={"line_name": "A线", "target_hour": "2026-11-15T09:00:00"},
    )
    assert r1.status_code == 400
    assert "时区" in r1.json()["detail"]["message"]
    # 非整点
    r2 = client.post(
        "/peaks",
        json={"line_name": "A线", "target_hour": "2026-11-15T09:30:00Z"},
    )
    assert r2.status_code == 400
    assert "整点" in r2.json()["detail"]["message"]


def test_timezone_offset_normalized_to_utc(client: TestClient) -> None:
    # 10:00+01:00 == 09:00 UTC
    resp = client.post(
        "/peaks",
        json={"line_name": "A线", "target_hour": "2026-11-15T10:00:00+01:00"},
    )
    assert resp.status_code == 201
    assert resp.json()["target_hour"] == "2026-11-15T09:00:00Z"


def test_get_unknown_conclusion_404(client: TestClient) -> None:
    assert client.get("/peaks/nonexistent").status_code == 404


def test_source_correction_marks_old_conclusion_stale(
    client: TestClient, pg_dsn: str
) -> None:
    first = client.post(
        "/peaks", json={"line_name": "A线", "target_hour": HOUR_09}
    ).json()
    assert first["stale"] is False
    cid = first["conclusion_id"]

    # 独立 SQL 更正 readings（语句级触发器推进 data_revision）
    restorer = _ReadingRestorer(pg_dsn)
    try:
        restorer.update(1001, _ts(9, 15), 1181)
        old = client.get(f"/peaks/{cid}").json()
        assert old["stale"] is True
        assert old["status"] == "stale"
        assert old["data_revision"] < old["latest_revision"]
    finally:
        restorer.restore()


def test_meter_change_also_marks_conclusion_stale(
    client: TestClient, pg_dsn: str
) -> None:
    first = client.post(
        "/peaks", json={"line_name": "A线", "target_hour": HOUR_09}
    ).json()
    conn = _admin(pg_dsn)
    try:
        conn.execute(
            "INSERT INTO meters (meter_id, line_name) VALUES (9999, '临时线')"
        )
        listed = client.get(f"/peaks/{first['conclusion_id']}").json()
        assert listed["stale"] is True
    finally:
        conn.execute("DELETE FROM meters WHERE meter_id = 9999")
        conn.close()


def test_recompute_supersedes_old_and_failure_keeps_old_stale(
    client: TestClient, pg_dsn: str
) -> None:
    first = client.post(
        "/peaks", json={"line_name": "A线", "target_hour": HOUR_09}
    ).json()
    # 一次无数据变化的重算：旧结论被取代（superseded），新结论可用
    second = client.post(
        "/peaks", json={"line_name": "A线", "target_hour": HOUR_09}
    ).json()
    assert second["conclusion_id"] != first["conclusion_id"]
    assert second["superseded"] is False
    old = client.get(f"/peaks/{first['conclusion_id']}").json()
    assert old["superseded"] is True
    assert old["stale"] is True

    # 把 A线 改成不可算（删一条边界读数），重算必须失败且保留旧结论
    conn = _admin(pg_dsn)
    try:
        conn.execute(
            "DELETE FROM readings WHERE meter_id = 1001 "
            "AND read_time = TIMESTAMPTZ '2026-11-15 10:00:00+00'"
        )
        failed = client.post(
            "/peaks", json={"line_name": "A线", "target_hour": HOUR_09}
        )
        assert failed.status_code == 422
        assert failed.json()["detail"]["reason_code"] in (
            "missing_boundary_reading",
            "reading_gap_too_large",
        )
        # 旧结论仍可查询，且保持过期状态，没有被失败的重算覆盖
        kept = client.get(f"/peaks/{first['conclusion_id']}").json()
        assert kept["conclusion_id"] == first["conclusion_id"]
        assert kept["stale"] is True
        kept2 = client.get(f"/peaks/{second['conclusion_id']}").json()
        assert kept2["stale"] is True
    finally:
        conn.execute(
            "INSERT INTO readings (meter_id, read_time, wh) "
            "VALUES (1001, TIMESTAMPTZ '2026-11-15 10:00:00+00', 1320) "
            "ON CONFLICT DO NOTHING"
        )
        conn.close()


def test_interleaved_correction_publishes_only_stale_conclusion(
    client: TestClient, monkeypatch, pg_dsn: str
) -> None:
    """计算与更正交错：发布闸门必须把结论标记为 stale/interleaved，
    绝不能冒充当前口径的新鲜结论。"""
    from app import peaks as peaks_mod

    restorer = _ReadingRestorer(pg_dsn)

    def hook(line_name: str, target_hour: datetime) -> None:
        # 精确改成原值 +3，结束后按捕获的原值还原
        restorer.update(1002, _ts(9, 30), 703)

    try:
        monkeypatch.setattr(peaks_mod, "SNAPSHOT_HOOK", hook)
        body = client.post(
            "/peaks", json={"line_name": "A线", "target_hour": HOUR_09}
        ).json()
        assert body["interleaved"] is True
        assert body["stale"] is True
        assert body["status"] == "stale"
        # 结论保存的是快照口径的版本，落后于当前版本
        assert body["data_revision"] < body["latest_revision"]
        monkeypatch.setattr(peaks_mod, "SNAPSHOT_HOOK", None)

        # 再算一次（无新更正）得到新鲜结论
        fresh = client.post(
            "/peaks", json={"line_name": "A线", "target_hour": HOUR_09}
        ).json()
        assert fresh["stale"] is False and fresh["interleaved"] is False
    finally:
        monkeypatch.setattr(peaks_mod, "SNAPSHOT_HOOK", None)
        restorer.restore()


def test_snapshot_isolation_concurrent_correction_invisible_to_round(
    client: TestClient, monkeypatch, pg_dsn: str
) -> None:
    """更正发生在快照取数之后时，本轮结论反映的是快照口径，
    且发布闸门如实标记交错（旧口径不冒充新口径）。"""
    from app import peaks as peaks_mod

    baseline = client.post(
        "/peaks", json={"line_name": "A线", "target_hour": HOUR_09}
    ).json()

    def hook(line_name: str, target_hour: datetime) -> None:
        writer = _admin(pg_dsn)
        try:
            writer.execute(
                "INSERT INTO readings VALUES (1001, %s, 9999) ON CONFLICT DO NOTHING",
                (_ts(12, 0),),
            )
        finally:
            writer.close()

    monkeypatch.setattr(peaks_mod, "SNAPSHOT_HOOK", hook)
    body = client.post(
        "/peaks", json={"line_name": "A线", "target_hour": HOUR_09}
    ).json()
    # 快照内看不到新读数 -> 计算值与基线完全一致
    assert body["peak_wh"] == baseline["peak_wh"]
    assert body["interleaved"] is True and body["stale"] is True
    monkeypatch.setattr(peaks_mod, "SNAPSHOT_HOOK", None)

    conn = _admin(pg_dsn)
    try:
        conn.execute(
            "DELETE FROM readings WHERE meter_id = 1001 AND read_time = %s",
            (_ts(12, 0),),
        )
    finally:
        conn.close()


def test_jobs_endpoints_still_available(client: TestClient) -> None:
    """保留 /jobs 能力：健康检查与任务列表不受新功能影响。"""
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/jobs").status_code == 200
