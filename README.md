# 电表日结 SQL 核对 + 15 分钟用电峰值 API

本服务提供两类能力：

1. **日结 SQL 核对**：数仓工程师改写电表日结的关联 SQL 后，用本服务确认读数
   **没有漏计或重复计入**。提交两条只读 `SELECT`，服务在同一已提交快照上并行
   读取两侧结果，按 **行内容 + 出现次数**做多重集核对（忽略行序，`NULL` 与空串
   视为不同），返回一致结论或最多 20 种差异行及两侧次数。
2. **15 分钟用电峰值**（新增）：面向产线短时负荷排查，按指定 UTC 整点小时计算
   该产线全部表计的 15 分钟窗口用电峰值，结论持久化、可查询，源表更正后旧结论
   自动展示为过期。

## 1. 快速启动

```bash
docker compose up --build
# API:   http://localhost:8000
# 文档:  http://localhost:8000/docs   （Swagger UI，可直接发请求演示）
```

启动后 PostgreSQL 自动执行 `sql/init.sql`，建好 `meters` / `readings`
源表并写入示例数据；文件末尾附有可直接粘贴到 `/docs` 的两条等价示例查询。

停止并清理：

```bash
docker compose down -v
```

## 2. 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET`  | `/health` | 存活探针，不触碰数据库 |
| `POST` | `/jobs` | 提交核对任务（同时只允许一组运行，占用中返回 `409`） |
| `GET`  | `/jobs` | 列出近期任务（含终态），状态查询始终可用 |
| `GET`  | `/jobs/{job_id}` | 查询单个任务状态与结论 |
| `POST` | `/jobs/{job_id}/cancel` | 请求取消，及时受理 |
| `POST` | `/peaks` | 计算并保存指定 UTC 整点小时的 15 分钟峰值（不可用返回 `422`） |
| `GET`  | `/peaks` | 列出已保存峰值结论（可按产线/小时过滤，含过期结论） |
| `GET`  | `/peaks/{conclusion_id}` | 查询单个峰值结论（过期状态实时判定） |

任务状态：`pending → running →（cancelling）→ succeeded | failed | cancelled | timeout`。
失败、取消、超时一律 **不发布比较结论**（`result` 为 `null`），仅给出原因。

## 2A. 15 分钟用电峰值 API

### 计算口径

- 按**同一已提交快照**中的 `meters.line_name` 选取该产线的全部表计；
- 读取覆盖目标小时 `[H, H+1h)` 的 `readings`（含小时前最近一条与小时后最早
  一条，用于跨边界相邻对的分摊）；
- 相邻累计 Wh 差值按**相交时长比例**线性分摊到 4 个 15 分钟窗口，再按窗口
  汇总各表计；
- 用**未舍入值**比较各窗口电量，并列时取**最早窗口**，返回峰值电量与窗口。

### 不可用即整体不可用（不发布部分结论）

出现下列任一情况，返回 `422` 并给出 `reason_code`，不写结论：

| `reason_code` | 触发条件 |
| --- | --- |
| `no_meters_on_line` | 该产线下没有任何表计 |
| `missing_boundary_reading` | 任一表计缺少覆盖小时起始或结束边界的读数 |
| `reading_regression` | 任一表计相邻读数累计 Wh 回退（变小） |
| `reading_gap_too_large` | 任一表计覆盖小时的相邻读数间隔超过 2 小时（恰好 2 小时合法） |

### 请求 / 响应示例

```bash
# A线 2026-11-15 09:00 UTC（init.sql 已备好 15 分钟粒度演示读数）
curl -X POST http://localhost:8000/peaks \
  -H 'Content-Type: application/json' \
  -d '{"line_name": "A线", "target_hour": "2026-11-15T09:00:00Z"}'
```

`target_hour` 必须是**带时区的 UTC 整点**（如 `...Z` 或 `+00:00`；带偏移量
会自动归一到 UTC），无时区或非整点返回 `400`。成功响应：

```json
{
  "conclusion_id": "…",
  "line_name": "A线",
  "target_hour": "2026-11-15T09:00:00Z",
  "status": "available",
  "stale": false,
  "superseded": false,
  "interleaved": false,
  "data_revision": 3,
  "latest_revision": 3,
  "meter_count": 2,
  "meter_ids": [1001, 1002],
  "window_minutes": 15,
  "windows": [
    {"window_start": "2026-11-15T09:00:00Z", "window_end": "…09:15:00Z", "wh": 60.0},
    {"window_start": "…09:15:00Z", "window_end": "…09:30:00Z", "wh": 60.0},
    {"window_start": "…09:30:00Z", "window_end": "…09:45:00Z", "wh": 140.0},
    {"window_start": "…09:45:00Z", "window_end": "…10:00:00Z", "wh": 30.0}
  ],
  "peak_wh": 140.0,
  "peak_window_start": "2026-11-15T09:30:00Z",
  "peak_window_end": "2026-11-15T09:45:00Z",
  "created_at": "…"
}
```

### 过期（陈旧）语义

- `meters` / `readings` 经独立 SQL `INSERT/UPDATE/DELETE/TRUNCATE` 更正后，
  库内 `data_revision` 由语句级触发器单调 +1；
- 结论保存其计算口径对应的 `data_revision`；查询时版本落后、或被同产线同小时
  的新结论取代（`superseded`），即展示 `"status": "stale"`；
- **计算与更正交错**：发布前在快照外重新读取当前版本，若已变化，则该结论按
  过期口径保存并置 `interleaved=true / stale=true`，绝不冒充当前口径的新鲜
  结论；
- 重算失败（如重算时数据已不可用）时**保留旧结论**，其过期状态不变，失败轮次
  不写入任何记录。

### 请求示例

