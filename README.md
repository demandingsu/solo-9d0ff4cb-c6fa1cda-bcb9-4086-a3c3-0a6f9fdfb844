# 港航 AIS 靠港停留事件 API

岸基 AIS 点位 → 靠港停留事件，供排放核算使用。FastAPI + SQLite（WAL），
提供 Docker Compose 启动、CSV 上传、事件查询与游标变更流。

## 快速开始

```bash
docker compose up --build
```

* Swagger UI: <http://localhost:8000/docs>
* OpenAPI JSON: <http://localhost:8000/openapi.json>
* 健康检查: <http://localhost:8000/healthz>

演示迟到补点与撤回：在 `/docs` 中展开 **demo → POST /demo/run → Try it out →
Execute**。每次调用创建独立的 `DEMO-XXXXXXXX` 港区，不会污染业务数据。

双实例（共享同一 SQLite 卷，验证交错上传不丢更新）：

```bash
docker compose -f docker-compose.dual.yml up --build
# http://localhost:8001/docs 与 http://localhost:8002/docs
```

## 业务规则

| 规则 | 实现 |
| --- | --- |
| 固定港区矩形范围 | `POST /ports`，同一 `code` 重复登记内容一致时幂等（200），不一致 409 |
| 多次上传 CSV | `POST /ports/{code}/uploads`，含船舶号、UTC 时间、经纬度 |
| 同船同刻同坐标去重 | 批次内与库内均去重，计入 `rows_duplicate` |
| 同船同刻异坐标整批拒收 | 返回 409 `conflicting_coordinates`，整批回滚，不留半批结果 |
| 按时间重排 | 每船按 `(event_time, id)` 排序后推导事件 |
| 相邻在港点 ≤ 10 分钟 | 间隔 > 600 秒断开 |
| 首末相距 ≥ 30 分钟 | 不足 1800 秒不形成事件 |
| 出港点断开 | 矩形外的点作为退出标记，关闭当前事件且不并入 |
| 迟到点改变事件 | 未变事件保留 `event_id`；被改变事件旧号 retract + 新号 upsert |
| 游标顺序读取变更 | `GET /ports/{code}/changes?after=<seq>`，严格递增、可续读 |
| 按船查询港间航段 | `GET /vessels/{vessel_id}/voyages`，全库游标快照可历史还原 |
| 同港相邻不拼接 | 同港区相邻停留不生成航段，也不跳过后再拼接 |
| 时间重叠标冲突 | 相邻停留时间区间重叠时记入 `conflicts`，不生成该段航程 |
| 历史游标可重放 | 变更流仅追加；携带旧游标重查还原当时航段，未来游标 422 |
| 上传原子生效 | 原始点、现役事件、变更记录同一 `BEGIN IMMEDIATE` 事务提交 |
| 两实例交错上传不丢更新 | SQLite WAL + `BEGIN IMMEDIATE` 写串行化 |
| 失败不推进游标 | 事务回滚，`seq` 不前进，无半批结果 |
| 重复上传不重发变更 | `Idempotency-Key`（缺省用内容 SHA-256）唯一约束，重放返回原批次 |

### 事件编号语义

事件以 `(port, vessel, start_time)` 为身份。迟到点只延长结束时间或增加点数时，
仍会 **retract 旧编号 + upsert 新编号**，保证消费方总能拿到完整快照而无需
patch 既有编号；起点不变、载荷不变的事件保留原编号。CSV 中落在港区矩形外的
点位也会入库（作为出港标记），`rows_outside_port` 会统计它们。

### CSV 格式

```csv
vessel,time,lat,lon
SHIP-001,2026-01-01T00:00:00Z,31.20,121.50
SHIP-001,2026-01-01T00:10:00Z,31.201,121.501
```

