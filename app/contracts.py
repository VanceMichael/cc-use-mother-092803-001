"""跨区域运力调度的输入输出约定。

服务围绕两类输入工作：

* 城市按运营日提交的线路加班需求（submit）；
* 有权限的调度员对需求进行复核（approve / reject）。

所有写命令都必须携带调用方生成的幂等键 ``request_id``：同一条命令
（以 request_id 标识）无论重试多少次，只生效一次。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


def now_utc() -> datetime:
    """统一的当前时间来源，便于测试注入。"""
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class Request:
    actor: str
    action: str
    payload: dict[str, Any]
    request_id: str
    created_at: datetime = field(default_factory=now_utc)

    @property
    def ts(self) -> datetime:
        """命令时间：优先使用 payload 中的时间（测试/批处理注入）。"""
        raw = self.payload.get("ts")
        if isinstance(raw, datetime):
            return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
        if isinstance(raw, str) and raw.strip():
            text = raw.strip().replace("Z", "+00:00")
            try:
                parsed = datetime.fromisoformat(text)
            except ValueError:
                raise ValueError(f"无法解析的时间: {raw}")
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        return self.created_at if self.created_at.tzinfo else self.created_at.replace(tzinfo=timezone.utc)


@dataclass
class Result:
    """一次命令的执行结果（也是幂等回放的载体）。"""

    accepted: bool
    state: str
    message: str
    data: dict[str, Any] = field(default_factory=dict)
    error_code: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "state": self.state,
            "message": self.message,
            "error_code": self.error_code,
            "data": self.data,
        }


# 业务错误码，调用方可据此决定重试或提示，而不必解析中文文案。
ERR_VALIDATION = "validation_error"
ERR_UNKNOWN_ACTION = "unknown_action"
ERR_ROUTE_UNKNOWN = "route_unknown"
ERR_WINDOW_CLOSED = "window_closed"
ERR_WINDOW_NOT_FOUND = "window_not_found"
ERR_CAPACITY = "capacity_exceeded"
ERR_FORBIDDEN = "forbidden"
ERR_NOT_PENDING = "not_pending"
ERR_VERSION_CONFLICT = "version_conflict"
ERR_NOT_FOUND = "not_found"
ERR_BATCH_MISMATCH = "batch_mismatch"
ERR_ALREADY_REVIEWED = "already_reviewed"


def validate_request(request: Request) -> None:
    if not request.actor or not request.action or not request.request_id:
        raise ValueError("请求缺少身份、动作或幂等键")
    if not isinstance(request.payload, dict):
        raise TypeError("请求数据必须是对象")
