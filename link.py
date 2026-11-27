"""设备链路状态：把「多久没采到数据」翻译成现场看得懂的在线 / 断线。

为什么单独一个模块而不是塞进 collector：判定只看 `device.last_ok_at` 与
`poll_interval`，既被 API（看板展示）用，也被聚合侧（结算时区分「断电」与
「停机」）用。放在 collector 里会让 aggregation 反向依赖采集器，测试也得起协程。

判定口径：
  · 已停用（is_enabled=0）      -> disabled  （不采集，不是故障）
  · 演示设备（is_demo=1）        -> demo      （数据是模拟器造的，无链路可言）
  · manual 设备                  -> manual    （人工录入，没有链路概念）
  · 从未成功过（last_ok_at 为 NULL）-> never （配置还没跑通，最常见是 IP/端口错）
  · 超过 stale 窗口没成功        -> offline   （断线：断电、网线掉了、对端重启）
  · 其余                          -> online

stale 窗口取 `poll_interval × 3`，并夹在 [120s, 3600s] 之间：
  · 下限 120s：轮询 10s 的设备也不该因为一次抖动就报断线；
  · 上限 3600s：轮询间隔配成 4 小时的设备，用 ×3 会得到 12 小时才报断线，
    现场「断电一小时」根本看不出来，所以封顶一小时。
"""
from __future__ import annotations

import time
from typing import Any

LINK_STALE_FACTOR: int = 3
LINK_STALE_MIN_SEC: int = 120
LINK_STALE_MAX_SEC: int = 3600

STATE_ONLINE: str = "online"
STATE_OFFLINE: str = "offline"
STATE_NEVER: str = "never"
STATE_DISABLED: str = "disabled"
STATE_MANUAL: str = "manual"
STATE_DEMO: str = "demo"

# 中文标签作为兜底文案随 API 一起下发：看板切英文时用 i18n 覆盖，
# 但告警/日志/第三方直接读 API 的场景需要一个不用查表的可读值。
LABELS: dict[str, str] = {
    STATE_ONLINE: "在线",
    STATE_OFFLINE: "断线",
    STATE_NEVER: "从未连通",
    STATE_DISABLED: "已停用",
    STATE_MANUAL: "人工录入",
    STATE_DEMO: "演示数据",
}


def stale_after_sec(poll_interval: Any) -> int:
    """多久没有成功采集即判断线（秒）。非法/空值按 60s 轮询兜底。"""
    try:
        interval: int = int(poll_interval or 60)
    except (TypeError, ValueError):
        interval = 60
    if interval <= 0:
        interval = 60
    return min(max(interval * LINK_STALE_FACTOR, LINK_STALE_MIN_SEC), LINK_STALE_MAX_SEC)


def effective_ok_at(device: dict[str, Any]) -> int | None:
    """设备「最后一次有生命迹象」的时刻：采集成功时间与最新读数时间取较晚者。

    两个信号都要看：
      · `last_ok_at` 只在 Modbus 采集成功时刷新，模拟模式根本不写它；
      · `last_data_at`（reading_latest 的最大 ts）反映「真的有待数据进来」，
        模拟模式下只有它可信。
    只用 `last_ok_at` 会让演示实例整片显示「从未连通」，把正常界面变成一片红。
    """
    moments: list[int] = [
        int(value) for value in (device.get("last_ok_at"), device.get("last_data_at"))
        if value is not None
    ]
    return max(moments) if moments else None


def classify(device: dict[str, Any], now: int | None = None) -> dict[str, Any]:
    """给一台设备算链路状态；device 需含 id/name/is_enabled/protocol/poll_interval，
    以及 last_ok_at 与（可选的）last_data_at。

    返回的 stale_sec 在「从未成功」时为 None——0 会被看板渲染成「刚刚采到」，
    正好把最需要被看见的配置错误显示成一切正常。
    """
    moment: int = int(now if now is not None else time.time())
    last_ok_at: int | None = effective_ok_at(device)
    protocol: str = str(device.get("protocol") or "").strip().lower()
    window: int = stale_after_sec(device.get("poll_interval"))

    if not int(device.get("is_enabled") or 0):
        state: str = STATE_DISABLED
    elif int(device.get("is_demo") or 0):
        # 模拟器预置的网关：数据是造出来的，报「在线/断线」都没有意义。
        # 它们的 protocol 恰好也是 manual（模拟器直接写库、不走 Modbus），
        # 若不加这一档，演示实例会整片显示成「人工录入」，比不显示还容易误导。
        state = STATE_DEMO
    elif protocol == "manual":
        state = STATE_MANUAL
    elif last_ok_at is None:
        state = STATE_NEVER
    elif moment - last_ok_at > window:
        state = STATE_OFFLINE
    else:
        state = STATE_ONLINE

    return {
        "device_id": int(device.get("id") or 0),
        "name": str(device.get("name") or ""),
        "protocol": protocol,
        "state": state,
        "label": LABELS[state],
        "stale_sec": None if last_ok_at is None else max(0, moment - last_ok_at),
        "stale_after_sec": window,
        "last_ok_at": last_ok_at,
        "last_error": device.get("last_error"),
    }
