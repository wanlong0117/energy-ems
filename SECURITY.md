# 安全策略 / Security Policy

## 支持版本

| 版本 | 安全更新 |
|---|---|
| 0.1.x | ✅ |
| < 0.1 | ❌ |

## 上报漏洞

**请勿通过公开 issue 披露安全问题。**

请使用 GitHub 的私密漏洞报告：

1. 打开仓库 **Security** → **Advisories** → **Report a vulnerability**
2. 或使用 **Security** → **Report a vulnerability** 入口

报告请尽量包含：

- 影响版本 / 部署方式（源码 / exe）
- 复现步骤或 PoC
- 影响范围（未授权访问、数据泄露、拒绝服务等）
- 你希望的处理方式与可公开时间

## 响应时限

- 3 个工作日内确认收到
- 7 个工作日内给出初步评估
- 修复后发布补丁版本，并在 CHANGELOG 中致谢（如你同意）

## 设计边界（已知限制）

以下为当前版本的**有意取舍**，不属于漏洞，但部署时需注意：

| 项 | 说明 | 建议 |
|---|---|---|
| 单用户密码 | 无多用户/RBAC，所有登录者权限相同 | 仅内网使用；不要多人共享账号 |
| 无内置 TLS | 服务以 HTTP 暴露 8000 端口 | 公网访问必须置于反向代理（Nginx/Caddy）并启用 HTTPS |
| 默认密码 | 默认值公开，但服务端会拦截：未改密前业务接口一律 403 | 无人值守部署用 `EMS_PASSWORD` 指定初始密码（不触发强制改密） |
| 接口文档 | `/docs`、`/openapi.json` 需登录；Swagger 资源取自 CDN | 纯内网无法加载页面时，直接用 `/openapi.json` 或看板操作 |
| 会话存内存 | 服务重启后需重新登录 | 属预期行为 |
| SQLite 单文件 | 无行级权限 | 通过操作系统文件权限保护 `energy.db` |
| `/healthz` 匿名 | 仅返回状态、版本、最近读数时间，不含配置与业务数据 | 如需隐藏，可在反向代理限制来源 IP |
| 配置损坏 | `config.json` 解析失败时拒绝登录（503）并停用后台任务，不回退默认密码 | 修正 JSON 后重启恢复 |

## 安全部署清单

- [ ] 首次登录后立即修改默认密码（服务端会拦截，改完才能用其它接口）
- [ ] 不要把 8000 端口直接映射到公网
- [ ] 反向代理启用 HTTPS，并设置 `X-Forwarded-Proto`
- [ ] 启用 HTTPS 后设置环境变量 `EMS_COOKIE_SECURE=1`，让会话 Cookie 带 Secure 标志
- [ ] 限制 `energy.db` 与 `config.json` 的文件权限（程序会把 `config.json` 写成 0600）
- [ ] 定期备份 `energy.db`：`python scripts/backup.py --db energy.db --out backups --keep 14`
- [ ] 关注依赖安全公告；本项目依赖已锁定具体版本，升级时请同步跑 `pytest` 与 `ruff check`
