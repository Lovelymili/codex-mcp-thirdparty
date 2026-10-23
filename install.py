#!/usr/bin/env python3
"""Install a standalone user-local launcher and optionally register Claude Code."""
import argparse
import json
import os
from pathlib import Path
import shlex
import shutil
import sys
import tempfile
import time

from codex_mcp import APP_NAME, BridgeError, Settings, atomic_json, read_json


def atomic_file(path, content, mode):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".install-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def register(path, launcher, settings):
    desired = {
        "type": "stdio", "command": str(launcher), "args": [],
        "env": {"CODEX_MCP_CONFIG": str(settings.config_path),
                "CODEX_MCP_STATE_DIR": str(settings.state)},
        "timeout": 86460000, "alwaysLoad": True,
    }
    backups = settings.state / "backups"
    backups.mkdir(parents=True, exist_ok=True, mode=0o700)
    for _ in range(5):
        original = path.read_bytes() if path.exists() else b"{}"
        data = json.loads(original)
        if not isinstance(data, dict) or not isinstance(data.get("mcpServers", {}), dict):
            raise BridgeError("Claude configuration must contain an mcpServers object")
        previous = data.get("mcpServers", {}).get("codex")
        if previous == desired:
            print("Claude Code registration is already up to date.")
            return
        stamp = time.strftime("%Y%m%d-%H%M%S") + "-" + str(time.time_ns())
        backup = backups / ("claude-config-" + stamp + ".json")
        atomic_file(backup, original, 0o600)
        current = path.read_bytes() if path.exists() else b"{}"
        if current != original:
            continue
        data.setdefault("mcpServers", {})["codex"] = desired
        atomic_json(path, data)
        if read_json(path).get("mcpServers", {}).get("codex") != desired:
            raise BridgeError("Claude configuration changed during installation; rerun the installer")
        atomic_json(settings.state / "installation.json", {
            "installedAt": stamp, "backup": str(backup), "claudeConfig": str(path),
            "previousCodexServer": previous, "server": desired,
        })
        print("Registered codex in " + str(path))
        print("Previous configuration saved to " + str(backup))
        return
    raise BridgeError("Claude configuration kept changing; rerun the installer")


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", type=Path, default=Path.home() / ".local", help="Installation prefix (default: ~/.local)")
    parser.add_argument("--codex", help="Stable Codex launcher path or command name")
    parser.add_argument("--claude-config", type=Path, default=Path.home() / ".claude.json", help="Claude Code configuration file")
    parser.add_argument("--no-register", action="store_true", help="Install without changing Claude Code configuration")
    args = parser.parse_args()
    if os.name != "posix":
        raise BridgeError("Run this installer in Linux or WSL2; native Windows is not supported")
    settings = Settings()
    prefix = args.prefix.expanduser().absolute()
    source = Path(__file__).resolve().parent
    destination = prefix / "share" / APP_NAME
    launcher = prefix / "bin" / APP_NAME
    destination.mkdir(parents=True, exist_ok=True)
    for filename in ("codex_mcp.py", "README.md", "README.zh-CN.md", "LICENSE"):
        atomic_file(destination / filename, (source / filename).read_bytes(), 0o644)
    executable = os.path.abspath(sys.executable)
    script = "#!/bin/sh\nexec " + shlex.quote(executable) + " " + shlex.quote(str(destination / "codex_mcp.py")) + ' "$@"\n'
    atomic_file(launcher, script.encode(), 0o755)
    config = dict(settings.raw)
    if args.codex:
        command = os.path.expanduser(args.codex)
        config["codex_command"] = shutil.which(command) or os.path.abspath(command)
    elif "codex_command" not in config:
        config["codex_command"] = shutil.which(settings.command) or settings.command
    config.setdefault("default_sandbox", "read-only")
    config.setdefault("timeout_seconds", 3600)
    config.setdefault("max_parallel", 4)
    config.setdefault("progress_interval_seconds", 10)
    if config != settings.raw or not settings.config_path.exists():
        atomic_json(settings.config_path, config)
    if not args.no_register:
        register(args.claude_config.expanduser().absolute(), launcher, settings)
    print("Installed " + str(launcher))
    print("Check health: " + shlex.quote(str(launcher)) + " doctor")
    if not args.no_register:
        print("Restart Claude Code to load the new MCP configuration.")


if __name__ == "__main__":
    try:
        main()
    except (BridgeError, OSError, ValueError) as error:
        print("Installation failed: " + str(error), file=sys.stderr)
        sys.exit(1)
