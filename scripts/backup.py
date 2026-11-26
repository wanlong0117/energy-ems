"""SQLite 在线备份：用 backup API 复制 WAL 模式下的数据库，不锁写事务。

用法：
    python scripts/backup.py                       # 备份到 ./backups/
    python scripts/backup.py --keep 14             # 只保留最近 14 份
    python scripts/backup.py --db D:/ems/energy.db --out E:/ems-backup

建议在工控机「任务计划程序」里每天日结后执行一次，并把 backups 目录同步到
NAS/移动硬盘。备份文件是完整数据库快照，恢复时停服务、替换 energy.db 即可
（同目录的 energy.db-wal / -shm 需一并删除，SQLite 会按备份重建）。
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path


# Windows 控制台默认按 cp1252 编码，打印中文会抛 UnicodeEncodeError，把整个 CI 步骤
# 直接跑挂（本地 macOS/Linux 是 UTF-8，永远复现不出来）。stdout / stderr 都定成 UTF-8 并
# 用 replace 兜底：日志里最多出现问号，不会因为一句话把构建/清理停掉。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")


def backup(db_path: Path, out_dir: Path) -> Path:
    """把 db_path 备份成 out_dir/energy-YYYYmmdd-HHMMSS.db，返回备份文件路径。"""
    if not db_path.is_file():
        raise FileNotFoundError(f"数据库不存在: {db_path}")
    out_dir.mkdir(parents=True, exist_ok=True)
    target: Path = out_dir / f"energy-{time.strftime('%Y%m%d-%H%M%S')}.db"
    source = sqlite3.connect(db_path, timeout=10.0)
    try:
        destination = sqlite3.connect(target)
        try:
            source.backup(destination)  # 在线备份：不阻塞采集写入
        finally:
            destination.close()
    finally:
        source.close()
    return target


def prune(out_dir: Path, keep: int) -> list[Path]:
    """只保留最近 keep 份备份，返回被删除的文件列表。"""
    if keep <= 0:
        return []
    files: list[Path] = sorted(out_dir.glob("energy-*.db"), key=lambda p: p.stat().st_mtime)
    removed: list[Path] = files[:-keep] if len(files) > keep else []
    for path in removed:
        path.unlink(missing_ok=True)
    return removed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Energy EMS 数据库备份")
    parser.add_argument("--db", default="energy.db", help="数据库路径（默认 ./energy.db）")
    parser.add_argument("--out", default="backups", help="备份目录（默认 ./backups）")
    parser.add_argument("--keep", type=int, default=14, help="保留份数（默认 14，0 表示不清理）")
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    out_dir = Path(args.out)
    try:
        target = backup(db_path, out_dir)
    except (OSError, sqlite3.Error) as exc:
        print(f"备份失败: {exc}", file=sys.stderr)
        return 1
    removed = prune(out_dir, int(args.keep))
    print(f"已备份: {target} ({target.stat().st_size} 字节)")
    if removed:
        print("已清理旧备份: " + ", ".join(path.name for path in removed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
