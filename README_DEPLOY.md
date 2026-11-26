# 能耗管理系统 energy_ems · Win11 部署说明

## 1. 目录结构（解压后）

```
energy_ems/
├── energy_ems.exe        # 主程序（onedir 包内）
├── _internal/            # PyInstaller 运行依赖
├── schema.sql            # 已内嵌只读资源（无需单独放置）
├── dashboard/            # 已内嵌单文件看板（无需单独放置）
├── build_win11.bat       # 备用启动脚本（主启动双击 energy_ems.exe）
├── README_DEPLOY.md      # 本说明
├── licenses/             # LICENSE（MIT）与 THIRD_PARTY_NOTICES.md，随包分发不可删除
├── scripts/              # backup.py / recompute.py / clear_demo.py
├── config.json           # 首次运行自动生成（可写，exe 同级）
└── energy.db             # 首次运行自动生成（可写，exe 同级）
```

**关键约定**：可写数据（config.json、energy.db）永远在 `energy_ems.exe` 同级目录；
只读资源（schema.sql、dashboard）从程序内嵌资源读取。升级时只替换 exe 与 `_internal/`，
不要删除 config.json / energy.db，历史数据与配置即可保留。

## 2. 启动

> ⚠️ **v0.1.7 及更早的 Windows 包有阻断缺陷，必须换成 v0.1.8 及以上**：
> 双击后窗口一闪就退，日志只有一行
> `ERROR: Error loading ASGI app. Could not import module "main".`，服务根本没起来。
> 原因是打包后按名字导入 `main` 模块失效（源码运行不受影响）。已在 v0.1.8 修复，
> 并加了「CI 真跑 exe 打 `/healthz`」的冒烟检查防止复发。

1. 解压 `energy-ems-v0.1.8-win11-x64.zip` 到任意目录（建议非中文路径、非 `C:\Program Files`，
   避免权限问题）。资产文件名带版本号，同一天的多个补丁包不会再同名。
2. **双击 `energy_ems.exe`** 启动（`build_win11.bat` 为可选备用脚本）。
3. 打开浏览器访问 `http://127.0.0.1:8000/`，即可看到单文件看板。
4. 默认密码 `ems2026`（见 config.json 的 `password`，可自行修改后重启）。

> **默认只监听本机**（127.0.0.1）。需要用手机或其他电脑访问时，先设置环境变量
> `EMS_ALLOW_LAN=1` 再启动（等价于 `EMS_HOST=0.0.0.0`），并务必先改掉默认密码——
> 默认口令是公开的，直接监听局域网等于让同网段任何人都能抢先登录改密、把管理员锁在外面。

> 注意：首次启动为 **simulate** 模式，内置模拟器会预置 100 电表 + 5 组冷却塔电极 +
> 5 个机械排水量手动点，并按 5 分钟间隔向真实 SQLite 写读数；看板与报表立即可用。

## 3. 数据模式切换（simulate / live）

两种方式等效：

- 看板「配置」页签 → 「数据模式切换」→ 选择 `simulate` 或 `live` → 点击「切换」；
- 直接编辑 `config.json` 的 `data_mode` 字段。

切换后**必须重启服务**（接口返回 `restart_required=true`）：

- `simulate`：启动内置模拟器（不启动 Modbus 采集器），额外每 10 分钟做一次小时/日汇总，
  并从旧库恢复此前被 live 停用过的演示数据；
- `live`：只启动 Modbus 采集器与小时汇总（不启动模拟器），由真实设备写入读数；
  **启动时会自动停用全部演示数据**（`is_demo=1` 的表/点位/设备），
  日结算覆盖率只按真实表计算，不会再被 100 块模拟表拖成「未结算」。

> 演示数据只是停用、没有删除。确认不再需要演示时执行一次
> `python scripts\clear_demo.py --db energy.db --dry-run` 看影响面，
> 再去掉 `--dry-run` 清理历史读数与旧日报。

两种模式互斥，共用同一套 API / 看板 / 报表，无需维护假数据分支。
常用可调字段（config.json）：

```json
{
  "password": "ems2026",
  "data_mode": "simulate",
  "simulate_interval_sec": 300,
  "simulate_rollup_sec": 600
}
```

## 4. 停机判定与日结算定稿

日结算只在「数据够全」时才把日报标成已结算（`is_final=1`），否则报表会显示
「已结算=否」，拿它对账的人会以为整套系统坏了。但车间夜里停产、周末停线时，
电表仍在线、功率掉到待机值、电量≈0——这些小时本来就没有生产电量，
把它们算进「应有数据的小时」，结果是停产日永远差一口气，定不了稿。

