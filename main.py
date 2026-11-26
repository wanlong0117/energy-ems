"""energy_ems 单进程入口：FastAPI + lifespan 按 data_mode 编排后台任务。

运行：python main.py（内部调用 uvicorn.run，workers=1）。
异常统一转成 {ok:false,error}，认证守卫由各 router 的 require_auth 依赖完成。

v2 模式编排：
  simulate -> Simulator.ensure_baseline() + start()，不启动 Collector，额外启 Aggregator.run_simulate_loop()
  live     -> Collector.start()，不启动 Simulator
  两种模式都启动 Aggregator.run_loop()；Aggregator 为周/月按需聚合视图，不落新表。
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

import auth
import db
from aggregation import aggregator
from api_auth import router as auth_router
from api_config import router as config_router
from api_data import router as data_router
from api_report import router as report_router
from collector import collector
from settings import (
    DATA_MODE_DEFAULT,
    DASHBOARD_DIR,
    HTTP_HOST,
    HTTP_PORT,
    SIMULATE_INTERVAL_SEC,
    SIMULATE_ROLLUP_SEC,
    STATIC_DIR,
)
from simulator import Simulator


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """启动：建库 + 按 data_mode 起后台任务；退出：取消任务并记审计日志。"""
    db.init_db()
    config: dict[str, Any] = auth.load_config()
    data_mode: str = str(config.get("data_mode", DATA_MODE_DEFAULT)).lower()
    if data_mode not in ("simulate", "live"):
        data_mode = DATA_MODE_DEFAULT

    background: list[asyncio.Task[Any]] = []
    simulator: Simulator | None = None

    if data_mode == "simulate":
        # 模拟模式：写真实表，绝不与采集器同时运行，避免主键冲突
        interval_sec: int = max(1, int(config.get("simulate_interval_sec") or SIMULATE_INTERVAL_SEC))
        simulator = Simulator(interval_sec, config)
        try:
            simulator.ensure_baseline()
        except Exception as exc:  # 基线预置失败不能阻断服务，日志可追溯
            db.log_event("error", "api", "BASELINE_FAIL", f"模拟基线预置失败: {exc}")
        background.append(asyncio.create_task(simulator.start(), name="ems-simulator"))
        # 保证看板小时/日有数：每 simulate_rollup_sec 汇总一次
        aggregator.rollup_interval_sec = max(
            1, int(config.get("simulate_rollup_sec") or SIMULATE_ROLLUP_SEC)
        )
        background.append(
            asyncio.create_task(aggregator.run_simulate_loop(), name="ems-simulate-rollup")
        )
        db.log_event("info", "api", "MODE_RUN", f"启动模拟模式，间隔 {interval_sec}s")
    else:
        # 真实模式：模拟器完全不启动
        background.append(asyncio.create_task(collector.start(), name="ems-collector"))
        db.log_event("info", "api", "MODE_RUN", "启动真实采集模式")

    # 两种模式都启动正式结算循环（UTC 00:10 结算昨日 + 补算遗漏日）
    background.append(asyncio.create_task(aggregator.run_loop(), name="ems-aggregator"))

    try:
        yield
    finally:
        for task in background:
            if not task.done():
                task.cancel()
        await asyncio.gather(*background, return_exceptions=True)
        await collector.shutdown()
        if simulator is not None:
            simulator.shutdown()
        db.log_event("info", "api", "SHUTDOWN", "energy_ems 服务停止")


app: FastAPI = FastAPI(title="energy_ems", version="0.1.0", lifespan=lifespan)

# 路由注册：认证路由在前，其余均挂 require_auth
app.include_router(auth_router)
app.include_router(config_router)
app.include_router(data_router)
app.include_router(report_router)


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """HTTPException（含 401 守卫、404）统一成共享响应格式。"""
    return JSONResponse(
        status_code=exc.status_code,
        content={"ok": False, "error": str(exc.detail)},
        headers=exc.headers,
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """请求参数校验失败统一成 {ok:false,error}。"""
    errors: list[str] = [
        f"{'.'.join(str(part) for part in item.get('loc', ())[1:])}: {item.get('msg', '')}"
        for item in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content={"ok": False, "error": "请求参数错误: " + "; ".join(errors)},
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """兜底异常：记录事件日志并隐藏内部细节。"""
    db.log_event("error", "api", "UNHANDLED", f"{request.url.path}: {exc}")
    return JSONResponse(
        status_code=500, content={"ok": False, "error": "服务器内部错误"}
    )


# 单文件看板：优先 resource_dir()/dashboard（frozen=_MEIPASS），回退 v1 static 目录
_dashboard_root: Path = DASHBOARD_DIR if DASHBOARD_DIR.is_dir() else STATIC_DIR
if _dashboard_root.is_dir():
    app.mount("/", StaticFiles(directory=str(_dashboard_root), html=True), name="dashboard")


if __name__ == "__main__":
    uvicorn.run("main:app", host=HTTP_HOST, port=HTTP_PORT, workers=1)
