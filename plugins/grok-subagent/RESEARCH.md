# Architecture

```text
Codex MCP stdio -> durable job mailbox -> detached worker -> Grok ACP stdio
ChatGPT tunnel -> scoped MCP adapter --^
```

The mailbox uses SQLite WAL. A worker owns an independent `--no-leader` Grok
process and keeps a private Python runtime snapshot. Commands have idempotent
request IDs and asynchronous receipts. Long-poll reads use a separate executor
from control requests.

Windows Job Objects manage process lifetime, not filesystem permissions.
Recovery compares recorded process creation identities and refuses uncertain
liveness. It does not restart inference automatically.

Native integration mode disables Cursor/Claude MCP scanning for the child
process; it does not disable every Grok-native, plugin or managed integration.
The ChatGPT adapter applies local workspace policy and client-scoped visibility,
while retaining shared workspace locking with Codex jobs.

Primary references:

- https://docs.x.ai/build/cli/headless-scripting
- https://agentclientprotocol.com/protocol/prompt-turn
- https://developers.openai.com/api/docs/guides/secure-mcp-tunnels
- https://github.com/openai/tunnel-client

No personal test transcripts, session identifiers or local machine paths are
included in this repository.
