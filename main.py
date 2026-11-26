"""energy_ems 单进程入口：FastAPI + lifespan 按 data_mode 编排后台任务。

运行：python main.py（内部调用 uvicorn.run，workers=1）。
异常统一转成 {ok:false,error}，认证守卫由各 router 的 require_auth 依赖完成。

v2 模式编排：
  simulate -> Simulator.ensure_baseline() + start()，不启动 Collector，额外启 Aggregator.run_simulate_loop()
  live     -> Collector.start() + Aggregator.run_hourly_loop()（小时汇总的唯一写入者），不启动 Simulator
  两种模式都启动 Aggregator.run_loop()（日结算 + 补算 + 超期读数清理）。
  FastAPI 自带文档路由已关闭，/docs 与 /openapi.json 改为带 require_auth 的入口。
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import auth
import db
from aggregation import aggregator
from api_auth import router as auth_router
from api_config import router as config_router
from api_data import router as data_router
from api_health import router as health_router
from api_report import router as report_router
from auth import ConfigError, require_auth
from collector import collector
from logging_config import get_logger, setup_logging
from settings import (
    APP_VERSION,
    DATA_MODE_DEFAULT,
    DASHBOARD_DIR,
    HTTP_HOST,
    HTTP_PORT,
    SIMULATE_INTERVAL_SEC,
    SIMULATE_ROLLUP_SEC,
    STATIC_DIR,
)
from simulator import Simulator

setup_logging()
logger = get_logger("main")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """启动：建库 + 按 data_mode 起后台任务；退出：取消任务并记审计日志。"""
    db.init_db()
    config_broken: bool = False
    try:
        config: dict[str, Any] = auth.load_config()
    except ConfigError as exc:
        # 配置损坏：服务仍起来以便看健康检查与事件日志，但登录会被拒绝（失败关闭），
        # 也不启动任何采集/模拟任务，避免在无人可管的状态下继续写错数据。
        config_broken = True
        logger.error("config.json 损坏，业务任务与登录均已停用")
        db.log_event("error", "api", "CONFIG_BROKEN", f"启动时配置不可用: {exc}")
        config = {}
    data_mode: str = str(config.get("data_mode", DATA_MODE_DEFAULT)).lower()
    if data_mode not in ("simulate", "live"):
        data_mode = DATA_MODE_DEFAULT

    background: list[asyncio.Task[Any]] = []
    simulator: Simulator | None = None

    if config_broken:
        db.log_event("warn", "api", "MODE_IDLE", "配置损坏，未启动采集/模拟/结算任务")
    elif data_mode == "simulate":
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
        # 真实模式：模拟器完全不启动，但必须起小时汇总，否则 hourly 无写入者
        #
        # 同时停用演示数据（P0）：先 simulate 演示、再切 live 接真表是本项目文档
        # 推荐的路径，但 100 块「模拟-*」表若仍处于启用+计入，日结算覆盖率分母
        # 会变成 101，真实表永远达不到 90% 门槛，日报全部停在「未结算」。
        disabled: dict[str, int] = db.disable_demo_data()
        if any(disabled.values()):
            logger.warning("已停用演示数据：%s", disabled)
        background.append(asyncio.create_task(collector.start(), name="ems-collector"))
        background.append(
            asyncio.create_task(aggregator.run_hourly_loop(), name="ems-hourly-rollup")
        )
        db.log_event("info", "api", "MODE_RUN", "启动真实采集模式")

    if not config_broken:
        # 两种模式都启动正式结算循环（日切点 +10 分钟结算昨日 + 补算遗漏日）
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


app: FastAPI = FastAPI(
    title="energy_ems",
    version=APP_VERSION,
    lifespan=lifespan,
    # 关闭 FastAPI 自带的免鉴权文档路由，改用下方带 require_auth 的入口
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

# 路由注册：认证与探活在前，其余均挂 require_auth
app.include_router(auth_router)
app.include_router(health_router)
app.include_router(config_router)
app.include_router(data_router)
app.include_router(report_router)


@app.get("/openapi.json", include_in_schema=False, dependencies=[Depends(require_auth)])
async def openapi_spec() -> JSONResponse:
    """带鉴权的 OpenAPI 描述：未登录返回 401，登录后才对外暴露接口清单。"""
    return JSONResponse(app.openapi())


@app.get("/docs", include_in_schema=False, dependencies=[Depends(require_auth)])
async def swagger_docs() -> HTMLResponse:
    """带鉴权的接口文档页（Swagger UI 静态资源取自 CDN，纯内网可能渲染不出）。"""
    return get_swagger_ui_html(openapi_url="/openapi.json", title="energy_ems API")


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """HTTPException（含 401 守卫、404）统一成共享响应格式。"""
    return JSONResponse(
        status_code=exc.status_code,
        content={"ok": False, "error": str(exc.detail)},
        headers=exc.headers,
    )


@app.exception_handler(ConfigError)
async def config_error_handler(request: Request, exc: ConfigError) -> JSONResponse:
    """配置损坏：503 + 明确话术，绝不悄悄回退到默认密码。"""
    logger.error("配置文件不可用，请求被拒绝")
    return JSONResponse(
        status_code=503,
        content={"ok": False, "error": "服务器配置文件损坏，请联系管理员修复 config.json"},
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
