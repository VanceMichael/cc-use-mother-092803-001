# 跨区域运力调度

面向中秋等大客流场景的**可独立运行运力决策服务**：各城市按运营日提交线路
加班需求，系统完成时间窗口锁定与容量校验后，交给有权限的调度员复核；查询
直接呈现当前有效决定、形成原因与责任人。

仅依赖 Python 3.10+ 标准库，数据落在一个 SQLite 文件中，无需任何外部服务。

## 解决的现场问题

| 现场痛点 | 机制 |
| --- | --- |
| 表格版本互相覆盖，不知道剩余容量 | 容量不做可覆盖的计数列；占用由 `events` 事件流实时汇总（批准 +、撤销 −），所有写操作在 `BEGIN IMMEDIATE` 事务内完成 |
| 哪次批复仍然有效说不清 | 申请单带单调递增版本号；复核可携带 `expected_version`，基于旧版本的批复直接拒绝；只有 `approved` 是当前有效决定 |
| 同一申请重试重复占配额 | 每条命令必须带调用方生成的 `request_id`（幂等键）；重试只回放首次结果，返回中带 `"replayed": true` |
| 逾期批次挤占新窗口 | 申请按命令时间锁定到唯一窗口批次；窗口截止后显式拒绝（`window_closed`），批次容量相互独立 |
| 谁批的、为什么查不到 | 每次提交/修订/批准/驳回/撤销都落事件（动作人、原因、时间），query 原样返回 |
| 审批中断、服务重启后状态丢失 | 全部状态持久化在 SQLite；重启后用同一 `request_id` 重放即可从原进度继续，配额不会二次扣减 |

## 快速开始

```bash
python3 -m unittest discover -s tests     # 14 个行为测试
python3 -m compileall app                 # 构建检查
```

### 命令行

```bash
python3 -m app.api dispatch.db < commands.json
# 数据库路径也可用 DISPATCH_DB 环境变量指定
```

标准输入接受单条命令：

```json
{
  "actor": "A城",
  "action": "submit",
  "request_id": "0242f3c1-…",
  "payload": {
    "route_code": "R-NIGHT-1",
    "op_date": "2026-10-04",
    "city": "A城",
    "seats": 30,
    "ts": "2026-10-04T19:00:00+08:00"
  }
}
```

或一批顺序执行的命令：`{"requests": [ {…}, {…} ]}`。
退出码：全部受理 `0`，存在被拒绝的命令 `1`，输入格式错误 `2`。

### 作为库调用

```python
from app.service import RouteControlService
from app.contracts import Request

svc = RouteControlService("dispatch.db")   # 或 ":memory:"
result = svc.handle(Request(actor="A城", action="submit", request_id="...", payload={...}))
```

## 动作清单

| action | 角色 | 关键字段 |
| --- | --- | --- |
| `configure_route` | 运营配置 | `route_code`、`route_name`、`total_seats` |
| `configure_window` | 运营配置 | `route_code`、`op_date`、`batch_no`、`opens_at`、`closes_at`（窗口锁定后不可改） |
| `grant_reviewer` | 运营配置 | `route_code`、`username` |
| `submit` | 城市 | `route_code`、`op_date`、`city`、`seats`，可选 `batch_no`；`ts` 决定锁定窗口 |
| `approve` | 授权复核人 | `request_key`，可选 `expected_version`、`reason` |
| `reject` | 授权复核人 | `request_key`、`reason`（不占配额） |
| `revoke` | 授权复核人 | `request_key`、`reason`（仅可撤销已批准决定，释放配额） |
| `query` | 任意 | `route_code`、`op_date`，可选 `city` |

申请键格式：`{线路}|{运营日}|{批次}|{城市}`，由提交结果与 query 返回，
复核时引用即可。同一城市同一窗口批次只有一张申请单；待复核期间可以用
**新的 `request_id`** 修订座位数（版本递增、留 `amended` 事件，不占配额）；
复核完成后不可覆盖，只能 `revoke`。

时间统一取命令 `payload.ts`（ISO 8601，可带时区），缺省用服务当前时间，
便于补录场景与测试。

## 数据模型（SQLite）

- `routes` 线路总配额；`windows` 每运营日的批次窗口；`reviewers` 复核授权
- `requests` 申请单（状态 `pending → approved / rejected`，批准后可 `revoked`，含版本号、提交人、复核人、原因）
- `events` 不可变事件流：`submitted / amended / approved / rejected / revoked`
- `commands` 幂等命令表：`request_id` 唯一，保存原始命令与首次结果

容量实时口径：某批次已占用 = Σ approved 座位 − Σ revoked 座位；
驳回从不占配额，批次之间互不相干。

## 目录

`app/` 领域服务（`contracts.py` 约定、`storage.py` 持久化、
`service.py` 业务规则、`api.py` 本地入口），`tests/` 行为测试。