```bash
curl -X POST http://localhost:8000/jobs -H 'Content-Type: application/json' -d '{
  "left_sql":  "SELECT meter_id::bigint, wh::bigint FROM readings",
  "right_sql": "SELECT meter_id::bigint, wh::bigint FROM readings",
  "name": "daily-settlement-v2"
}'
```

### 结论结构（成功时）

```json
{
  "status": "succeeded",
  "result": {
    "identical": false,
    "columns": ["meter_id", "wh"],
    "column_types": ["bigint", "bigint"],
    "left_row_total": 504013,
    "right_row_total": 504012,
    "matched_group_count": 504011,
    "diff_group_total": 1,
    "diffs_shown": 1,
    "diffs": [
      {"values": [1001, 1150], "left_count": 2, "right_count": 1}
    ]
  }
}
```

## 3. 对提交 SQL 的约束

- 必须是**单条只读 SELECT**（允许 `WITH` / `UNION` / `INTERSECT` / `EXCEPT`），
  静态解析拒绝多语句与任何写操作 / DDL / `SELECT INTO` / `LOCK` / `CALL` 等；
- 结果列只允许 `bigint` 与 `text`，两侧**列数相同、对应位置类型相同**，
  否则本轮失败且不给出比较结论；
- 数据库连接再叠加一层纵深防御：`READ ONLY` 事务、`statement_timeout`、
  `lock_timeout`，即使静态校验被绕过也无法写入或长时间持锁。

## 4. 关键设计

### 同一已提交快照，源表仍可更正

每条任务开三条独立连接：快照连接执行
`BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY` + `pg_export_snapshot()`，
两条读取连接分别 `SET TRANSACTION SNAPSHOT` 绑定**同一个快照**。
核对期间其他会话对 `readings` / `meters` 的更正照常提交，但对本轮不可见。

### 内存有界的外部归并（不全量载入、不靠哈希判等）

1. 命名服务端游标按 `itersize` 分批拉取，客户端从不缓存整表；
2. 每 `SORT_CHUNK_ROWS`（默认 2 万）行在进程内排序，逐行流式 pickle 落盘为一个 run；
3. 归并阶段对两侧 run 做单轮 k 路堆归并（堆中每流只驻留一行），
   键随行走，按全序键聚合等值组后比较两侧次数；
4. 差异明细最多保留 20 种，但总行数、差异组总数等计数覆盖全量数据；
5. 全程不使用整表哈希作为判等依据。

### 单名额、30 秒时限与终止语义

- 进程内单名额信号量：全局同时只运行一组，冲突返回 `409`；
- 看门狗按 `JOB_TIMEOUT_SECONDS`（默认 30）判定超时；
- 取消/超时/任一侧失败后：`conn.cancel()` 请求中止本轮全部 SQL，
  必要时 `pg_terminate_backend` 兜底；
- 严格按顺序收尾：**中止 SQL → 关闭连接 → 清空暂存目录 → 释放名额 → 才落终态**，
  因此状态查询一旦看到终态，名额必然已经释放、暂存必然已经清空。

### 峰值 API 的同快照与过期口径

- 峰值计算在单连接 `REPEATABLE READ READ ONLY` 事务内完成，取数期间其他会话的
  更正对本轮不可见；
- `data_revision` 单行版本表 + `meters`/`readings` 上的语句级触发器，任何独立
  更正（含批量、`TRUNCATE`）都使版本 +1；
- 发布闸门在快照**外**重新读取版本，识别“计算与更正交错”，交错结论一律按过期
  口径标记；
- 结论表只保存成功结论；同产线同小时重算用事务级咨询锁串行化，旧结论在同一事务
  内置 `superseded`，重算失败不影响旧结论。

## 5. 配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `PG_DSN` | `postgresql://recon:recon@postgres:5432/recon` | 数据库连接串 |
| `JOB_TIMEOUT_SECONDS` | `30` | 单轮硬时限 |
| `PG_STATEMENT_TIMEOUT_MS` | `28000` | 服务端单语句超时（留 2s 收尾余量） |
| `SORT_CHUNK_ROWS` | `20000` | 单个排序块行数 |
| `MAX_DIFF_TYPES` | `20` | 差异展示上限 |
| `WORK_DIR` | `/tmp/recon-work` | 外部排序暂存目录（独立卷） |

## 6. 本地自测

```bash
# 单元 + 集成测试（集成测试需要一个 PostgreSQL；可用 PG_DSN 指定）
python -m pytest tests/ -q

# 50 万行级端到端压测（内存峰值、暂存占用、结论正确性、清理与名额释放）
python scripts/load_test.py
```

实测（受限沙箱环境，容器内通常更快）：两侧各 504,012 行、
含一次重复计入差异，端到端约 16 秒，进程峰值 RSS 约 68 MiB，
暂存峰值约 44 MiB，结束后暂存目录清空、名额释放。

## 7. 目录结构

```
app/
  config.py          运行期配置
  sqlvalidation.py   只读 SELECT 静态校验
  db.py              快照会话、只读连接、命名游标流式拉取、列类型闸门；
                     峰值快照会话、版本读取、结论持久化与陈旧判定
  external_merge.py  流式 run、k 路归并、多重集核对
  peaks.py           15 分钟峰值：纯函数分摊/选峰 + 同快照编排与发布闸门
  manager.py         单名额编排、看门狗、取消/超时/失败的统一收尾
  models.py          API 模型（核对任务 + 峰值结论）
  main.py            FastAPI 路由（/jobs 与 /peaks）
sql/init.sql         源表、版本表/触发器、峰值结论表、示例数据、示例查询
scripts/             压测数据生成与负载脚本
tests/               单元与集成测试（含峰值口径、过期、交错发布闸门）
docker-compose.yml   应用（128MiB 上限）+ PostgreSQL
Dockerfile
```
