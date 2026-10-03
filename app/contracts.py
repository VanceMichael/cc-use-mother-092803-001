"""跨区域运力调度 的输入输出约定。"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

def utcnow() -> datetime:
    """无时区的 UTC 当前时间，与库中存储的时间格式保持一致。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)

@dataclass(frozen=True)
class Request:
    actor: str
    action: str
    payload: dict[str, Any]
    request_id: str
    created_at: datetime = field(default_factory=utcnow)

@dataclass
class Result:
    accepted: bool
    state: str
    message: str
    data: dict[str, Any] = field(default_factory=dict)

def validate_request(request: Request) -> None:
    if not request.actor or not request.action or not request.request_id:
        raise ValueError("请求缺少身份、动作或幂等键")
    if not isinstance(request.payload, dict):
        raise TypeError("请求数据必须是对象")
