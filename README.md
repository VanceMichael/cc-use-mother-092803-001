# 跨区域运力调度

面向节假日客流高峰的运力决策服务：各城市按运营日提交线路加班需求，
系统完成容量校验与时间窗口锁定后，交给有权限的复核人批复；查询直接
呈现当前有效决定、形成原因与责任人。状态全部持久化在 SQLite 中，
审批中断或服务重启后从原进度继续。

## 动作约定

所有请求经 `RouteControlService.handle(Request)` 处理，`Request` 包含
`actor`（操作人）、`action`、`payload`、`request_id`（幂等键）。

| action | 权限 | payload | 说明 |
| --- | --- | --- | --- |
| `open_window` | 窗口管理员 | `window_id, route_id, service_date, open_at, close_at, capacity` | 为某线路某运营日开放申报窗口 |
| `submit` | 任意城市方 | `city, route_id, service_date, quantity[, window_id]` | 容量校验并锁定窗口配额，进入 `pending_review` |
| `approve` / `reject` | 复核人 | `demand_id[, reason]` | 批复 / 驳回（驳回释放配额），记录责任人 |
| `query` | 任意 | `demand_id` | 返回当前有效决定、原因、责任人与版本 |

行为保证：

- **幂等**：同一 `request_id` 重试返回首次结果，不重复占用配额；
  同一幂等键携带不同内容时返回 `conflict`。
- **窗口锁定**：仅在窗口开放期内可提交；配额在提交时即锁定。
- **逾期不挤占**：窗口关闭仍待复核的批次自动置为 `expired` 并释放配额，
  不能再被批复，也不占用后续窗口。
- **可续跑**：申请、窗口、决定与幂等回执全部落库，重启后继续处理。

## 运行

```bash
export ROUTE_CONTROL_DB=dispatch.db
export ROUTE_CONTROL_SCHEDULERS=sched-1
export ROUTE_CONTROL_REVIEWERS=lead-1,lead-2
echo '{"actor":"sched-1","action":"open_window","request_id":"w-1","payload":{"window_id":"W1","route_id":"G102","service_date":"2026-10-04","open_at":"2026-10-03T00:00:00","close_at":"2026-10-03T20:00:00","capacity":40}}' | python3 -m app.api
```

## 测试

```bash
python3 -m unittest discover -s tests
```

## 构建检查

```bash
python3 -m compileall app
```