做法：某个小时的平均功率低于阈值 → 该小时判为**停机**，从覆盖率分母里剔除。
覆盖分母变成 `24 − 停机小时`，**分子只算「有数据且非停机」的小时**，两边同源。
只要生产时段的数据齐，当天就能定稿。

> ⚠️ 分母和分子必须同时剔停机小时。只剔分母会让比值被凭空放大：12 小时待机有数 +
> 12 小时生产全缺，也能算出 `12/12 = 1.0` 并定稿，等于替一次采集中断签字。
> 0.1.6 之前就是这个口径，升级后请留意原本「已结算」的日子可能变回「未结算」——
> 那不是倒退，是它本来就该等你核查。

阈值有两条设置路径，表级优先：

| 位置 | 字段 | 说明 |
|---|---|---|
| 全局 | 环境变量 `EMS_SHUTDOWN_POWER_W` | 默认 `0`，即**关闭判定**（行为与 0.1.4 一致） |
| 单表 | 「配置 → meter → `shutdown_power_w`」 | 单位 W；留空则用全局值 |
| 缺口推断 | 环境变量 `EMS_SHUTDOWN_GAP_MAX_HOURS` | 默认 `3`；填 `0` 关闭推断（最保守） |

```bat
:: 全厂默认：低于 50 W 视为停机
set EMS_SHUTDOWN_POWER_W=50
energy_ems.exe
```

现场怎么定这个值：先看夜里那几块表的 `hourly.avg_power` 典型值（看板「小时」页签），
取「待机功率 × 1.5 ~ 2」左右；注塑机、空压机这类待机功率大的设备单独填表级阈值，
别用一个全局值硬套。

**缺口推断**：待机时段本来就可能一条数据都没有（设备随产线断电）。这种缺口只有同时
满足两条才按停机处理——① 左右两侧最近的有数据小时都判定为停机；② 连续长度不超过
`EMS_SHUTDOWN_GAP_MAX_HOURS`。贴着日界（凌晨/收班后）的缺口、以及超过上限的长缺口
一律按生产小时算，继续拖住定稿：半天的采集空白不该由系统替人签字。

四条边界要记住：

- **只认功率读数**。表没配功率点、或那个小时没采到功率，不当停机——
  否则「采集断了」会被伪装成「停机」，反而放过真正缺数据的日子；
- **停机小时照样记电量**。判定只改覆盖率口径，不改电量与电费，
  待机那点电（几度/天）仍然是真金白银；
- **没数据的小时默认算缺数据**。设备彻底断电时既没有电量也没有功率，
  系统只能看到「这小时没数据」，只有满足上面的缺口推断条件才会当停机；
- **长缺口不推断**。采集器挂了半天、网线断了半天，这类日子本来就该人工看一眼。

每个日报行都带 `shutdown_hours`（含推断出来的缺口小时），报表里对应「停机小时」列。
想事后确认某天为什么定稿了，看这一列 + 事件日志里的 `SETTLE_SHUTDOWN`（消息里会写明
「含缺口 N」）即可。

从 0.1.5 升级上来、想让历史日报按新口径重判定稿状态：

```bat
python scripts\recompute.py --db energy.db --dry-run          :: 先看影响面
python scripts\recompute.py --db energy.db --strict-coverage  :: 按当前口径重判定稿
```

不加 `--strict-coverage` 时会保留原来的定稿标记，只更新数字与 `shutdown_hours`。

## 5. Modbus 三协议配置

在「配置」页签的 `device` 资源中按协议填写：

| protocol | host | port | 说明 |
|---|---|---|---|
| `modbus_tcp` | 网关 IP | 502 | 标准 MBAP over TCP |
| `modbus_rtu_tcp` | 网关 IP | TCP 端口 | RTU 帧（地址+PDU+CRC16）走 TCP |
| `modbus_rtu_serial` | COM 口（如 COM3） | 波特率（如 9600/115200） | pyserial 串口，to_thread 不阻塞 |
| `manual` | 任意 | 任意 | 手动表/机械表，不参与 Modbus 采集 |

点位试读（`/api/point/{id}/read`）返回原始寄存器十六进制、解析值和四种字节序对照值，
超时上限 3 秒。若现场字节序未确认，用试读结果比对 `parsed_orders`。

