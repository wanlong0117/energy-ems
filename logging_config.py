"""结构化日志：应用日志输出单行 JSON，便于工控机日志采集与 grep。

不使用第三方依赖：只挂一个 StreamHandler 到 `ems` logger，业务模块通过
get_logger("ems.xxx") 取用；uvicorn 自身的访问日志格式保持不变。
必须显式使用 `%` 风格参数或预先拼好 message，不要传 dict，否则 json 序列化失败。
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone

_RESERVED: set[str] = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__)
_EXTRA_KEYS: tuple[str, ...] = ("event", "code", "device_id", "point_id", "meter_id", "day_start")
_CONFIGURED: bool = False


class JsonFormatter(logging.Formatter):
    """把 LogRecord 序列化成单行 JSON；附带业务字段与异常栈。"""

    def format(self, record: logging.LogRecord) -> str:
        moment: str = datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat()
        payload: dict[str, object] = {
            "ts": moment,
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in _EXTRA_KEYS:
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        for key, value in record.__dict__.items():
            if key not in _RESERVED and key not in _EXTRA_KEYS:
                payload.setdefault(key, value if isinstance(value, (str, int, float, bool, type(None))) else str(value))
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(level: int = logging.INFO, stream: object | None = None) -> None:
    """幂等配置 `ems` logger：单行 JSON 输出到 stdout。"""
    global _CONFIGURED
    logger = logging.getLogger("ems")
    if _CONFIGURED:
        logger.setLevel(level)
        return
    handler = logging.StreamHandler(stream or sys.stdout)  # type: ignore[arg-type]
    handler.setFormatter(JsonFormatter())
    logger.handlers = [handler]
    logger.setLevel(level)
    logger.propagate = False
    _CONFIGURED = True


def get_logger(name: str = "ems") -> logging.Logger:
    """取业务 logger（自动确保已配置）。"""
    if not _CONFIGURED:
        setup_logging()
    return logging.getLogger(name if name == "ems" or name.startswith("ems.") else f"ems.{name}")


__all__ = ["JsonFormatter", "get_logger", "setup_logging"]
