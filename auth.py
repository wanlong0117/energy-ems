"""极简认证：一个固定密码 + 内存 session cookie。单厂单用户场景，不做角色。

在原骨架的 login/logout/current_user 基础上，新增 FastAPI 依赖 require_auth，
供除 /api/login、/api/session 之外的所有 /api/* 路由复用。
未登录统一抛 HTTPException(401)，由 main.py 的异常处理器转成 {ok:false,error}。

v2：CONFIG_PATH 走 settings.app_dir()；load_config() 为旧配置（仅 password）
    补全 data_mode / simulate_interval_sec / simulate_rollup_sec，且不覆盖用户已有键。
v3：安全加固（对应 2026-11-26 审计报告）
    - require_auth 增加服务端强制改密拦截：默认密码状态下除改密/登出/会话查询外一律 403；
    - 密码改为 PBKDF2-SHA256 加盐哈希存储，旧明文配置在首次成功登录时自动升级；
    - 登录失败锁定：同来源连续 5 次失败锁定 60 秒，并记事件日志；
    - 配置文件原子写入 + 0600 权限；配置解析结果做 mtime 缓存，避免每请求读盘。
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import tempfile
import threading
import time
from typing import Any

from fastapi import HTTPException, Request, Response
from fastapi.responses import JSONResponse

from db import log_event
from settings import (
    DATA_MODE_DEFAULT,
    DATA_MODES,
    SESSION_COOKIE_NAME,
    SESSION_COOKIE_SECURE,
    SESSION_TTL_SEC,
    SIMULATE_INTERVAL_SEC,
    TRUST_PROXY_HEADERS,
    SIMULATE_ROLLUP_SEC,
    app_dir,
)

CONFIG_PATH = app_dir() / "config.json"
# 配置文件写入串行化（见 save_config）
_config_write_lock: threading.Lock = threading.Lock()


class ConfigError(RuntimeError):
    """配置文件损坏（JSON 解析失败）。

    解析失败必须「失败关闭」：绝不回退到默认密码 ems2026——否则任何人都能用
    公开默认口令登录，反而比服务不可用更危险。管理员看到事件日志后修正文件即可恢复。
    """

# token -> 过期时间戳
_sessions: dict[str, float] = {}
# 内存状态锁：登录/登出/校验都在 AnyIO 线程池里的同步路由上跑，多个请求会真的并发
# 操作这两个 dict。CPython 的 dict 单次操作是原子的，但「读-改-写」不是：
# 并发 login 时 token 写入与 _prune_sessions 的遍历删除交错，可能让刚发的会话被清掉。
_state_lock: threading.Lock = threading.Lock()
_last_prune: float = 0.0
SESSION_TTL: int = SESSION_TTL_SEC  # 兼容原有命名，7 天
COOKIE_NAME: str = SESSION_COOKIE_NAME
DEFAULT_PASSWORD: str = "ems2026"
PASSWORD_ENV: str = "EMS_PASSWORD"          # 首次生成配置时的初始密码（仅首次生效）
PASSWORD_MIN_LEN: int = 8

# 密码哈希：PBKDF2-SHA256，120k 轮（单次校验约 40ms，兼顾内网工控机性能）
_HASH_PREFIX: str = "pbkdf2_sha256"
_HASH_ITERATIONS: int = 120_000

# 登录失败锁定：同来源连续失败达到阈值后锁定一段时间
LOGIN_FAIL_LIMIT: int = 5
LOGIN_LOCK_SEC: float = 60.0
# 失败计数记录的空闲回收窗口：来源在这么久之内没再试错，就把它那条计数丢掉。
# 只清「已过期且 count<=0」是漏的——扫描器每个 IP 只试 1~4 次（永远到不了 5 次
# 锁定线），这些记录 count>0 且 until==0，既不会被锁定也不会被回收，长期运行
# 就是一条无上限的内存增长路径。窗口取 10 分钟：足以覆盖正常人打错几次密码的
# 间隔，而攻击者被清零后要重来 5 次，速率从「5 次/分钟」降到「5 次/10 分钟」，
# 对撞库毫无意义。
LOGIN_FAIL_IDLE_SEC: float = max(LOGIN_LOCK_SEC, 600.0)
_fail_state: dict[str, dict[str, float]] = {}

# 默认密码状态下仍允许访问的路径（改密、登出、会话查询）
PASSWORD_CHANGE_ALLOWLIST: tuple[str, ...] = (
    "/api/password", "/api/logout", "/api/session",
)

# 配置解析缓存：以 (路径, mtime_ns, size) 为版本戳，写入后立即失效
_config_cache: dict[str, Any] | None = None
_config_stamp: tuple[str, int, int] | None = None


def hash_password(password: str) -> str:
    """生成 `pbkdf2_sha256$轮数$盐$哈希` 形式的可存储口令串。"""
    salt: str = secrets.token_hex(16)
    digest: bytes = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), _HASH_ITERATIONS
    )
    return f"{_HASH_PREFIX}${_HASH_ITERATIONS}${salt}${digest.hex()}"


def is_hashed(stored: str) -> bool:
    """判断配置里存的是否为哈希串（旧版本存明文）。"""
    return str(stored).startswith(_HASH_PREFIX + "$")


def verify_password(password: str, stored: str) -> bool:
    """校验口令：哈希串走 PBKDF2，旧明文走常量时间比较。"""
    stored = str(stored)
    if not is_hashed(stored):
        return secrets.compare_digest(str(password), stored)
    try:
        _prefix, iterations, salt, expected = stored.split("$")
        digest: bytes = hashlib.pbkdf2_hmac(
            "sha256", str(password).encode("utf-8"), bytes.fromhex(salt), int(iterations)
        )
    except (ValueError, TypeError):
        return False
    return secrets.compare_digest(digest.hex(), expected)


def _config_version() -> tuple[str, int, int]:
    """配置文件版本戳；文件不存在时返回哨兵值。"""
    try:
        stat = CONFIG_PATH.stat()
    except OSError:
        return (str(CONFIG_PATH), -1, -1)
    return (str(CONFIG_PATH), stat.st_mtime_ns, stat.st_size)


CONFIG_DEFAULTS: dict[str, Any] = {
    "password": DEFAULT_PASSWORD,
    "password_changed": False,
    "data_mode": DATA_MODE_DEFAULT,
    "simulate_interval_sec": SIMULATE_INTERVAL_SEC,
    "simulate_rollup_sec": SIMULATE_ROLLUP_SEC,
}


def _default_config() -> dict[str, Any]:
    """返回一份全新的默认配置副本；EMS_PASSWORD 仅在首次生成配置时生效。

    使用环境变量指定初始密码时直接落哈希并标记「已改密」——该密码不是公开默认值，
    不应再强制操作员修改。
    """
    config: dict[str, Any] = dict(CONFIG_DEFAULTS)
    env_password: str = os.environ.get(PASSWORD_ENV, "").strip()
    if env_password:
        config["password"] = hash_password(env_password)
        config["password_changed"] = True
    return config


def load_config() -> dict[str, Any]:
    """读取配置并用默认值补全缺失键；不覆盖用户已有键（结果按文件版本戳缓存）。

    旧 config.json 仅含 password 时会自动补 data_mode/simulate_*/password_changed 并回写，
    这样 /api/mode、lifespan 与模拟器都能拿到一致的运行时配置。
    """
    global _config_cache, _config_stamp
    stamp: tuple[str, int, int] = _config_version()
    if _config_cache is not None and _config_stamp == stamp:
        return dict(_config_cache)

    config: dict[str, Any] = _default_config()
    if CONFIG_PATH.exists():
        try:
            loaded: Any = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log_event("error", "api", "CONFIG_PARSE_FAIL", f"配置文件解析失败，拒绝启动登录: {exc}")
            raise ConfigError(f"配置文件 {CONFIG_PATH} 解析失败：{exc}") from exc
        if not isinstance(loaded, dict):
            log_event("error", "api", "CONFIG_PARSE_FAIL", "配置文件根节点不是对象")
            raise ConfigError(f"配置文件 {CONFIG_PATH} 根节点必须是 JSON 对象")
        # 用户已有键优先；仅对缺失键填默认值
        for key, value in loaded.items():
            if value is not None:
                config[key] = value
    config["data_mode"] = str(config.get("data_mode", DATA_MODE_DEFAULT)).lower()
    if config["data_mode"] not in DATA_MODES:
        config["data_mode"] = DATA_MODE_DEFAULT
    for key in ("simulate_interval_sec", "simulate_rollup_sec"):
        try:
            config[key] = max(1, int(config[key]))
        except (TypeError, ValueError):
            config[key] = int(CONFIG_DEFAULTS[key])

    # 仅在文件缺失或缺键时回写，保持用户原有键不变
    need_write: bool = not CONFIG_PATH.exists()
    if not need_write:
        try:
            existing: Any = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            need_write = not isinstance(existing, dict) or any(
                key not in existing for key in CONFIG_DEFAULTS
            )
        except (OSError, ValueError):
            need_write = True
    if need_write:
        save_config(config)
        stamp = _config_version()

    _config_cache = dict(config)
    _config_stamp = stamp
    return config


def save_config(config: dict[str, Any]) -> None:
    """整体写回配置（调用方负责合并已有键）；原子替换 + 收紧文件权限。

    临时文件名必须唯一：固定叫 config.json.tmp 时，两个并发写（改密 + 切模式）
    会往同一个文件里写，后一个 os.replace 还可能在 Windows 上直接
    FileNotFoundError（文件已被前一个挪走）；写入交错则可能落下半截 JSON，
    而 load_config 对解析失败是「失败关闭」，整个服务从此登不进去。
    """
    global _config_cache, _config_stamp
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload: str = json.dumps(config, indent=2, ensure_ascii=False)
    with _config_write_lock:
        handle: int = -1
        temp_path: str = ""
        try:
            handle, temp_path = tempfile.mkstemp(
                prefix=CONFIG_PATH.name + ".", suffix=".tmp", dir=str(CONFIG_PATH.parent)
            )
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                handle = -1
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.chmod(temp_path, 0o600)  # 配置含口令哈希，仅属主可读
            except OSError:
                pass
            os.replace(temp_path, CONFIG_PATH)
        except BaseException:
            if handle >= 0:
                os.close(handle)
            if temp_path:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass
            raise
        _config_cache = None
        _config_stamp = None


def set_data_mode(mode: str) -> str:
    """切换 data_mode：写配置并返回规范化后的模式值（重启后生效）。"""
    normalized: str = str(mode).lower()
    if normalized not in DATA_MODES:
        raise ValueError(f"data_mode 仅支持 {', '.join(DATA_MODES)}")
    config: dict[str, Any] = load_config()
    config["data_mode"] = normalized
    save_config(config)
    log_event("info", "user", "MODE_SWITCH", f"data_mode 切换为 {normalized}（待重启生效）")
    return normalized


def load_password() -> str:
    """读取 config.json 中的口令存储值（哈希或旧明文）；首次运行生成默认配置。"""
    return str(load_config().get("password", DEFAULT_PASSWORD))


def verify_login_password(password: str) -> bool:
    """校验登录口令（对哈希与旧明文都成立）。"""
    return verify_password(password, load_password())


def must_change_password() -> bool:
    """是否仍在使用公开默认密码：是则前端引导 + 服务端拦截业务接口。"""
    config: dict[str, Any] = load_config()
    if "password_changed" in config:
        return not bool(config.get("password_changed"))
    # 旧配置没有标记位：明文等于默认值即视为未改；哈希只可能由改密写入
    stored: str = str(config.get("password", DEFAULT_PASSWORD))
    if is_hashed(stored):
        return False
    return secrets.compare_digest(stored, DEFAULT_PASSWORD)


def change_password(old_password: str, new_password: str) -> JSONResponse:
    """校验原密码并设置新密码；成功后清空全部会话，强制重新登录。"""
    if not verify_login_password(old_password):
        log_event("warn", "user", "PASSWORD_CHANGE_FAIL", "改密失败：原密码错误")
        return JSONResponse({"ok": False, "error": "原密码错误"}, status_code=401)
    candidate: str = str(new_password or "")
    if len(candidate) < PASSWORD_MIN_LEN:
        return JSONResponse(
            {"ok": False, "error": f"新密码至少 {PASSWORD_MIN_LEN} 位"}, status_code=400
        )
    if candidate == DEFAULT_PASSWORD:
        return JSONResponse({"ok": False, "error": "新密码不能与默认密码相同"}, status_code=400)
    config: dict[str, Any] = load_config()
    config["password"] = hash_password(candidate)
    config["password_changed"] = True
    save_config(config)
    with _state_lock:
        _sessions.clear()
    log_event("warn", "user", "PASSWORD_CHANGED", "管理员密码已修改，所有会话失效")
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE_NAME)
    return resp


def client_from_request(request: Request) -> str:
    """请求来源标识：默认取 socket 对端地址。

    置于反向代理之后时对端恒为 127.0.0.1，所有人共用一个锁定计数——一个人输错
    5 次就把全厂锁在门外。显式开启 EMS_TRUST_PROXY_HEADERS 后改用
    X-Forwarded-For 的第一跳。该头可被伪造，所以只在后端仅代理可达时开启。
    """
    if TRUST_PROXY_HEADERS:
        forwarded: str = request.headers.get("x-forwarded-for", "")
        first: str = forwarded.split(",")[0].strip()
        if first:
            return first
    return request.client.host if request.client else ""


def _client_key(client: str) -> str:
    return str(client or "unknown")


def is_login_locked(client: str = "") -> bool:
    """该来源是否处于登录失败锁定期。"""
    with _state_lock:
        state: dict[str, float] | None = _fail_state.get(_client_key(client))
        if not state:
            return False
        return float(state.get("until", 0.0)) > time.time()


def _record_login_failure(client: str = "") -> None:
    """记录一次失败；达到阈值则锁定一段时间并清空计数。

    每条记录都盖一个 last_at：prune 靠它判断「这条来源还活着没有」。没有它就只能
    看 count，而「试了 2 次就换 IP」的记录 count 恒为正，永远不会被回收。
    """
    key: str = _client_key(client)
    locked: bool = False
    with _state_lock:
        state: dict[str, float] = _fail_state.setdefault(key, {"count": 0.0, "until": 0.0})
        state["count"] = float(state.get("count", 0.0)) + 1.0
        state["last_at"] = time.time()
        if state["count"] >= LOGIN_FAIL_LIMIT:
            state["until"] = time.time() + LOGIN_LOCK_SEC
            state["count"] = 0.0
            locked = True
    if locked:  # 日志在锁外写：db 有自己的一把锁，不要嵌套持有
        log_event(
            "warn", "api", "LOGIN_LOCKED",
            f"登录失败次数达到 {LOGIN_FAIL_LIMIT} 次，来源 {key} 锁定 {int(LOGIN_LOCK_SEC)} 秒",
        )


def _clear_login_failures(client: str = "") -> None:
    with _state_lock:
        _fail_state.pop(_client_key(client), None)


def _upgrade_plaintext_password(password: str) -> None:
    """旧明文口令在成功登录后升级为哈希。

    password_changed 是「操作员声明此密码已非公开默认值」的唯一事实源：
    标记已存在就原样保留，缺失时才按「是否等于默认密码」推断。
    """
    config: dict[str, Any] = load_config()
    stored: str = str(config.get("password", DEFAULT_PASSWORD))
    if is_hashed(stored):
        return
    still_default: bool = secrets.compare_digest(stored, DEFAULT_PASSWORD)
    config["password"] = hash_password(password)
    if "password_changed" not in config:
        config["password_changed"] = not still_default
    save_config(config)


def _prune_sessions(now: float) -> None:
    """定期清掉过期会话与失效的失败计数，避免内存长期堆积。

    _fail_state 原来只增不减：扫描器换着源 IP 撞库时，每个 IP 留一条记录永不释放。
    但回收条件不能只看 count：内网扫描器或写错的脚本每个 IP 只试 1~4 次，根本到不了
    5 次锁定线，这些记录 count>0 且 until==0，一条都清不掉——200 个 IP 的段扫一遍
    就是 200 条永久驻留，扫十遍就是两千条。
    改成按「最近一次活动」回收：仍在锁定期的一律留着；锁定已过期、且距最近一次失败
    超过 LOGIN_FAIL_IDLE_SEC 的，不论 count 是否为 0 都丢掉。
    """
    global _last_prune
    with _state_lock:
        if now - _last_prune < 300:
            return
        _last_prune = now
        for token, expire_at in list(_sessions.items()):
            if expire_at < now:
                _sessions.pop(token, None)
        for key, state in list(_fail_state.items()):
            until: float = float(state.get("until", 0.0))
            if until >= now:
                continue  # 仍在锁定期，计数与到期时刻都还要用
            # 取 until 与 last_at 的较晚者：锁定期间不再有失败写入，只看 last_at
            # 会把「刚解封、正被观察」的记录当成空闲已久直接回收。
            last_active: float = max(until, float(state.get("last_at", 0.0)))
            if now - last_active >= LOGIN_FAIL_IDLE_SEC:
                _fail_state.pop(key, None)


def login(password: str, response: Response, client: str = "") -> JSONResponse:
    """校验固定密码，成功则签发 token 并写入 httponly cookie。"""
    if is_login_locked(client):
        log_event("warn", "api", "LOGIN_LOCKED_REJECT", f"锁定期间拒绝登录，来源 {_client_key(client)}")
        return JSONResponse(
            {"ok": False, "error": f"登录失败次数过多，请 {int(LOGIN_LOCK_SEC)} 秒后再试"},
            status_code=429,
        )
    try:
        password_ok: bool = verify_login_password(password)
    except ConfigError:
        # 配置损坏时拒绝登录，绝不用公开默认密码兜底
        return JSONResponse(
            {"ok": False, "error": "服务器配置文件损坏，请联系管理员修复 config.json"},
            status_code=503,
        )
    if not password_ok:
        _record_login_failure(client)
        log_event("warn", "api", "LOGIN_FAIL", "登录密码错误")
        return JSONResponse({"ok": False, "error": "密码错误"}, status_code=401)
    _clear_login_failures(client)
    try:
        _upgrade_plaintext_password(password)
    except ConfigError:
        return JSONResponse(
            {"ok": False, "error": "服务器配置文件损坏，请联系管理员修复 config.json"},
            status_code=503,
        )
    token = secrets.token_hex(32)
    with _state_lock:
        _sessions[token] = time.time() + SESSION_TTL
    resp = JSONResponse({"ok": True, "must_change_password": must_change_password()})
    resp.set_cookie(
        COOKIE_NAME, token, max_age=SESSION_TTL,
        httponly=True, samesite="lax", secure=SESSION_COOKIE_SECURE,
    )
    log_event("info", "api", "LOGIN", "登录成功")
    return resp


def logout(request: Request) -> JSONResponse:
    """清除内存会话与浏览器 cookie。"""
    token: str | None = request.cookies.get(COOKIE_NAME)
    if token:
        with _state_lock:
            _sessions.pop(token, None)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE_NAME)
    return resp


def current_user(request: Request) -> bool:
    """判断请求是否携带有效且未过期的会话 token。"""
    token: str | None = request.cookies.get(COOKIE_NAME)
    if not token:
        return False
    now: float = time.time()
    _prune_sessions(now)
    with _state_lock:
        exp: float | None = _sessions.get(token)
        if not exp or exp < now:
            _sessions.pop(token, None)
            return False
    return True


def require_auth(request: Request) -> bool:
    """FastAPI 依赖守卫：未登录 401；仍用默认密码时除改密相关路径外一律 403。

    「强制改密」必须由服务端把关——仅靠看板引导，用脚本直接调接口就能绕过。
    """
    if not current_user(request):
        raise HTTPException(status_code=401, detail="未登录或会话已过期")
    if request.url.path not in PASSWORD_CHANGE_ALLOWLIST and must_change_password():
        raise HTTPException(status_code=403, detail="请先修改默认密码")
    return True
