<p align="center">
  <img src="plugins/grok-subagent/assets/bridge-mark.png" width="128" height="128" alt="ChatGPT Grok Bridge 原创桥梁标志">
</p>

<h1 align="center">ChatGPT Grok Bridge</h1>

<p align="center">
  <strong>在 Codex 中将本机 Grok Build 作为外部子代理使用。</strong><br>
  后台执行，持续追问，随时掌握任务状态。
</p>

<p align="center">
  <a href="https://github.com/luohui1/chatgpt-grok-bridge/actions/workflows/tests.yml"><img src="https://github.com/luohui1/chatgpt-grok-bridge/actions/workflows/tests.yml/badge.svg?branch=main" alt="Windows 测试"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-47E6AF?style=flat-square" alt="MIT 许可证"></a>
  <img src="https://img.shields.io/badge/Python-3.11%2B-3776AB?style=flat-square" alt="Python 3.11 及以上">
  <img src="https://img.shields.io/badge/platform-Windows-0078D4?style=flat-square" alt="Windows">
  <img src="https://img.shields.io/badge/status-experimental-E6B84A?style=flat-square" alt="实验性">
</p>

<p align="center">
  <a href="#快速开始">快速开始</a> ·
  <a href="#工作原理">工作原理</a> ·
  <a href="#工具一览">工具一览</a> ·
  <a href="SECURITY.md">安全说明</a> ·
  <a href="README.md">English</a>
</p>

---

一个开源、非官方的 Codex 插件，通过 **MCP** 和 **ACP stdio**
管理本机 Grok Build 会话，提供后台任务、连续追问和明确的生命周期控制。

> [!IMPORTANT]
> **当前范围：Windows 上的 Codex 本地集成。**
> 仓库名称保留以兼容已有链接。历史云端适配代码保留，但不属于当前支持的使用路径。
> 本项目与 OpenAI、Grok/xAI 无隶属或官方背书关系。

## 为什么做这个桥接器？

| 能力 | 解决什么问题 |
| --- | --- |
| **原生协议接入** | 使用 ACP stdio，不靠终端截图或模拟键盘操作。 |
| **持久后台任务** | 立即返回任务 ID，提供增量结果和明确的命令回执。 |
| **完整会话控制** | 追问、中断、权限回应、关闭和显式恢复。 |
| **本地任务管理** | 工作目录锁、自有进程清理和每任务运行代码快照。 |

Grok 运行在你的电脑上，但提示和工具结果仍会经过所配置的服务。
**本地执行不等于离线推理。**

## 快速开始

**前提：** Windows、Python 3.11+、官方 Grok Build CLI，以及你自己的有效 Grok 登录。
本项目不附带 CLI、账号、密钥或模型额度。

### 接入 Codex

```powershell
codex plugin marketplace add luohui1/chatgpt-grok-bridge
codex plugin add grok-subagent@grok-subagent-community
```

新开一个 Codex 任务以加载插件。不要同时启用其他来源的同名副本。
内部插件 ID `grok-subagent` 保持不变，以兼容已有安装。

### 第一个任务

向已连接的助手发送：

> 用 Grok 在我授权的工作目录里检查测试配置。不要修改文件，
> 先在后台执行，再告诉我检查结果。

助手应先调用 `grok_doctor`，再启动任务、保留任务 ID 并读取结果。

## 工作原理

![Grok 外部子代理原理图：Codex、MCP、SQLite 任务信箱、后台 Worker 与 Grok Build](docs/assets/grok-local-infographic.png)

图中的 ChatGPT 与 Grok 图标仅用于品牌识别；当前入口为 Codex 本地客户端，
不表示支持 ChatGPT 云端接入。

```text
Codex -> MCP 服务 -> SQLite 任务信箱 -> 后台 Worker <-> Grok Build
                                                 ACP / stdio
```

