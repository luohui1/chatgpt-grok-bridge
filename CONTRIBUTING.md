# Contributing

Use Python 3.11 or later. Ordinary tests require neither Grok nor credentials:

```powershell
python -m unittest discover -s plugins/grok-subagent/tests -v
```

Keep changes scoped and include regression tests for behavior changes.
Do not add auto-approval, weaken process ownership checks, silently replay
failed work, or describe workspace selection as an OS sandbox.

Keep real CLI tests opt-in. Never run them automatically on pull requests:
they execute a model with local-account permissions and consume quota.

Before sending a pull request:

- Run the ordinary tests in an environment without a Grok executable.
- Do not commit local evidence, logs, task IDs, databases, personal paths,
  secrets, downloaded executables or third-party product artwork.
- Describe which platforms and live scenarios were actually verified.
- Keep the plugin manifest and runtime version consistent.

Contributions are accepted under the repository's MIT license.
