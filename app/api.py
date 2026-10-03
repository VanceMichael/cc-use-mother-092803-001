"""跨区域运力调度的本地命令行入口。

用法::

    python -m app.api [sqlite文件路径] < 请求.json

数据库路径也可用环境变量 ``DISPATCH_DB`` 指定，缺省为当前目录的
``dispatch.db``。标准输入接受两种 JSON：

1. 单条命令::

       {
         "actor": "city-a",
         "action": "submit",
         "request_id": "uuid-1",
         "payload": { ... , "ts": "2026-10-04T10:00:00+08:00"}
       }

2. 一批顺序执行的命令（适合现场脚本/补录）::

       {"requests": [ {..}, {..} ]}

标准输出对应打印单个结果对象或结果数组。全部受理退出码 0；
存在被拒绝的命令退出码 1；输入本身有误退出码 2。

支持的动作：configure_route / configure_window / grant_reviewer /
submit / approve / reject / revoke / query。
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any

from .contracts import Request, now_utc
from .service import RouteControlService


def _build_request(item: dict[str, Any]) -> Request:
    for field in ("actor", "action", "request_id"):
        if not str(item.get(field, "")).strip():
            raise ValueError(f"命令缺少字段: {field}")
    payload = item.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("payload 必须是对象")
    return Request(
        actor=str(item["actor"]).strip(),
        action=str(item["action"]).strip(),
        payload=payload,
        request_id=str(item["request_id"]).strip(),
        created_at=now_utc(),
    )


def run(db_path: str, raw: str) -> tuple[list[dict[str, Any]], bool, bool]:
    obj = json.loads(raw)
    if isinstance(obj, dict) and isinstance(obj.get("requests"), list):
        items, batch = obj["requests"], True
    else:
        items, batch = [obj], False

    service = RouteControlService(db_path)
    try:
        results = [service.handle(_build_request(item)).to_dict() for item in items]
    finally:
        service.close()
    return results, all(r["accepted"] for r in results), batch


def main(argv: list[str]) -> int:
    db_path = argv[1] if len(argv) > 1 else os.environ.get("DISPATCH_DB", "dispatch.db")
    raw = sys.stdin.read().strip()
    if not raw:
        sys.stderr.write(__doc__)
        return 2
    try:
        results, all_accepted, is_batch = run(db_path, raw)
    except (ValueError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"输入错误: {exc}\n")
        return 2

    output: Any = results if is_batch else results[0]
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if all_accepted else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
