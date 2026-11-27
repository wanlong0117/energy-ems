"""历史重算：按当前口径从 reading 重建 hourly，再重结算 daily。

为什么需要：v0.1.3 修掉了「整点读数丢一个采样间隔」——旧口径下每小时的收口
取 `ts < hour_end`，正好落在整点的读数只被下一小时当基准，导致每小时少计
一个采样间隔（300s 轮询约 8.3%）。口径改了，历史 hourly / daily 必须重算，
否则新旧数字混在一张表里，月报永远对不上。

用法：
    python scripts/recompute.py --db energy.db --dry-run          # 先看影响面
    python scripts/recompute.py --db energy.db                    # 保留原定稿标记
    python scripts/recompute.py --db energy.db --strict-coverage  # 按真实覆盖率重新判定
    python scripts/recompute.py --db energy.db --from 2026-11-01 --to 2026-11-30

安全约定：
  · 只重建「reading 里仍有数据的小时」，超期已被清理的历史汇总值不会被抹掉；
  · 每个小时先删旧行再重算，单小时失败不影响其它小时；
  · 默认保留历史 is_final（对账表不会整片翻成未结算），加 --strict-coverage 才按现状重判；
  · 不做 VACUUM、不删表，出问题可以用备份库直接覆盖回来。
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import db  # noqa: E402
from aggregation import Aggregator  # noqa: E402
from settings import DAY_BOUNDARY_UTC_HOUR, SECONDS_PER_HOUR, day_floor  # noqa: E402


# Windows 控制台默认按 cp1252 编码，打印中文会抛 UnicodeEncodeError，把整个 CI 步骤
# 直接跑挂（本地 macOS/Linux 是 UTF-8，永远复现不出来）。stdout / stderr 都定成 UTF-8 并
# 用 replace 兜底：日志里最多出现问号，不会因为一句话把构建/清理停掉。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")


def _parse_day(text: str | None) -> int | None:
    """把 YYYY-MM-DD 解析成该统计日的 day_start；None 表示不限。

    报表里的日期标签是 day_start 的 UTC 日期（reports.py），所以正确的反解是
    「该日 00:00 UTC + 日切点在 UTC 的偏移」。用 day_floor() 会把 00:00 UTC
    归到前一天——默认日切点（08:00 本地 = 00:00 UTC）下两者等价，改过
    EMS_DAY_START_HOUR 的现场就会整体错一天。
    """
    if not text:
        return None
    moment = datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(moment.timestamp()) + DAY_BOUNDARY_UTC_HOUR * SECONDS_PER_HOUR


def _hours_to_rebuild(from_ts: int | None, to_ts: int | None) -> list[int]:
    """需要重建的小时集合：读数所在的整点，加上跨小时区间途经的整点。

    电量窗是左开右闭的：10:00 整点这条读数，既是 09:00 那个小时的收口值，
    也是 10:00 那个小时的基准。老实现只按 `ts // 3600` 归档，于是「08:30 与
    10:00 各一条读数」时 09:00 那个小时（真实有电量）根本不在重建名单里。
    这里改成按相邻两条读数的区间扫一遍，把途经的整点全部纳入。

    不能简单地「每个整点再补上 hour-3600」：那会给上一个小时造出一行 energy=0
    的假数据（窗口里只有本小时的整点读数、没有基准），正是本脚本要清掉的那种脏行。
    """
    conditions: list[str] = []
    params: list[object] = []
    if from_ts is not None:
        conditions.append("ts>=?")
        params.append(int(from_ts))
    if to_ts is not None:
        conditions.append("ts<?")
        params.append(int(to_ts))
    where: str = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    rows = db.query(f"SELECT DISTINCT ts FROM reading{where} ORDER BY ts", tuple(params))
    stamps: list[int] = [int(row["ts"]) for row in rows]
    if not stamps:
        return []

    # 窗口前最近一条读数：它是窗口内第一条读数的基准，跨小时区间要从它算起
    previous: int | None = None
    if from_ts is not None:
        prior = db.query_one("SELECT MAX(ts) AS ts FROM reading WHERE ts<?", (int(from_ts),))
        if prior is not None and prior["ts"] is not None:
            previous = int(prior["ts"])

    hours: set[int] = set()
    for ts in stamps:
        hours.add((ts // SECONDS_PER_HOUR) * SECONDS_PER_HOUR)
        if previous is not None:
            for index in range(previous // SECONDS_PER_HOUR + 1, ts // SECONDS_PER_HOUR + 1):
                hours.add(index * SECONDS_PER_HOUR)
        previous = ts
    return sorted(hours)


def _energy_totals(from_ts: int | None, to_ts: int | None) -> tuple[int, int]:
    """返回 (daily 电量合计毫度, 行数)，用于重算前后对比。"""
    conditions: list[str] = []
    params: list[object] = []
    if from_ts is not None:
        conditions.append("day_start>=?")
        params.append(int(from_ts))
    if to_ts is not None:
        conditions.append("day_start<?")
        params.append(int(to_ts))
    where: str = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    row = db.query_one(
        f"SELECT COALESCE(SUM(energy_wh),0) AS total, COUNT(*) AS cnt FROM daily{where}",
        tuple(params),
    )
    return int(row["total"] or 0), int(row["cnt"] or 0)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="按当前口径重算 hourly / daily")
    parser.add_argument("--db", default=str(ROOT / "energy.db"), help="数据库文件路径")
    parser.add_argument("--from", dest="from_day", default=None, help="起始日 YYYY-MM-DD（含）")
    parser.add_argument("--to", dest="to_day", default=None, help="结束日 YYYY-MM-DD（含）")
    parser.add_argument("--dry-run", action="store_true", help="只统计影响面，不写库")
    parser.add_argument(
        "--strict-coverage",
        action="store_true",
        help="按当前覆盖率规则重新判定 is_final（默认保留历史定稿标记）",
    )
    args = parser.parse_args(argv)

    db_path = Path(args.db).resolve()
    if not db_path.exists():
        print(f"数据库不存在: {db_path}")
        return 2
    db.DB_PATH = db_path

    from_ts = _parse_day(args.from_day)
    to_ts = _parse_day(args.to_day)
    if to_ts is not None:
        to_ts += 86400  # --to 是闭区间

    if args.dry_run:
        # --dry-run 的承诺是「只统计影响面，不写库」。原先无条件先 init_db()，
        # 等于在看影响面之前就把 schema.sql 与迁移执行了一遍（改库结构 + 写
        # schema_version），升级后、备份前最需要只读的时候恰好是它最不老实的时候。
        db.use_readonly(db_path)
    else:
        db.init_db()
    hours = _hours_to_rebuild(from_ts, to_ts)
    if not hours:
        print("指定范围内没有可用于重算的原始读数，未做任何改动。")
        return 0

    before_total, before_rows = _energy_totals(from_ts, to_ts)
    days: list[int] = sorted({day_floor(hour) for hour in hours})
    print(f"数据库      : {db_path}")
    print(f"待重建小时  : {len(hours)} 个（{len(days)} 个统计日）")
    print(f"清空区间    : {datetime.fromtimestamp(hours[0], tz=timezone.utc):%Y-%m-%d %H:%M} UTC "
          f"起，逐小时重建")
    print(f"重算前合计  : {before_total / 1e6:,.3f} kWh / {before_rows} 行 daily")
    if args.dry_run:
        print("\n--dry-run：未写入任何数据。去掉该参数即可执行重算。")
        return 0

    aggregator = Aggregator()
    aggregator.silent = True
    started = time.monotonic()

    # 先整段清空再逐小时重建：旧口径会给「本小时没有任何读数」的时间片也写一行
    # energy=0 / sample_cnt=0（掉线被记成没用电）。只删有读数的那些小时会把这些
    # 假 0 行留在表里，所以按「有读数的首末小时」整段清空最干净。
    # 区间之外（reading 已被 90 天清理掉的部分）保持原样，历史汇总不会被抹掉。
    span_start: int = hours[0]
    span_end: int = hours[-1] + SECONDS_PER_HOUR
    removed = db.execute(
        "DELETE FROM hourly WHERE hour_start>=? AND hour_start<?", (span_start, span_end)
    )
    rebuilt = 0
    # 这一段刻意不包成单个大事务：整段包起来会让本进程长时间持有写锁，
    # 正在跑的服务端采集写 reading 会连吃 busy_timeout（5 秒）失败，
    # 用「可重跑」换「采集停摆」不划算。中断时按小时重跑即可（脚本幂等）。
    try:
        for hour in hours:
            try:
                aggregator.rollup_hour(hour)
                rebuilt += 1
            except sqlite3.Error as exc:
                print(f"  小时 {hour} 重算失败，已跳过: {exc}", file=sys.stderr)
    except KeyboardInterrupt:
        print(
            f"\n已中断：hourly 在 {span_start}~{span_end} 区间内只重建了 {rebuilt}/{len(hours)} 小时，"
            "汇总表此刻不完整。重新执行本脚本即可补齐（按小时幂等）。",
            file=sys.stderr,
        )
        return 130

    settled = 0
    kept_final = 0
    for day in days:
        previous = {
            int(row["meter_id"]): int(row["is_final"] or 0)
            for row in db.query("SELECT meter_id, is_final FROM daily WHERE day_start=?", (day,))
        }
        try:
            if args.strict_coverage:
                aggregator.settle_day(day)
            else:
                # 全部按定稿写入，再把原本未定稿的表改回去：对账口径不变，
                # 只有数字更新；严格模式才让覆盖率如实决定
                aggregator.settle_day(day, is_final=1, enforce_coverage=False)
                for meter_id, flag in previous.items():
                    if flag == 0:
                        kept_final += 1
                        db.execute(
                            "UPDATE daily SET is_final=0 WHERE meter_id=? AND day_start=?",
                            (meter_id, day),
                        )
            settled += 1
        except sqlite3.Error as exc:
            print(f"  日 {day} 重结算失败，已跳过: {exc}", file=sys.stderr)

    after_total, after_rows = _energy_totals(from_ts, to_ts)
    delta = after_total - before_total
    ratio = (delta / before_total * 100.0) if before_total else 0.0
    print(f"清除旧行    : {removed} 行")
    print(f"重建小时    : {rebuilt}/{len(hours)}")
    print(f"重结算日    : {settled}/{len(days)}（保留未定稿 {kept_final} 行）")
    print(f"重算后合计  : {after_total / 1e6:,.3f} kWh / {after_rows} 行 daily")
    print(f"电量变化    : {delta / 1e6:+,.3f} kWh（{ratio:+.2f}%）")
    print(f"耗时        : {time.monotonic() - started:.1f}s")
    db.log_event(
        "info", "api", "RECOMPUTE_DONE",
        f"历史重算完成：{rebuilt} 小时 / {settled} 日，电量 {before_total} -> {after_total} 毫度",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
