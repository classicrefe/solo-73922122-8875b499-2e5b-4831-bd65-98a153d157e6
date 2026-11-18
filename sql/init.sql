-- ============================================================
-- 电表日结核对：源表结构 + 示例数据
-- 容器首次启动时由 /docker-entrypoint-initdb.d 自动执行
-- ============================================================

-- 表计档案：表号 -> 产线
CREATE TABLE IF NOT EXISTS meters (
    meter_id  bigint PRIMARY KEY,          -- 表号
    line_name text   NOT NULL              -- 产线
);

-- 读数流水：表号、时刻、累计电量 Wh
CREATE TABLE IF NOT EXISTS readings (
    meter_id   bigint NOT NULL REFERENCES meters(meter_id),
    read_time  timestamptz NOT NULL,       -- 时刻
    wh         bigint NOT NULL,            -- 累计电量（Wh）
    PRIMARY KEY (meter_id, read_time)
);

-- 日结常用过滤索引
CREATE INDEX IF NOT EXISTS idx_readings_time ON readings (read_time);

-- ---------- 示例档案：3 条产线、6 块表 ----------
INSERT INTO meters (meter_id, line_name) VALUES
    (1001, 'A线'),
    (1002, 'A线'),
    (2001, 'B线'),
    (2002, 'B线'),
    (3001, 'C线'),
    (3002, 'C线')
ON CONFLICT DO NOTHING;

-- ---------- 示例读数：2026-11-15 当日整点 ----------
INSERT INTO readings (meter_id, read_time, wh) VALUES
    (1001, TIMESTAMPTZ '2026-11-15 08:00:00+00', 1000),
    (1001, TIMESTAMPTZ '2026-11-15 09:00:00+00', 1150),
    (1001, TIMESTAMPTZ '2026-11-15 10:00:00+00', 1320),
    (1002, TIMESTAMPTZ '2026-11-15 08:00:00+00',  500),
    (1002, TIMESTAMPTZ '2026-11-15 09:00:00+00',  640),
    (2001, TIMESTAMPTZ '2026-11-15 08:00:00+00', 2000),
    (2001, TIMESTAMPTZ '2026-11-15 09:00:00+00', 2210),
    (2002, TIMESTAMPTZ '2026-11-15 08:00:00+00',  900),
    (3001, TIMESTAMPTZ '2026-11-15 08:00:00+00',  700),
    (3001, TIMESTAMPTZ '2026-11-15 09:00:00+00',  880),
    (3002, TIMESTAMPTZ '2026-11-15 08:00:00+00',  300),
    (3002, TIMESTAMPTZ '2026-11-15 09:00:00+00',  420)
ON CONFLICT DO NOTHING;

-- ---------- 峰值 API 演示读数：A线两块表在 09:00-10:00 的 15 分钟粒度 ----------
-- 累计 Wh 单调非降；覆盖小时边界（09:00 与 10:00 各有一条），可直接算出
-- 4 个 15 分钟窗口的分摊电量。1001 的 09:30-09:45 增量最大（110 Wh）。
INSERT INTO readings (meter_id, read_time, wh) VALUES
    (1001, TIMESTAMPTZ '2026-11-15 09:15:00+00', 1180),
    (1001, TIMESTAMPTZ '2026-11-15 09:30:00+00', 1210),
    (1001, TIMESTAMPTZ '2026-11-15 09:45:00+00', 1320),
    (1002, TIMESTAMPTZ '2026-11-15 09:15:00+00',  670),
    (1002, TIMESTAMPTZ '2026-11-15 09:30:00+00',  700),
    (1002, TIMESTAMPTZ '2026-11-15 09:45:00+00',  730),
    (1002, TIMESTAMPTZ '2026-11-15 10:00:00+00',  760)
ON CONFLICT DO NOTHING;

-- ============================================================
-- 数据修订版本：meters / readings 的任何独立更正都使 revision +1
-- 语句级触发器（含 TRUNCATE），批量更正也只推进一次版本号。
-- 峰值结论保存其计算口径对应的 revision；查询时落后即展示“已过期”。
-- ============================================================
CREATE TABLE IF NOT EXISTS data_revision (
    id       smallint PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    revision bigint NOT NULL DEFAULT 0
);
INSERT INTO data_revision (id, revision) VALUES (1, 0)
ON CONFLICT (id) DO NOTHING;

