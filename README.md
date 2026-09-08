# Grok Subagent

**Unofficial, experimental, Windows-first.**

Control a local Grok Build CLI from Codex through native ACP and durable
background jobs. A separate adapter prepares private ChatGPT connectivity
through OpenAI Secure MCP Tunnel.

This project is not affiliated with or endorsed by Grok/xAI or OpenAI.

## 功能

- 非阻塞派发，持久任务 ID、增量结果和命令回执。
- 同会话追问、中断、权限回应、关闭和显式恢复。
- Windows 进程树清理和保守的失联进程身份核验。
- 每个任务保存运行代码快照，减少插件更新对已有任务的影响。
- 默认关闭 Cursor/Claude MCP 扫描，保留按任务继承集成的选项。
- ChatGPT 私人入口：工作目录别名、任务可见性隔离和完整文字结果分页。

## 安装

实际运行需要 Windows、Python 3.11+、官方 Grok Build CLI 和你自己的有效登录。
本项目不附带 CLI、账号、密钥或模型额度。

```powershell
codex plugin marketplace add luohui1/grok-subagent
codex plugin add grok-subagent@grok-subagent-community
```

新开一个 Codex 任务以加载工具。不要同时启用另一个来源的同名本地插件副本。
也可克隆仓库后从本地 marketplace 安装。

## 使用

让 Codex 使用 Grok 在明确授权的工作目录内执行任务。
先调用 `grok_doctor`，再通过 `grok_start` 获取任务 ID。

| 工具 | 用途 |
| --- | --- |
| `grok_start` | 启动后台任务或显式续接已记录的会话 |
| `grok_read` | 读取状态、增量事件和命令回执 |
| `grok_send` | 向空闲会话发送追问 |
| `grok_interrupt` | 中断当前轮次，不撤销已有副作用 |
| `grok_permission` | 回应实际权限请求 |
| `grok_close` | 关闭任务及其自有进程 |
| `grok_recover` | 核验失联进程已停止后释放任务锁 |
| `grok_list` | 列出近期任务 |
| `grok_doctor` | 检查本机 CLI 与版本 |

ChatGPT 专用入口另提供 `grok_workspaces` 和 `grok_result`。
接入步骤见 [ChatGPT guide](plugins/grok-subagent/CHATGPT.md)。
**本地适配器测试不代表 ChatGPT 云端已接通。**

## 测试

普通测试仅需 Python，不要求安装 Grok，不调用模型，不消耗额度：

```powershell
python -m unittest discover -s plugins/grok-subagent/tests -v
```

Windows CI 使用模拟 ACP 对端验证控制逻辑。实际 CLI 的兼容基线是 Grok
1.0.13；其他版本应运行显式 live 测试。暂不宣称 Linux/macOS 支持、多日稳定性
或任意未来 CLI 版本兼容。

以下为可选真实测试，使用你的登录和额度，并在用户数据目录创建独立测试任务：

```powershell
python plugins/grok-subagent/tests/live_smoke.py
python plugins/grok-subagent/tests/live_chatgpt.py
```

资源与崩溃测试另需可选依赖：

```powershell
python -m pip install -r requirements-live.txt
python plugins/grok-subagent/tests/live_resources.py
```

真实测试输出保存在被 Git 忽略的 `evidence/`，不要提交本机运行记录。

## 安全边界

**这不是操作系统沙箱。** Grok 以本机账号权限运行，可能访问工作目录外的文件
或网络。工作目录锁、别名、任务可见性和权限确认均不能代替 OS 隔离。

- 只运行明确授权的任务；取消不能撤销已完成修改。
- ChatGPT 入口只适合 owner-only 私人连接，默认不开放任何目录。
- 不适合直接提供为公开或多人共享的远程执行服务。
- 不自动下载或升级 Grok，不读取或复制 Grok 登录凭据。
- 任务记录可能包含敏感提示、结果和路径，请保护本机数据目录。

更多内容见 [SECURITY.md](SECURITY.md)。

## 开发与许可

源代码使用 [MIT License](LICENSE)。第三方 CLI、服务和商标不在该许可证授权范围。
公开仓库不附带 Grok 官方图标；使用产品名称仅用于说明兼容对象。

贡献说明见 [CONTRIBUTING.md](CONTRIBUTING.md)。
