# Security

## Intended deployment

This is an experimental, single-owner local automation tool. It is not an
OS sandbox and is not designed as a public or multi-tenant execution service.
The Grok process uses the local account's filesystem and network privileges.
Workspace aliases, locks and client scopes do not constrain those privileges.

The ChatGPT adapter requires an owner-only authenticated Secure MCP Tunnel and
explicit local workspace configuration. Do not expose its stdio executor
through an unauthenticated HTTP proxy or share its tunnel with untrusted users.
Do not give a long-lived tunnel runtime an administrator API key.

## Local data

The default data directory is `~/.grok-subagent`. It contains task prompts,
answers, events, paths, process identities and runtime code snapshots.
Treat these records as potentially sensitive. The repository excludes local
databases, runtime logs, model outputs, credentials and third-party binaries.

Recovery does not guess that an inaccessible process is dead. Older jobs
without sufficient identity evidence can require manual inspection.
Cancellation and forced process termination do not roll back side effects.

## Reporting

Never post credentials, private prompts or task databases in a public issue.
Use GitHub private vulnerability reporting when available. Otherwise submit
only a minimal, non-sensitive description and arrange private disclosure before
sharing exploit details. No response-time or security-audit guarantee is made.

## Verification boundaries

Automated tests use a fake ACP peer and temporary directories. Live tests are
explicitly opt-in and can consume model quota. Passing these tests does not
prove multi-day reliability, cloud ChatGPT connectivity, OS confinement, or
compatibility with every CLI release.