CREATE OR REPLACE FUNCTION bump_data_revision() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    UPDATE data_revision SET revision = revision + 1 WHERE id = 1;
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS trg_meters_revision ON meters;
CREATE TRIGGER trg_meters_revision
    AFTER INSERT OR UPDATE OR DELETE OR TRUNCATE ON meters
    FOR EACH STATEMENT EXECUTE FUNCTION bump_data_revision();

DROP TRIGGER IF EXISTS trg_readings_revision ON readings;
CREATE TRIGGER trg_readings_revision
    AFTER INSERT OR UPDATE OR DELETE OR TRUNCATE ON readings
    FOR EACH STATEMENT EXECUTE FUNCTION bump_data_revision();

-- ============================================================
-- 峰值结论表：只保存成功结论；不可用原因不落库（不发布部分结论）
-- ============================================================
CREATE TABLE IF NOT EXISTS peak_conclusions (
    conclusion_id     text PRIMARY KEY,
    line_name         text        NOT NULL,
    target_hour       timestamptz NOT NULL,
    meter_count       integer     NOT NULL,
    meter_ids         bigint[]    NOT NULL,
    window_minutes    integer     NOT NULL,
    window_starts     timestamptz[] NOT NULL,
    window_ends       timestamptz[] NOT NULL,
    window_wh         double precision[] NOT NULL,   -- 未舍入的各窗口电量
    peak_wh           double precision NOT NULL,     -- 未舍入的峰值电量
    peak_window_start timestamptz NOT NULL,
    peak_window_end   timestamptz NOT NULL,
    data_revision     bigint      NOT NULL,          -- 计算口径对应的数据版本
    interleaved       boolean     NOT NULL DEFAULT FALSE, -- 发布闸门：计算期间源表被更正
    superseded        boolean     NOT NULL DEFAULT FALSE, -- 被同产线同小时的新结论取代
    created_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_peak_line_hour
    ON peak_conclusions (line_name, target_hour);

-- ============================================================
-- 示例核对查询（两条都只输出 bigint / text，列数、类型一致）
-- 场景：按“产线 + 小时”汇总当日每块表相对上一读数的增量电量（Wh）
--   left  —— 数仓工程师改写后的写法（CTE + 窗口差值 + 外连接聚合）
--   right —— 基准写法（子查询 + CASE 判空 + 内连接聚合）
-- 把这两条分别贴进 POST /jobs 的 left_sql / right_sql 即可。
-- 注意所有数值列都显式 ::bigint，文本列为 text，满足列类型约束。
-- ============================================================

-- LEFT（改写版）：
-- WITH delta AS (
--     SELECT r.meter_id,
--            EXTRACT(HOUR FROM r.read_time)::bigint AS hr,
--            (r.wh - LAG(r.wh, 1, r.wh)
--                   OVER (PARTITION BY r.meter_id ORDER BY r.read_time))::bigint AS d
--     FROM readings r
--     WHERE r.read_time >= TIMESTAMPTZ '2026-11-15 00:00:00+00'
--       AND r.read_time <  TIMESTAMPTZ '2026-11-16 00:00:00+00'
-- )
-- SELECT m.line_name,
--        d.hr,
--        SUM(d.d)::bigint AS wh_delta
-- FROM delta d
-- JOIN meters m ON m.meter_id = d.meter_id
-- GROUP BY m.line_name, d.hr
-- ORDER BY m.line_name, d.hr;

-- RIGHT（基准版，写法不同但结果应一致；ORDER BY 会被忽略，行序不参与比较）：
-- SELECT line_name, hr, SUM(d)::bigint AS wh_delta FROM (
--     SELECT m.line_name AS line_name,
--            EXTRACT(HOUR FROM r.read_time)::bigint AS hr,
--            COALESCE(r.wh - LAG(r.wh) OVER w, 0)::bigint AS d
--     FROM readings r
--     JOIN meters m ON m.meter_id = r.meter_id
--     WHERE r.read_time >= TIMESTAMPTZ '2026-11-15 00:00:00+00'
--       AND r.read_time <  TIMESTAMPTZ '2026-11-16 00:00:00+00'
--     WINDOW w AS (PARTITION BY r.meter_id ORDER BY r.read_time)
-- ) baseline
-- GROUP BY line_name, hr
-- ORDER BY line_name, hr;
