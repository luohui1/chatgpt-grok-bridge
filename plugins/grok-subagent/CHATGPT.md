# ChatGPT 私人接入

本地适配已实现；ChatGPT 云端接通需要账号配置与真实联调，不能仅凭本地测试
声称已在 ChatGPT 可用。本插件非 Grok/xAI 官方产品。

## 路径

```text
ChatGPT -> OpenAI Secure MCP Tunnel -> 本机 tunnel-client
                                     -> chatgpt_server.py -> Grok ACP
```

采用官方的出站 HTTPS 隧道，不给本机 MCP 开公网监听端口，也不需要自建 HTTP
网关。Codex 继续用原来的本地 stdio 入口；两种入口不会互相替代。

参考：
- https://developers.openai.com/api/docs/guides/secure-mcp-tunnels
- https://github.com/openai/tunnel-client
- https://developers.openai.com/apps-sdk/deploy/connect-chatgpt

## 本地准备

在插件根目录运行：

```powershell
python scripts/chatgpt_setup.py init
python scripts/chatgpt_setup.py install-client
python scripts/chatgpt_setup.py status
```

安装器只从 OpenAI 官方 GitHub release 下载 Windows x64 客户端，并验证官方
SHA256 校验和。二进制放在 `~/.grok-subagent/chatgpt/tools/`，不写系统 PATH，
不安装服务，不自动连接。没有声称完成 Sigstore 来源证明校验。

配置文件是 `~/.grok-subagent/chatgpt/config.json`。初始工作目录清单为空，
不会自动把当前项目开放给 ChatGPT。新增一个你明确允许的目录：

```powershell
python scripts/chatgpt_setup.py add-workspace my-project "D:/your-project" --allow-execution
```

这是明确授予该入口启动 Grok 的权限。去掉 `--allow-execution` 则只登记目录，
不能启动或追问执行任务。修改配置后重启对应隧道运行实例。

重要：目录别名只是入口选择和任务可见性限制，不是文件系统沙箱。Grok 的工具
仍使用本机账号权限，可能访问目录外文件或网络。不要把这个入口分享给其他人，
也不要关联多人可访问的工作区或公开发布。需要多人使用时，必须另做身份映射、
用户隔离与 OS 级执行隔离。

## 账号接通

1. 在 OpenAI Platform 的 Tunnels 设置中创建或选择仅自己可用的私人隧道。
   隧道管理需要 Tunnels Read + Manage，运行需要 Read + Use。
2. 关联实际要使用的 ChatGPT 工作区；ChatGPT 开发者模式权限与 Tunnel 权限
   是两套独立权限。无法选择隧道时先检查这两项，不假设有套餐即有权限。
3. 使用实际返回的 Tunnel ID 生成本地 profile：

```powershell
python scripts/chatgpt_setup.py prepare --tunnel-id YOUR_ACTUAL_TUNNEL_ID
```

4. 在本机安全地配置运行用 `CONTROL_PLANE_API_KEY`。不要把 API Key 发到聊天、
   写进插件、工作目录配置或提交进代码。长驻实例不能用 admin key 代替 runtime key。
5. 按已安装客户端的 `help quickstart` 和 `help plugin` 指引运行 doctor 并连接
   managed runtime。先检查状态的 process_running、healthy、ready，再在 ChatGPT
   开发者模式创建连接并选择 Tunnel。
6. 公开包不附带官方图标。创建连接时可上传你有权使用的图标；若在本地添加
   `assets/grok-icon.png`，入口可通过 MCP serverInfo 返回它，但不保证界面自动采用。
7. 刷新工具清单并确认有 11 个工具。先调用 grok_workspaces，再运行一个明确授权的
   最小任务；验证读取、追问、取消、关闭和结果分页。

账号页面：
- https://platform.openai.com/settings/organization/tunnels
- https://platform.openai.com/settings/organization/api-keys
- https://chatgpt.com/#settings/Connectors

只有取得实际注册的 ChatGPT app/connector ID 后，才可以按 plugin-creator 的
规范加入 `.app.json`。当前不伪造 app ID，也不把本地 `.mcp.json` 当作云端注册。

## 接口与边界

- 保留启动、读取、追问、中断、权限回应、关闭、恢复、列表和诊断九个控制工具。
- 增加 `grok_workspaces`，只返回本地配置的目录别名。
- 增加 `grok_result`，分页返回完整文字结果；ChatGPT 不必尝试打开本机文件路径。
- ChatGPT 只看到自己 client_scope 下创建的任务，不能读取或控制 Codex 任务。
- 两个入口共享底层目录锁，防止同一目录被各自同时派发。
- MCP initialize 内提供流程说明，ChatGPT 不需要读取本地 SKILL.md 才能发现用法。
- 权限意图与工具副作用通过 annotations 声明；模型仍需按实际请求获得用户授权。
- 长任务不会占住一次 MCP 请求，但不能保证后台完成会自动唤醒已结束的 ChatGPT 回合。
- 隧道客户端必须运行且电脑在线。电脑关机、网络断开或账号无权限时，云端不可用。

## 更新

插件版本更新后刷新 ChatGPT 工具元数据并重启对应 tunnel runtime，避免长驻入口
仍加载旧 Python 代码。已有 worker 使用自己保存的代码快照；需要更新 Grok CLI
可执行文件时，仍须先关闭活动任务。

私人 Tunnel 不等于公开插件发行通道。公开发行需要另一套稳定 HTTPS 服务、
认证、隐私说明、来源标识及平台审核，未包含在这次私人本机接入中。
