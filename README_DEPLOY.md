# 能耗管理系统 energy_ems · Win11 部署说明

## 1. 目录结构（解压后）

```
energy_ems/
├── energy_ems.exe        # 主程序（onedir 包内）
├── _internal/            # PyInstaller 运行依赖
├── schema.sql            # 已内嵌只读资源（无需单独放置）
├── dashboard/            # 已内嵌单文件看板（无需单独放置）
├── build_win11.bat       # 双击启动脚本
├── README_DEPLOY.md      # 本说明
├── config.json           # 首次运行自动生成（可写，exe 同级）
└── energy.db             # 首次运行自动生成（可写，exe 同级）
```

**关键约定**：可写数据（config.json、energy.db）永远在 `energy_ems.exe` 同级目录；
只读资源（schema.sql、dashboard）从程序内嵌资源读取。升级时只替换 exe 与 `_internal/`，
不要删除 config.json / energy.db，历史数据与配置即可保留。

## 2. 启动

1. 解压 `energy-ems-win11-x64-<YYYYMMDD>.zip` 到任意目录（建议非中文路径、非 C:\Program Files，
   避免权限问题）。
2. **双击 `build_win11.bat`**。脚本会把工作目录切到 exe 所在目录并启动服务。
3. 打开浏览器访问 `http://127.0.0.1:8000/`，即可看到单文件看板。
4. 默认密码 `ems2026`（见 config.json 的 `password`，可自行修改后重启）。

> 注意：首次启动为 **simulate** 模式，内置模拟器会预置 100 电表 + 5 组冷却塔电极 +
> 5 个机械排水量手动点，并按 5 分钟间隔向真实 SQLite 写读数；看板与报表立即可用。

## 3. 数据模式切换（simulate / live）

两种方式等效：

- 看板「配置」页签 → 「数据模式切换」→ 选择 `simulate` 或 `live` → 点击「切换」；
- 直接编辑 `config.json` 的 `data_mode` 字段。

切换后**必须重启服务**（接口返回 `restart_required=true`）：

- `simulate`：启动内置模拟器（不启动 Modbus 采集器），额外每 10 分钟做一次小时/日汇总；
- `live`：只启动 Modbus 采集器（不启动模拟器），由真实设备写入读数。

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

## 4. Modbus 三协议配置

在「配置」页签的 `device` 资源中按协议填写：

| protocol | host | port | 说明 |
|---|---|---|---|
| `modbus_tcp` | 网关 IP | 502 | 标准 MBAP over TCP |
| `modbus_rtu_tcp` | 网关 IP | TCP 端口 | RTU 帧（地址+PDU+CRC16）走 TCP |
| `modbus_rtu_serial` | COM 口（如 COM3） | 波特率（如 9600/115200） | pyserial 串口，to_thread 不阻塞 |
| `manual` | 任意 | 任意 | 手动表/机械表，不参与 Modbus 采集 |

点位试读（`/api/point/{id}/read`）返回原始寄存器十六进制、解析值和四种字节序对照值，
超时上限 3 秒。若现场字节序未确认，用试读结果比对 `parsed_orders`。

## 5. 备份与恢复

- 停服务后直接复制 `energy.db` 即可完整备份（含 WAL 数据）。
- 不停机备份：先执行检查点把 WAL 合并进主库，再复制 `energy.db`：

  ```bat
  python -c "import sqlite3; c=sqlite3.connect('energy.db'); c.execute('PRAGMA wal_checkpoint(TRUNCATE)'); c.close()"
  copy energy.db energy.db.bak
  ```

- 恢复：停服务 → 用备份覆盖 `energy.db` → 重新启动。
- 旧版本库升级：直接替换程序，启动会自动重跑 schema.sql 幂等补表（`manual_reading`）
  并保留原有 12 表全部数据。

## 6. 常见问题

**Q：双击 bat 后窗口一闪而过 / 提示缺少 DLL**
A：目标机缺少 Visual C++ 运行库时安装 `vc_redist.x64.exe`（Microsoft Visual C++
Redistributable 2015-2022 x64），安装后重启电脑再试。Win11 通常已自带，
如仍报错请查看黑框中的具体缺失模块名。

**Q：端口 8000 被占用**
A：以命令行方式启动并指定端口：
`energy_ems.exe` 内部使用 settings.HTTP_PORT，源码运行时可用环境变量或直接修改源码常量后重新打包。

**Q：看板为空**
A：确认已登录；simulate 模式首屏会立即产数，若仍为空检查 `energy.db` 是否可写、
事件日志页签是否有 `BASELINE_READY` / `SIMULATE_FAIL` 记录。

**Q：PDF 中文乱码**
A：reportlab 使用内置 `STSong-Light` CID 字体，无需外部字体文件；生僻字可能缺字，
属正常现象，常用简体中文均正常。

## 7. 从源码运行（开发/验收）

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python main.py
```

macOS 不交叉产 exe；Windows 安装包仅由 GitHub Actions 的 `windows-latest` runner 产出，
在 Actions 页面下载 `energy-ems-win11-x64-<YYYYMMDD>.zip` artifact 即可。
