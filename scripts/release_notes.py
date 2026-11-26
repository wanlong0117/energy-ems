"""按 tag 生成 Release 正文：从 CHANGELOG 抽取本版段落 + 统一升级须知。

为什么需要（v0.1.3 复审 P1-5）：四个 tag 的 Release 正文是同一段通用启动说明，
而 v0.1.3 改了小时电量口径、必须重算历史，下载页却看不到——用户直接替换 exe，
历史电量会继续按旧口径偏低（300s 轮询下约低 8%）。

用法：
    python scripts/release_notes.py --tag v0.1.4 --out release_notes.md
    python scripts/release_notes.py --tag v0.1.4            # 打印到标准输出
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

HEADER = """能耗管理系统 energy-ems {tag} · Win11 x64 onedir 完整包（MIT 许可）

解压后双击 energy_ems.exe 启动，浏览器打开 http://127.0.0.1:8000/ 。
默认密码 ems2026（首次登录强制修改），也可用 EMS_PASSWORD 指定初始密码。
默认只监听本机；需要手机/其他电脑访问时设置 EMS_ALLOW_LAN=1 后再启动。
"""

INSTALL = """
## 安装与升级

1. 解压 zip 到任意目录（不要覆盖旧的 energy.db 之外的运行目录结构）；
2. 升级前先备份：`python scripts/backup.py`（或直接复制 energy.db）；
3. 双击 energy_ems.exe 启动，浏览器访问 http://127.0.0.1:8000/ ；
4. 首次启动默认 simulate 演示模式，看板与报表立即可用；
   接真表前请在配置页切到 live（切 live 会自动停用模拟数据）。

## 包内许可

`licenses/LICENSE`（MIT）与 `licenses/THIRD_PARTY_NOTICES.md`（第三方组件声明）
随二进制一同分发；再分发时请保留。
"""

RECOMPUTE_BLOCK = """
## ⚠️ 本版必须重算历史数据

本版修改了电量/结算口径，旧库中按旧算法生成的 hourly / daily 不会自动变正确：

```bash
python scripts/recompute.py --db energy.db --dry-run   # 先看影响面
python scripts/recompute.py --db energy.db             # 确认后执行
```

重算前请务必备份 energy.db；重算期间不要同时跑采集。若跳过这一步，
历史日报会继续按旧口径显示（偏低），新旧数字混在一张表里月报永远对不上。
"""


def _section(changelog: str, tag: str) -> str:
    """从 CHANGELOG 里取出本 tag 的段落（到下一个二级标题为止）。"""
    version: str = tag.lstrip("v")
    pattern = re.compile(
        rf"^##[^\n]*{re.escape(version)}[^\n]*\n(.*?)(?=^##\s|\Z)", re.S | re.M
    )
    match = pattern.search(changelog)
    return match.group(1).strip() if match else ""


def build(tag: str, changelog_path: Path) -> str:
    """拼装 Release 正文：版本段落 + 是否必须重算 + 安装说明。"""
    changelog: str = changelog_path.read_text(encoding="utf-8") if changelog_path.exists() else ""
    section: str = _section(changelog, tag)
    parts: list[str] = [HEADER.format(tag=tag)]
    if section:
        parts.append("## 本版变更\n\n" + section)
    # 只在「必须/需要重算」时提示；写了「无需重算」的版本不该出现这段警告
    if re.search(r"(必须|需要)重算|recompute\.py", section or "", re.IGNORECASE):
        parts.append(RECOMPUTE_BLOCK)
    parts.append(INSTALL)
    return "\n".join(parts).strip() + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成 GitHub Release 正文")
    parser.add_argument("--tag", required=True, help="标签名，如 v0.1.4")
    parser.add_argument("--changelog", default=str(ROOT / "CHANGELOG.md"))
    parser.add_argument("--out", default=None, help="输出文件；省略则打印到 stdout")
    args = parser.parse_args(argv)

    body: str = build(args.tag, Path(args.changelog))
    if args.out:
        Path(args.out).write_text(body, encoding="utf-8")
        print(f"Release 正文已写入 {args.out}")
    else:
        sys.stdout.write(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
