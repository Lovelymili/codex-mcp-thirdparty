# Contributing

Thank you for helping improve codex-mcp-thirdparty.

## Reporting an issue

Include your operating system, Python version, Codex CLI version, client version, and the smallest set of steps that reproduces the problem. Attach the relevant error and the non-sensitive parts of `codex-mcp-thirdparty doctor`.

Remove credentials, private prompts, source code, local usernames, and project paths before posting diagnostics. Never upload Codex authentication files, complete Claude configuration backups, or conversation history.

## Making a change

1. Keep the change focused on one behavior.
2. Preserve Python 3.10 compatibility and the standard-library-only runtime.
3. Keep stdout reserved for MCP JSON-RPC when serving.
4. Preserve explicit sandbox choices, cancellation cleanup, and the rule against automatically replaying started tasks.
5. Update both `README.md` and `README.zh-CN.md` when changing user-facing behavior.

Basic local checks:

```bash
python3 -m py_compile codex_mcp.py install.py
python3 codex_mcp.py --help
python3 codex_mcp.py doctor
```

For changes to task execution, verify the affected behavior in a temporary workspace with your own Codex account. For installer changes, use an isolated home/configuration directory. Describe the checks you actually performed in the pull request; a CLI health check alone does not verify model execution.

Keep generated files, local configuration, credentials, runtime snapshots, conversation metadata, and one-off validation artifacts outside the repository.

Contributions are made under the [MIT License](LICENSE).
