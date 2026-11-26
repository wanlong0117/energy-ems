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

1. 解压 `energy-ems-v0.1.5-win11-x64.zip` 到任意目录（建议非中文路径、非 `C:\Program Files`，
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
覆盖分母变成 `24 − 停机小时`，只要生产时段的数据齐，当天就能定稿。

阈值有两条设置路径，表级优先：

| 位置 | 字段 | 说明 |
|---|---|---|
| 全局 | 环境变量 `EMS_SHUTDOWN_POWER_W` | 默认 `0`，即**关闭判定**（行为与 0.1.4 一致） |
| 单表 | 「配置 → meter → `shutdown_power_w`」 | 单位 W；留空则用全局值 |

```bat
:: 全厂默认：低于 50 W 视为停机
set EMS_SHUTDOWN_POWER_W=50
energy_ems.exe
```

现场怎么定这个值：先看夜里那几块表的 `hourly.avg_power` 典型值（看板「小时」页签），
取「待机功率 × 1.5 ~ 2」左右；注塑机、空压机这类待机功率大的设备单独填表级阈值，
别用一个全局值硬套。

三条边界要记住：

- **只认功率读数**。表没配功率点、或那个小时没采到功率，不当停机——
  否则「采集断了」会被伪装成「停机」，反而放过真正缺数据的日子；
- **停机小时照样记电量**。判定只改覆盖率口径，不改电量与电费，
  待机那点电（几度/天）仍然是真金白银；
- **没数据的小时无法判停机**。设备彻底断电时既没有电量也没有功率，
  系统只能看到「这小时没数据」，会照旧按缺数据算。

每个日报行都带 `shutdown_hours`，报表里对应「停机小时」列。想事后确认某天为什么
定稿了，看这一列 + 事件日志里的 `SETTLE_SHUTDOWN` 即可。

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
在 Actions 页面下载 `energy-ems-v0.1.5-win11-x64.zip` artifact 即可；
同名 `.sha256` 文件里有校验值，用 `certutil -hashfile <zip> SHA256` 对比。
