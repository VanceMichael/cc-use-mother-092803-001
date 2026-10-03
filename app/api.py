"""跨区域运力调度 的轻量本地调用入口。

从标准输入读取一条 JSON 请求，输出一条 JSON 结果。
环境变量：
  ROUTE_CONTROL_DB         SQLite 文件路径（默认 route_control.db）
  ROUTE_CONTROL_REVIEWERS  逗号分隔的复核人名单
  ROUTE_CONTROL_SCHEDULERS 逗号分隔的窗口管理员名单
"""
import json
import os
import sys
from .contracts import Request, utcnow
from .service import RouteControlService


def _names(env: str) -> list[str]:
    return [name.strip() for name in os.environ.get(env, "").split(",") if name.strip()]


def main() -> int:
    raw = sys.stdin.read().strip()
    if not raw:
        return 2
    service = RouteControlService(
        db_path=os.environ.get("ROUTE_CONTROL_DB", "route_control.db"),
        reviewers=_names("ROUTE_CONTROL_REVIEWERS"),
        schedulers=_names("ROUTE_CONTROL_SCHEDULERS"),
    )
    try:
        item = json.loads(raw)
        request = Request(
            str(item.get("actor", "")),
            str(item.get("action", "")),
            dict(item.get("payload", {})),
            str(item.get("request_id", "")),
            utcnow(),
        )
        result = service.handle(request)
    finally:
        service.close()
    print(json.dumps(
        {"accepted": result.accepted, "state": result.state,
         "message": result.message, "data": result.data},
        ensure_ascii=False,
    ))
    return 0 if result.accepted else 1


if __name__ == "__main__":
    raise SystemExit(main())