任务状态存放于 SQLite WAL。每个 worker 启动独立 Grok 会话，并保存自己的
Python 运行代码快照。客户端断连本身不会取消任务；失联恢复会核验记录中的
进程身份，不会因为心跳过期就猜测进程已经停止。

默认轻量模式仅对该子进程关闭 Cursor/Claude MCP 配置扫描；
Grok 自身、插件和受管集成仍可能生效。详见 [架构说明](plugins/grok-subagent/RESEARCH.md)。

## 定位与性能

项目定位为单用户的外部代理控制层，复用现有 Grok Build 安装与登录。
它不是模型服务，也不是 Codex 原生 `spawn_agent` 的实现。

| 指标 | Windows 短时实测 |
| --- | --- |
| 后台进程工作集 | 采样中位数约 **112 MiB** |
| 任务派发延迟 | 约 **0.1 秒**，仅指返回任务 ID |
| 中断响应时间 | 单次测量约 **0.53 秒** |

内存统计包含后台管理进程、Grok 和控制台辅助进程，不包含 Codex 主程序。
以上为短时观察值，不是性能承诺；任务完成耗时取决于模型、网络和任务复杂度。

## 工具一览

| 工具 | 用途 |
| --- | --- |
| `grok_start` | 启动已授权任务，或显式续接已记录的会话。 |
| `grok_read` | 读取状态、增量事件和命令回执。 |
| `grok_send` | 向空闲会话发送追问。 |
| `grok_interrupt` | 中断当前轮次，不冒充已经撤销副作用。 |
| `grok_permission` | 在用户授权范围内回应实际权限请求。 |
| `grok_close` | 关闭任务及其自有进程。 |
| `grok_recover` | 确认记录中的进程已停止后，恢复失联任务状态。 |
| `grok_list` | 列出该入口可见的近期任务。 |
| `grok_doctor` | 检查本机 CLI 和兼容基线。 |

## 验证到什么程度？

| 层次 | 当前证据 |
| --- | --- |
| 自动化控制与隔离 | Windows / Python 3.11、3.12 CI；无需 Grok 安装、账号或额度。 |
| 真实 Grok 生命周期 | 已记录本地追问、中断、文件写读、关闭和恢复检查。 |
| 进程故障处理 | 已记录后台管理进程崩溃、子进程清理和失联恢复检查。 |
| 多日连续运行 / Linux / macOS | **不作已验证承诺。** |

CLI 兼容基线为 **Grok Build 1.0.13**，不代表未来所有版本都兼容。
最新自动化测试状态以页首 CI 徽章为准。

## 开发与测试

普通测试不调用 Grok，也不消耗模型额度：

```powershell
python -m unittest discover -s plugins/grok-subagent/tests -v
```

<details>
<summary>可选真实测试：使用自己的登录与额度</summary>

```powershell
python plugins/grok-subagent/tests/live_smoke.py
python -m pip install -r requirements-live.txt
python plugins/grok-subagent/tests/live_resources.py
```

这些测试在用户数据目录创建独立测试任务，输出已被 Git 忽略。
不要提交本机任务数据库、日志或个人结果。

</details>

欢迎贡献。请先阅读 [贡献说明](CONTRIBUTING.md)，不要在拉取请求中自动调用真实模型。

## 安全边界

- 目录锁和权限确认均**不是文件系统隔离**。
- Grok 使用本机账号权限，可能访问指定目录之外的文件或网络。
- 取消不能回滚已经发生的文件修改或外部请求。
- 这是单用户私人工具，不是公开或多人共享的远程执行服务。

授予执行权限前，请阅读 [SECURITY.md](SECURITY.md)。

## 许可

项目采用 [MIT 许可证](LICENSE)，包含随项目提供的 [原创桥梁标志](plugins/grok-subagent/assets/ASSETS.md)。
原理图中的品牌图标仅用于识别，不代表官方背书；第三方服务、CLI 和商标仍适用各自条款。
