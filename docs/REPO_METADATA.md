# 仓库展示信息（About / Topics）

GitHub 仓库右侧 About 与 Topics 决定「搜索能不能搜到、点进来 3 秒内看不看得懂」。
这两项在网页上改，或直接用下面的命令改（需具备仓库 admin 权限）。

## 现状（2026-11-27 核对）

| 项 | 当前值 | 问题 |
|---|---|---|
| Description | `轻量能耗管理系统 energy_ems - Win11 onedir 自动构建` | 第二半句是**内部构建视角**，访客不关心 onedir；也没说清「解决什么问题」「谁能用」 |
| Topics | 空 | 搜 `modbus`/`energy-management`/`scada` 都搜不到本项目 |
| Website | 空 | 建议填 Release 页或文档页 |

## 建议文案

**Description**（≤350 字符，直接整段粘贴）：

```
单机轻量级开源能源管理系统：Modbus TCP/RTU 直连、SQLite 零运维、中文 Excel/PDF 报表，Windows 解压即用。为中小工厂、园区、物业「几十到几百块表、只有工控机、没有专职 IT」的现场而做。
```

**Topics**（17 个，逗号分隔，全部小写、只含字母数字与连字符）：

```
ems, energy-management, energy-monitoring, energy-metering, modbus, modbus-tcp,
modbus-rtu, fastapi, sqlite, scada, industrial-iot, iiot, python, self-hosted,
reporting, windows-service, pyinstaller
```

挑 topics 的依据：前 7 个是**行业词**（有人按这些词找系统），中间 6 个是**技术词**
（有人按技术栈找可改的项目），最后 3 个是**部署形态词**（自托管 / Windows 服务 / 免安装包）。
不要堆到 20 个上限——语义重复的 topics（如同时有 `energy` 和 `energy-management`）会稀释相关性。

**Website**：`https://github.com/wanlong0117/energy-ems/releases/latest`

## 用 gh 一次性设置

```bash
gh auth login   # 只需一次

gh repo edit wanlong0117/energy-ems \
  --description "单机轻量级开源能源管理系统：Modbus TCP/RTU 直连、SQLite 零运维、中文 Excel/PDF 报表，Windows 解压即用。为中小工厂、园区、物业「几十到几百块表、只有工控机、没有专职 IT」的现场而做。" \
  --homepage "https://github.com/wanlong0117/energy-ems/releases/latest" \
  --add-topic ems,energy-management,energy-monitoring,energy-metering,modbus,modbus-tcp,modbus-rtu,fastapi,sqlite,scada,industrial-iot,iiot,python,self-hosted,reporting,windows-service,pyinstaller

gh repo view wanlong0117/energy-ems --json description,repositoryTopics,homepageUrl
```

## 校验清单

- [ ] 仓库首页右侧 About 显示新描述，且「About」面板出现 Topics 标签云
- [ ] GitHub 搜索 `topic:modbus language:python` 能找到本仓库（索引有延迟，通常几分钟）
- [ ] 移动端打开仓库首页，首屏能看到「这是什么 + 怎么装」两件事