## 6. 备份与恢复

- 停服务后直接复制 `energy.db` 即可完整备份（含 WAL 数据）。
- 推荐停服后复制：`energy.db` 在服务关闭时会自动检查点，直接复制最稳妥。
- 若确实需要不停机备份：exe 包不含 Python 解释器，请在装有 Python 的机器上执行检查点，
  再把 `energy.db` 复制过来：

  ```bat
  python -c "import sqlite3; c=sqlite3.connect('energy.db'); c.execute('PRAGMA wal_checkpoint(TRUNCATE)'); c.close()"
  copy energy.db energy.db.bak
  ```

- 恢复：停服务 → 用备份覆盖 `energy.db`（同时删除 `energy.db-wal` / `energy.db-shm`）→ 重新启动。
- 源码运行时可用内置脚本做在线备份（SQLite backup API，不锁采集写入）：

  ```bash
  python scripts/backup.py --db energy.db --out backups --keep 14
  ```

- 旧版本库升级：直接替换程序，启动会自动执行 `schema.sql` 与 `migrations.py` 的版本化迁移
  （补齐新增列/索引、实体主键改自增），13 张业务表数据全部保留；迁移记录见 `schema_version` 表。

- **v0.1.3 升级后必须重算历史**（重要）：本版修正了小时电量的取窗方式，旧库里
  `hourly` / `daily` 的数字是在「整点读数丢一个采样间隔」的旧口径下算出来的，
  300s 轮询实测偏低约 8.3%。升级步骤：

  1. 停服务，备份 `energy.db`（见上）；
  2. 在装有 Python 的机器上先试算影响面：

     ```bat
     python scripts\recompute.py --db energy.db --dry-run
     ```

  3. 确认无误后正式重算：

     ```bat
     python scripts\recompute.py --db energy.db
     ```

  脚本只重建「原始读数仍存在」的时间段，已过 90 天保留期的历史汇总不会被抹掉；
  默认保留原定稿标记（对账表不会整片变成未结算），加 `--strict-coverage` 才按现状重判。
  输出会打印重算前后的电量合计与变化百分比，并写入 `RECOMPUTE_DONE` 事件。

## 7. 常见问题

**Q：双击 exe 后窗口一闪而过 / 提示缺少 DLL**
A：包内 `_internal/` 已内置 `VCRUNTIME140.dll`，Win11 通常无需另装 VC++ 运行库。
请先确认 zip 已完整解压（`energy_ems.exe` 与 `_internal/` 同级），并检查 Windows Defender
是否误删了 exe（可加白名单后重新解压）。如仍报错，查看黑框里的具体缺失模块名。

**Q：手机或另一台电脑打不开看板**
A：默认只监听 127.0.0.1 是刻意的安全默认。确认已改掉默认密码后，用
`set EMS_ALLOW_LAN=1` 再启动 exe；随后在手机浏览器访问 `http://<本机IP>:8000/`。

**Q：端口 8000 被占用**
A：exe 版端口默认 8000（可用 `EMS_PORT` 覆盖），先关闭占用它的程序（在 exe 目录打开 cmd 运行
`netstat -ano | findstr :8000`，再按 PID 结束进程）。若必须使用其他端口，
可用 Windows 端口转发：`netsh interface portproxy add v4tov4 listenport=8080 connectport=8000 connectaddress=127.0.0.1`。

**Q：看板为空**
A：确认已登录；simulate 模式首屏会立即产数，若仍为空检查 `energy.db` 是否可写、
事件日志页签是否有 `BASELINE_READY` / `SIMULATE_FAIL` 记录。

**Q：PDF 中文乱码**
A：reportlab 使用内置 `STSong-Light` CID 字体，无需外部字体文件；生僻字可能缺字，
属正常现象，常用简体中文均正常。

## 8. 从源码运行（开发/验收）

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python main.py
```

macOS 不交叉产 exe；Windows 安装包仅由 GitHub Actions 的 `windows-latest` runner 产出，
在 Actions 页面下载 `energy-ems-v0.1.8-win11-x64.zip` artifact 即可。

校验下载是否完整，两种都行：

```bat
:: Windows
certutil -hashfile energy-ems-v0.1.8-win11-x64.zip SHA256
```

```bash
# macOS / Linux：.sha256 自 0.1.7 起为纯 LF 行尾，可直接校验
shasum -a 256 -c energy-ems-v0.1.8-win11-x64.zip.sha256
```
