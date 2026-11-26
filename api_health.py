"""健康检查端点：/healthz（匿名、最小信息）与 /api/health（登录后详细）。

设计取舍：
- /healthz 只返回「服务活着 / 数据库可读 / 数据是否新鲜」，不暴露配置细节，
  供工控机守护脚本或反向代理探活；数据库不可读返回 503。
- /api/health 返回模式、任务数、最近读数时间与迁移版本，供运维定位。
健康检查不查业务大表，只走索引/聚合最大值，避免探活本身拖慢系统。
"""
from __future__ import annotations

import sqlite3
import time
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

import db
from auth import ConfigError, require_auth
from settings import APP_VERSION

STARTED_AT: int = int(time.time())

# 最近读数超过该时长即标记 stale（live 模式下通常意味着采集器掉线）
STALE_AFTER_SEC: int = 15 * 60

router = APIRouter(tags=["health"])


def _snapshot(detailed: bool) -> tuple[dict[str, Any], bool]:
    """组装健康快照；第二个返回值为「是否健康」（决定 HTTP 状态码）。"""
    payload: dict[str, Any] = {
        "status": "ok",
        "version": APP_VERSION,
        "uptime_sec": max(0, int(time.time()) - STARTED_AT),
    }
    healthy: bool = True

    try:
        db.query_one("SELECT 1")
        payload["db"] = {"ok": True}
        latest = db.query_one("SELECT MAX(ts) AS ts FROM reading_latest")
        last_ts: int | None = int(latest["ts"]) if latest and latest["ts"] is not None else None
        payload["db"]["last_reading_ts"] = last_ts
        if last_ts is not None:
            payload["db"]["stale_sec"] = max(0, int(time.time()) - last_ts)
            payload["db"]["stale"] = payload["db"]["stale_sec"] > STALE_AFTER_SEC
    except sqlite3.Error as exc:
        healthy = False
        payload["status"] = "error"
        payload["db"] = {"ok": False, "error": type(exc).__name__}

    if not detailed:
        if payload.get("db", {}).get("stale"):
            payload["status"] = "degraded"
        return payload, healthy

    try:
        import auth

        config: dict[str, Any] = auth.load_config()
        payload["data_mode"] = str(config.get("data_mode", "simulate"))
    except ConfigError as exc:
        healthy = False
        payload["status"] = "error"
        payload["config_error"] = str(exc)
    except Exception as exc:  # 健康检查自身不得抛错，否则探活失去意义
        payload["data_mode"] = "unknown"
        payload["config_error"] = type(exc).__name__

    try:
        version_row = db.query_one("SELECT MAX(version) AS v FROM schema_version")
        payload["schema_version"] = int(version_row["v"] or 0) if version_row else 0
    except sqlite3.Error:
        payload["schema_version"] = None

    try:
        from collector import collector

        running = sum(1 for task in collector.tasks.values() if not task.done())
        payload["collector"] = {"devices": len(collector.tasks), "running": running}
        if collector.tasks and running < len(collector.tasks):
            payload["status"] = "degraded"
    except Exception:  # 采集器未启动（simulate 模式）时不算异常
        payload["collector"] = {"devices": 0, "running": 0}

    return payload, healthy


@router.get("/healthz", include_in_schema=False)
def healthz() -> JSONResponse:
    """匿名探活：最小信息，数据库不可用返回 503。"""
    payload, healthy = _snapshot(detailed=False)
    return JSONResponse(payload, status_code=200 if healthy else 503)


@router.get("/api/health")
def health(_: bool = Depends(require_auth)) -> JSONResponse:
    """登录后详细健康信息：模式、任务、迁移版本、配置是否损坏。"""
    payload, healthy = _snapshot(detailed=True)
    return JSONResponse(payload, status_code=200 if healthy else 503)


__all__ = ["router"]