表头别名（大小写不敏感）：`vessel/船舶号/mmsi/ship_id`、`time/时间/timestamp`、
`lat/纬度`、`lon/lng/longitude/经度`。时间支持 ISO-8601（无时区按 UTC）或
epoch 秒/毫秒。坐标按 1e-6 度取整后比较。上传上限默认 10 MiB / 100,000 行，
可用 `AIS_MAX_UPLOAD_BYTES`、`AIS_MAX_ROWS` 调整。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/ports` | 登记港区矩形范围（幂等） |
| GET | `/ports/{code}` | 读取港区定义 |
| POST | `/ports/{code}/uploads` | 上传 CSV 点位，整批原子生效 |
| GET | `/ports/{code}/events` | 现役事件，按 `event_id` 分页 |
| GET | `/ports/{code}/changes` | 变更流，`after` 游标 + `limit` |
| GET | `/vessels/{vessel_id}/voyages` | 按船查询港间航段与冲突，支持历史游标还原 |
| POST | `/demo/run` | 迟到补点 / retract 演示（`AIS_DEMO_ENABLED=1`） |
| GET | `/healthz` | 健康检查 |

上传响应示例：

```json
{
  "replayed": false,
  "batch_id": 7,
  "rows_received": 15,
  "rows_new": 15,
  "rows_duplicate": 0,
  "rows_outside_port": 0,
  "event_changes": {"upserted": 3, "retracted": 0},
  "change_cursor": {"from": 12, "to": 14}
}
```

变更响应示例（`GET /ports/CNSHA-PD/changes?after=14`）：

```json
{
  "port_id": 1,
  "next_cursor": 16,
  "has_more": false,
  "changes": [
    {"cursor": 15, "type": "retract", "event_id": 41, "vessel_id": "SHIP-001", "...": "..."},
    {"cursor": 16, "type": "upsert", "event_id": 42, "event": {"event_id": 42, "...": "..."}}
  ]
}
```

`seq` 在全库单调递增，同一港区的游标可能不连续；按 `seq > after` 读取即可。
`GET /events` 返回的 `cursor` 与变更流同源，可用于「先快照、后增量」。

## 港间航段（排放核算）

`GET /vessels/{vessel_id}/voyages` 把同一艘船在各港区的停留事件配对成航段。

**游标语义（历史可还原）**

* 首次查询不带 `cursor`：返回当前**全库**事件变更游标，并在该已提交快照上
  配对。保存响应中的 `cursor` 即保存了这次结果的版本。
* 携带旧 `cursor` 重查：服务端从仅追加的 `event_changes` 变更流重放该船
  `seq <= cursor` 的全部 `upsert/retract`，重建当时的现役事件后再配对。
  因此即使期间发生过跨实例上传、事件撤回（retract）或服务重启，旧结果都能
  原样还原，不依赖任何易失内存状态。
* 游标超过当前最新 `seq` 视为「未来游标」，返回 422 `cursor_in_future`，
  避免把当前状态误当作历史结果。

**配对规则**（事件按 `(start_time, event_id)` 排序，只看相邻两项）

1. 时间区间重叠（`first.end_time > second.start_time`）：记入 `conflicts`
   （`reason=overlapping_stays`，同港重叠为 `same_port_overlap`），不生成航段。
2. 同港区且不重叠：不生成航段；该停留仍继续与下一项配对，不会被跳过。
3. 跨港区且不重叠：生成航段，含两端事件编号、港区编号与代码，以及离港
   （前一停留 `end_time`）至进港（后一停留 `start_time`）的 `sailing_seconds`。

响应示例：

```json
{
  "vessel_id": "MV-1",
  "cursor": 2,
  "latest_cursor": 4,
  "snapshot_event_count": 2,
  "voyages": [
    {
      "order": 1,
      "departure": {"event_id": 1, "port_id": 1, "port_code": "CNSHA-PD",
                    "start_time": 1767225600, "end_time": 1767227400},
      "arrival":   {"event_id": 2, "port_id": 2, "port_code": "CNNING-ZJ",
                    "start_time": 1767236400, "end_time": 1767238200},
      "sailing_seconds": 9000
    }
  ],
  "conflicts": []
}
```

时间均为 UTC epoch 秒。未出现过的船返回空 `voyages`/`conflicts`（200，非 404）。

## 本地开发与测试

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
AIS_DEMO_ENABLED=1 uvicorn app.main:app --reload
```

测试覆盖：10 分钟/30 分钟边界、出港断开、批内与库内去重、异坐标整批回滚、
迟到点 retract/upsert 与编号保留、两事件合并、幂等重放、游标分页、港区隔离、
双实例（共享 DB 的并发线程与两个 FastAPI 实例）交错上传；航段配对规则（跨港
成段、同港不拼接、重叠冲突、边界相接、确定性排序）的纯函数单测，以及首次查询
游标、旧游标重放（迟到补点、retract、重启、跨实例上传后仍还原）、未来游标 422、
船舶隔离与空船的 API 测试。`docker build`
未能在本环境执行（无 Docker 守护进程），Compose 文件仅做了 YAML 解析校验。

## 配置

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `AIS_DB_PATH` | `./data/ais.sqlite3` | SQLite 路径（容器内 `/data/ais.sqlite3`） |
| `AIS_DEMO_ENABLED` | `0`（镜像内为 `1`） | 是否注册 `/demo/run` |
| `AIS_MAX_UPLOAD_BYTES` | `10485760` | 单次上传字节上限 |
| `AIS_MAX_ROWS` | `100000` | 单次上传唯一行数上限 |
| `AIS_SQLITE_BUSY_TIMEOUT_MS` | `30000` | 写锁等待时间，超时返回 503 |

## 已知限制

* 变更流只保留写入的记录、不提供保留期清理；长期运行需自行归档。
* 事件推导按船全量重算该港区点位；单船超大数据量时应改为增量窗口。
* SQLite 共享卷适用于单机多实例；跨主机需换成服务端数据库。
* 不做鉴权；生产部署应置于反向代理/网关之后。
