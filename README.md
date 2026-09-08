<p align="center">
  <img src="plugins/grok-subagent/assets/bridge-mark.png" width="128" height="128" alt="ChatGPT Grok Bridge: an original bridge mark">
</p>

<h1 align="center">ChatGPT Grok Bridge</h1>

<p align="center">
  <strong>Use local Grok Build as an external subagent in Codex.</strong><br>
  Start a task. Follow its progress. Stay in control.
</p>

<p align="center">
  <a href="https://github.com/luohui1/chatgpt-grok-bridge/actions/workflows/tests.yml"><img src="https://github.com/luohui1/chatgpt-grok-bridge/actions/workflows/tests.yml/badge.svg?branch=main" alt="Windows tests"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-47E6AF?style=flat-square" alt="MIT license"></a>
  <img src="https://img.shields.io/badge/Python-3.11%2B-3776AB?style=flat-square" alt="Python 3.11 or later">
  <img src="https://img.shields.io/badge/platform-Windows-0078D4?style=flat-square" alt="Windows">
  <img src="https://img.shields.io/badge/status-experimental-E6B84A?style=flat-square" alt="Experimental">
</p>

<p align="center">
  <a href="#quick-start">Quick start</a> ·
  <a href="#how-it-works">Architecture</a> ·
  <a href="#toolbox">Toolbox</a> ·
  <a href="SECURITY.md">Security</a> ·
  <a href="README.zh-CN.md">简体中文</a>
</p>

---

An open-source, unofficial Codex plugin that manages local Grok Build sessions
through **MCP** and **ACP over stdio**. It provides background jobs, follow-up
messages and explicit lifecycle control.

> [!IMPORTANT]
> **Current scope: local Codex integration on Windows.**
> The repository name is retained for existing links. Historical cloud adapter
> code remains in the repository but is outside the current supported workflow.
> This project is not affiliated with or endorsed by OpenAI or Grok/xAI.

## Why this bridge?

| Capability | What it gives you |
| --- | --- |
| **Native protocol** | ACP over stdio, not terminal screenshots or simulated keystrokes. |
| **Durable background jobs** | An immediate job ID, incremental results and explicit command receipts. |
| **Session control** | Follow-up messages, cancellation, permission responses, close and explicit recovery. |
| **Local ownership** | Workspace locks, owned-process cleanup and per-job runtime snapshots. |

Grok stays on your machine. Prompts and tool results still travel through the
configured services; **local execution does not mean offline inference**.

## Quick start

**Requirements:** Windows, Python 3.11+, the official Grok Build CLI, and your own
working Grok login. No CLI binaries, credentials or model credits are included.

### Codex

Install the local MCP plugin from this repository's marketplace:

```powershell
codex plugin marketplace add luohui1/chatgpt-grok-bridge
codex plugin add grok-subagent@grok-subagent-community
```

Open a new Codex task to load the plugin. Avoid enabling a second copy from
another marketplace. The internal `grok-subagent` ID is retained for compatibility.

### Your first task

Ask your connected assistant:

> Use Grok in the workspace I authorize to inspect the test setup.
> Do not modify files. Start it in the background and report its findings.

The assistant should check `grok_doctor`, start the authorized task, preserve its
job ID, and read the result.

## How it works

![Local Grok subagent architecture: Codex, MCP, SQLite mailbox, worker and Grok Build](docs/assets/grok-local-infographic.png)

The ChatGPT and Grok symbols identify the respective brands only. The entry point
shown here is the local Codex client, not ChatGPT cloud access.

```text
Codex -> MCP server -> SQLite job mailbox -> background worker <-> Grok Build
                                                            ACP / stdio
```

The mailbox uses SQLite WAL. Each worker starts an independent Grok session and
keeps its own Python runtime snapshot. A client disconnect does not itself cancel
the job. Recovery verifies recorded process identities instead of guessing from
a stale heartbeat.

Native integration mode disables Cursor/Claude MCP scanning for the child
process. Grok-native, plugin and managed integrations can still apply.

Read the [architecture notes](plugins/grok-subagent/RESEARCH.md) for the boundaries
between transport, job state and process lifetime.

## Performance profile

The plugin is a single-user control layer, not a model server or Codex's native
`spawn_agent` implementation.

| Measurement | Recorded Windows short test |
| --- | --- |
| Background working set | About **112 MiB**, median across sampled processes |
| Dispatch latency | About **0.1 s** to return a job ID, not complete the task |
| Cancellation response | About **0.53 s**, one measurement |

Memory includes the supervisor, Grok and console helper, excluding the Codex
application. These are short-run observations, not performance guarantees.
Completion time depends on the model, network and task.

## Toolbox

| Tool | Purpose |
| --- | --- |
| `grok_start` | Start an authorized job or explicitly resume a recorded session. |
| `grok_read` | Read status, incremental events and command receipts. |
| `grok_send` | Continue an idle session. |
| `grok_interrupt` | Cancel the current turn without pretending to undo side effects. |
| `grok_permission` | Respond to a real permission request within the user's authorization. |
| `grok_close` | Close the job and its owned processes. |
| `grok_recover` | Recover stale job state only after verifying the recorded processes stopped. |
| `grok_list` | List recent jobs visible to the entry point. |
| `grok_doctor` | Check the local CLI and compatibility baseline. |

## What is verified?

| Layer | Current evidence |
| --- | --- |
| Automated control and isolation | Windows CI on Python 3.11 and 3.12; no Grok install or credentials required. |
| Live Grok lifecycle | Recorded local checks for follow-ups, cancellation, file write/read, close and resume. |
| Process failure handling | Recorded local supervisor-crash, descendant cleanup and stale-job recovery checks. |
| Multi-day uptime / Linux / macOS | **Not claimed.** |

The tested CLI baseline is **Grok Build 1.0.13**, not a promise of compatibility
with every future CLI release. The latest automated status is shown by the CI badge.

## Development

Run the isolated test suite without Grok or model quota:

```powershell
python -m unittest discover -s plugins/grok-subagent/tests -v
```

<details>
<summary>Optional live tests: use your own login and model quota</summary>

```powershell
python plugins/grok-subagent/tests/live_smoke.py
python -m pip install -r requirements-live.txt
python plugins/grok-subagent/tests/live_resources.py
```

These create isolated fixture tasks under your local data directory.
Their output is Git-ignored. Never commit task databases, logs or personal results.

</details>

Contributions are welcome. Start with [CONTRIBUTING.md](CONTRIBUTING.md) and keep
real model calls opt-in rather than running them automatically on pull requests.

## Security and limitations

- Workspace locks and permission prompts are **not filesystem confinement**.
- Grok runs with local-account access and may reach files or networks outside the selected directory.
- Cancellation does not roll back completed file changes or external requests.
- This is a single-owner tool, not a public or multi-user remote execution service.

See [SECURITY.md](SECURITY.md) before granting execution access.

## License

[MIT](LICENSE) for this project. The [original bridge mark](plugins/grok-subagent/assets/ASSETS.md)
is included with the project. Brand symbols depicted in the architecture diagram
are for identification only and do not imply endorsement.
Third-party services, CLIs and trademarks retain their own terms.
