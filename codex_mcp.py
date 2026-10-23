#!/usr/bin/env python3
"""Independent stdio MCP server for Codex's stable non-interactive CLI.

Python 3.10+, standard library only. stdout is exclusively MCP JSON-RPC.
Prompts go through stdin, never a shell or command-line argument.
"""
import argparse
import asyncio
import collections
import contextlib
import datetime
import fcntl
import hashlib
import json
import logging
import logging.handlers
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid

VERSION = "1.0.0"
APP_NAME = "codex-mcp-thirdparty"
PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
MAX_FRAME = 8 * 1024 * 1024
SAFE_CONFIG = {"model_reasoning_effort", "model_reasoning_summary", "model_verbosity",
               "model_context_window", "model_auto_compact_token_limit", "service_tier",
               "web_search", "developer_instructions"}
SANDBOXES = ("read-only", "workspace-write", "danger-full-access")


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return default


def redact(text):
    text = re.sub(r"(?i)(bearer\s+)[\w.\-]+", r"\1[REDACTED]", text)
    return re.sub(r"\b(?:sk-[\w-]{10,}|eyJ[\w-]{20,}\.[\w.-]+)\b", "[REDACTED]", text)


class BridgeError(Exception):
    pass


class Settings:
    def __init__(self):
        home = Path.home()
        default_config = home / ".config" / APP_NAME / "config.json"
        default_state = home / ".local/state" / APP_NAME
        # Keep sessions and pinned runtimes from the original local bridge usable.
        legacy_config = home / ".config/codex-mcp-bridge/config.json"
        if not default_config.exists() and legacy_config.exists():
            default_config = legacy_config
            default_state = home / ".local/state/codex-mcp-bridge"
        config_path = Path(os.environ.get("CODEX_MCP_CONFIG", default_config)).expanduser().absolute()
        self.config_path = config_path
        self.raw = read_json(config_path, {})
        if not isinstance(self.raw, dict):
            raise BridgeError("Bridge configuration must be a JSON object")
        self.state = Path(os.environ.get("CODEX_MCP_STATE_DIR", default_state)).expanduser().absolute()
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        local_command = home / ".local/bin/codex"
        discovered = str(local_command) if local_command.exists() else shutil.which("codex") or "codex"
        self.command = self.raw.get("codex_command", discovered)
        if not isinstance(self.command, str) or not self.command.strip() or "\0" in self.command:
            raise BridgeError("codex_command must be an executable path or command name")
        self.command = os.path.expanduser(self.command)
        self.timeout = self.raw.get("timeout_seconds", 3600)
        self.sandbox = self.raw.get("default_sandbox", "read-only")
        self.parallel = self.raw.get("max_parallel", 4)
        self.progress_interval = self.raw.get("progress_interval_seconds", 10)
        if self.sandbox not in SANDBOXES or type(self.parallel) is not int or not 1 <= self.parallel <= 32:
            raise BridgeError("Invalid bridge default_sandbox or max_parallel configuration")
        if type(self.timeout) not in (int, float) or not 1 <= self.timeout <= 86400:
            raise BridgeError("timeout_seconds must be between 1 and 86400")
        if type(self.progress_interval) not in (int, float) or not 1 <= self.progress_interval <= 60:
            raise BridgeError("progress_interval_seconds must be between 1 and 60")
        self.log = logging.getLogger(APP_NAME)
        self.log.setLevel(logging.INFO)
        if not self.log.handlers:
            handler = logging.handlers.RotatingFileHandler(self.state / "bridge.log", maxBytes=1048576, backupCount=3)
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            self.log.addHandler(handler)


class Runtime:
    def __init__(self, settings):
        self.settings = settings
        self.cache = {}

    def probe(self, path):
        try:
            path = Path(path).expanduser().resolve(strict=True)
            stat = path.stat()
            key = (str(path), stat.st_ino, stat.st_size, stat.st_mtime_ns)
            if key in self.cache:
                return self.cache[key]
            def run(args):
                result = subprocess.run([str(path), *args], stdin=subprocess.DEVNULL,
                                        capture_output=True, text=True, timeout=8)
                if result.returncode:
                    raise BridgeError("CLI capability check failed: " + redact(result.stderr[-1200:]))
                return result.stdout
            version = run(["--version"]).strip()
            top = run(["--help"])
            help_exec = run(["exec", "--help"])
            help_resume = run(["exec", "resume", "--help"])
            for flag in ("--json", "--output-last-message", "--skip-git-repo-check", "--config"):
                if flag not in help_exec or flag not in help_resume:
                    raise BridgeError("Codex is missing a required exec/resume capability: " + flag)
            if "--sandbox" not in help_exec:
                raise BridgeError("Codex exec no longer supports explicit sandbox selection")
            result = {"path": str(path), "version": version, "no_daemon": "--no-daemon" in top,
                      "identity": list(key)}
            self.cache[key] = result
            return result
        except (OSError, subprocess.SubprocessError) as error:
            raise BridgeError(f"Cannot start Codex at {path}: {type(error).__name__}") from error

    def select(self):
        primary = self.settings.command
        if not Path(primary).is_absolute():
            primary = shutil.which(primary) or primary
        try:
            return {**self.probe(primary), "fallback": False}
        except BridgeError as error:
            primary_error = str(error)
        manifest = read_json(self.settings.state / "fallback.json", {})
        if manifest.get("path"):
            try:
                fallback = self.probe(manifest["path"])
                self.settings.log.warning("Using fallback runtime %s; primary check: %s", fallback["version"], primary_error)
                return {**fallback, "fallback": True, "primary_error": primary_error}
            except BridgeError:
                pass
        raise BridgeError(primary_error + "; no working fallback. Run codex-mcp-thirdparty doctor.")

    def pin(self):
        """Explicitly snapshot a tested standalone distribution, never credentials."""
        runtime = self.select()
        binary = Path(runtime["path"])
        package = binary.parent.parent
        if binary.parent.name != "bin" or not (package / "codex-package.json").is_file():
            raise BridgeError("pin requires an official standalone distribution with codex-package.json")
        target_root = self.settings.state / "runtimes"
        target_root.mkdir(exist_ok=True, mode=0o700)
        hasher = hashlib.sha256()
        with binary.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                hasher.update(chunk)
        digest = hasher.hexdigest()
        target = target_root / (re.sub(r"[^a-zA-Z0-9._-]", "_", runtime["version"]) + "-" + digest[:12])
        if not target.exists():
            temporary = Path(tempfile.mkdtemp(prefix=".pin-", dir=target_root))
            try:
                shutil.copytree(package, temporary / "package", symlinks=True)
                self.probe(temporary / "package/bin/codex")
                os.rename(temporary / "package", target)
            finally:
                shutil.rmtree(temporary)
        fallback = self.probe(target / "bin/codex")
        manifest = {"path": fallback["path"], "version": fallback["version"], "sha256": digest,
                    "pinned_at": now(), "source": runtime["path"]}
        atomic_json(self.settings.state / "fallback.json", manifest)
        return manifest


STRING = {"type": "string"}
COMMON = {
    "prompt": {"type": "string", "minLength": 1, "description": "Task or follow-up prompt. May include code and newlines."},
    "model": {"type": "string", "description": "Optional model; otherwise use your existing Codex configuration."},
    "cwd": {"type": "string", "description": "Absolute project directory. For replies, defaults to the original directory."},
    "sandbox": {"type": "string", "enum": list(SANDBOXES), "description": "Default read-only. Use workspace-write for authorized edits. danger-full-access disables the sandbox and must be explicitly requested."},
    "approval-policy": {"type": "string", "enum": ["never"], "description": "Non-interactive execution cannot show approval prompts; restricted operations fail instead of hanging."},
    "reasoning_effort": {"type": "string", "enum": ["minimal", "low", "medium", "high", "xhigh", "max"]},
    "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 86400, "description": "Default 3600 seconds. Cancellation stops the running process group."},
    "config": {"type": "object", "additionalProperties": False,
               "properties": {key: ({"type": "integer"} if key in {"model_context_window", "model_auto_compact_token_limit"} else STRING) for key in sorted(SAFE_CONFIG)},
               "description": "Per-call Codex overrides; does not edit global configuration."},
    "images": {"type": "array", "items": STRING, "description": "Existing local image paths."},
    "add_dirs": {"type": "array", "items": STRING, "description": "Additional writable directories when sandbox=workspace-write."},
}


def tool(name, description, properties, required, readonly=False):
    return {"name": name, "description": description,
            "inputSchema": {"type": "object", "properties": properties, "required": required, "additionalProperties": False},
            "annotations": {"readOnlyHint": readonly, "destructiveHint": not readonly,
                            "idempotentHint": readonly, "openWorldHint": not readonly}}


TOOLS = [
    tool("codex", "Run a task in a new Codex conversation. Returns threadId for codex-reply. Uses your existing Codex login and model. Set cwd to the project; use sandbox=workspace-write for edits. Long tasks emit progress. A failed/cancelled task may already have made changes: inspect before retrying.", COMMON, ["prompt"]),
    tool("codex-reply", "Continue a Codex conversation across MCP/Claude restarts. Pass threadId from codex. Legacy conversationId is also accepted. Only one turn may run per conversation.", {**COMMON, "threadId": STRING, "conversationId": STRING}, ["prompt"]),
    tool("codex-status", "Check Codex executable, compatibility, login state, fallback runtime, and active tasks without making a model request.", {}, [], True),
    tool("codex-sessions", "List recent conversations created by this bridge (metadata only) to recover threadId for codex-reply.", {"limit": {"type": "integer", "minimum": 1, "maximum": 100}}, [], True),
]


def validate(args, schema):
    if not isinstance(args, dict):
        raise BridgeError("Tool arguments must be an object")
    unknown = set(args) - set(schema["properties"])
    if unknown:
        raise BridgeError("Unknown arguments: " + ", ".join(sorted(unknown)))
    for field in schema["required"]:
        if field not in args:
            raise BridgeError("Missing argument: " + field)
    for key, value in args.items():
        rule = schema["properties"][key]
        kind = rule["type"]
        valid = ((kind == "string" and isinstance(value, str)) or
                 (kind == "integer" and isinstance(value, int) and not isinstance(value, bool)) or
                 (kind == "object" and isinstance(value, dict)) or
                 (kind == "array" and isinstance(value, list) and all(isinstance(x, str) for x in value)))
        if not valid:
            raise BridgeError(f"Invalid type for {key}: expected {kind}")
        if "enum" in rule and value not in rule["enum"]:
            raise BridgeError(f"Unsupported {key}: {value}; allowed: {rule['enum']}")
        if kind == "string" and "\0" in value:
            raise BridgeError(f"NUL byte in {key}")
        if "minimum" in rule and not rule["minimum"] <= value <= rule["maximum"]:
            raise BridgeError(f"{key} is out of range")
        if "minLength" in rule and not value.strip():
            raise BridgeError(f"{key} must not be blank")
        if kind == "object":
            validate(value, {**rule, "required": []})


async def stop_process(process):
    # Even if the leader exited, clean up any children left in its process group.
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        await asyncio.wait_for(process.wait(), timeout=2)
    except asyncio.TimeoutError:
        pass
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    await process.wait()


class Server:
    def __init__(self, settings):
        self.settings = settings
        self.runtime = Runtime(settings)
        self.tasks = {}
        self.running = {}
        self.initialized = False
        self.transport = None

    def send(self, value):
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", **value}, ensure_ascii=False, allow_nan=False) + "\n")
        sys.stdout.flush()

    def response(self, request_id, result):
        self.send({"id": request_id, "result": result})

    def error(self, request_id, code, message):
        self.send({"id": request_id, "error": {"code": code, "message": message}})

    @staticmethod
    def result(value, error=False):
        return {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}],
                "structuredContent": value, "isError": error}

    async def status(self):
        selected = await asyncio.to_thread(self.runtime.select)
        auth = await asyncio.create_subprocess_exec(selected["path"], "login", "status",
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, start_new_session=True)
        try:
            stdout, stderr = await asyncio.wait_for(auth.communicate(), timeout=10)
            text = (stdout + stderr).decode(errors="replace")
            login = "logged-in" if auth.returncode == 0 else "not-logged-in"
            if auth.returncode == 0 and not re.search(r"logged in", text, re.I):
                login = "check-succeeded"
        except asyncio.TimeoutError:
            login = "check-timed-out"
        finally:
            await stop_process(auth)
        return {"bridgeVersion": VERSION, "backend": "codex exec", "runtime": selected,
                "authentication": login, "fallback": read_json(self.settings.state / "fallback.json", {}),
                "defaultSandbox": self.settings.sandbox, "defaultTimeoutSeconds": self.settings.timeout,
                "activeTasks": list(self.running.values()), "stateDirectory": str(self.settings.state),
                "configFile": str(self.settings.config_path)}

    def sessions(self, limit):
        directory = self.settings.state / "sessions"
        files = sorted(directory.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)[:limit]
        return {"sessions": [read_json(path) for path in files]}

    async def execute(self, name, args, request_id, token=None):
        if len(self.running) >= self.settings.parallel:
            raise BridgeError("Concurrency limit reached; wait for an active Codex task to finish")
        thread_id = None
        prior = {}
        lock = None
        session_dir = self.settings.state / "sessions"
        session_dir.mkdir(exist_ok=True, mode=0o700)
        if name == "codex-reply":
            thread_id = args.get("threadId") or args.get("conversationId")
            if not thread_id:
                raise BridgeError("codex-reply requires threadId (or legacy conversationId)")
            if args.get("threadId") and args.get("conversationId") and args["threadId"] != args["conversationId"]:
                raise BridgeError("threadId and conversationId disagree")
            try:
                thread_id = str(uuid.UUID(thread_id))
            except ValueError as error:
                raise BridgeError("threadId must be a Codex session UUID") from error
            lock = open(session_dir / (thread_id + ".lock"), "a")
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                lock.close()
                raise BridgeError("This conversation already has an active turn") from error
            prior = read_json(session_dir / (thread_id + ".json"), {})
        self.running[request_id] = {"requestId": request_id, "threadId": thread_id, "startedAt": now()}
        process = None
        heartbeat = None
        output_tasks = []
        started = time.monotonic()
        sandbox = args.get("sandbox", prior.get("sandbox", self.settings.sandbox))
        cwd = Path(args.get("cwd", prior.get("cwd", os.getcwd()))).expanduser()
        saved_options = {**prior.get("options", {})}
        saved_options.update({key: args[key] for key in ("model", "reasoning_effort", "add_dirs") if key in args})
        config = {**prior.get("config", {}), **args.get("config", {})}
        last_event = "starting"
        stderr_lines = collections.deque(maxlen=30)
        messages = collections.deque(maxlen=16)
        turn_error = None
        completed = False
        runtime = None
        usage = None
        work = None

        def persist(status):
            if thread_id:
                # Instructions are already in Codex history; do not duplicate them here.
                atomic_json(session_dir / (thread_id + ".json"), {
                    "threadId": thread_id, "cwd": str(cwd), "sandbox": sandbox,
                    "options": saved_options,
                    "config": {k: v for k, v in config.items() if k != "developer_instructions"},
                    "createdAt": prior.get("createdAt", now()), "updatedAt": now(),
                    "status": status, "runtimeVersion": runtime["version"] if runtime else None})

        async def progress():
            while True:
                if token is not None:
                    self.send({"method": "notifications/progress", "params": {
                        "progressToken": token, "progress": round(time.monotonic() - started, 2),
                        "message": f"Codex {last_event}; threadId={thread_id or 'starting'}"}})
                await asyncio.sleep(self.settings.progress_interval)

        async def read_events(stream):
            nonlocal thread_id, last_event, turn_error, completed, usage, lock
            while line := await stream.readline():
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    continue
                if not isinstance(event, dict):
                    continue
                event_type = event.get("type", "")
                if not isinstance(event_type, str):
                    continue
                last_event = event_type
                if event_type == "thread.started":
                    raw_id = event.get("thread_id")
                    try:
                        new_id = str(uuid.UUID(raw_id))
                    except (ValueError, TypeError, AttributeError) as error:
                        raise BridgeError("Codex emitted an invalid thread ID") from error
                    if thread_id and new_id != thread_id:
                        raise BridgeError("Codex resumed a different conversation; execution stopped")
                    thread_id = new_id
                    if lock is None:
                        lock = open(session_dir / (thread_id + ".lock"), "a")
                        try:
                            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        except BlockingIOError as error:
                            raise BridgeError("Codex returned a conversation already being used") from error
                    self.running[request_id]["threadId"] = thread_id
                    persist("running")
                if event_type == "item.completed":
                    item = event.get("item", {})
                    if isinstance(item, dict) and item.get("type") == "agent_message":
                        messages.append(str(item.get("text", "")))
                if event_type == "turn.completed":
                    completed = True
                    usage = event.get("usage")
                if event_type == "turn.failed":
                    turn_error = event.get("error", "Codex turn failed")

        async def read_errors(stream):
            while line := await stream.readline():
                stderr_lines.append(redact(line.decode(errors="replace")[-2000:]).strip())

        async def run_child(command, prompt):
            nonlocal process, output_tasks
            process = await asyncio.create_subprocess_exec(*command, cwd=str(cwd),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, start_new_session=True, limit=MAX_FRAME)
            output_tasks = [asyncio.create_task(read_events(process.stdout)),
                            asyncio.create_task(read_errors(process.stderr))]
            process.stdin.write(prompt.encode("utf-8"))
            try:
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            process.stdin.close()
            await asyncio.gather(*output_tasks, process.wait())

        try:
            if not cwd.is_absolute() or not cwd.is_dir():
                raise BridgeError("cwd must be an existing absolute directory")
            cwd = cwd.resolve()
            for directory in saved_options.get("add_dirs", []):
                if not Path(directory).is_absolute() or not Path(directory).is_dir():
                    raise BridgeError("add_dirs must contain existing absolute directories")
            if saved_options.get("add_dirs") and sandbox != "workspace-write":
                raise BridgeError("add_dirs requires sandbox=workspace-write")
            images = []
            for filename in args.get("images", []):
                path = Path(filename).expanduser()
                path = path if path.is_absolute() else cwd / path
                if not path.is_file():
                    raise BridgeError("Image path does not exist: " + str(path))
                images.append(str(path))
            runtime = await asyncio.to_thread(self.runtime.select)
            work = tempfile.TemporaryDirectory(prefix="turn-", dir=self.settings.state)
            output_path = Path(work.name) / "response.txt"
            command = [runtime["path"]]
            if runtime["no_daemon"]:
                command.append("--no-daemon")
            command.append("exec")
            if thread_id:
                command.extend(["resume", thread_id])
            command.extend(["--json", "--skip-git-repo-check", "--output-last-message", str(output_path)])
            if saved_options.get("model"):
                command.extend(["--model", saved_options["model"]])
            for filename in images:
                command.extend(["--image", filename])
            if saved_options.get("reasoning_effort"):
                config["model_reasoning_effort"] = saved_options["reasoning_effort"]
            # Configuration is passed as separate argv entries, never interpreted by a shell.
            for key, value in config.items():
                command.extend(["-c", key + "=" + json.dumps(value, ensure_ascii=False)])
            # Apply the explicit sandbox after all other options, on new AND resumed turns.
            command.extend(["-c", 'approval_policy="never"', "-c", 'approvals_reviewer="user"',
                            "-c", "sandbox_mode=" + json.dumps(sandbox),
                            "-c", "sandbox_workspace_write.writable_roots=" + json.dumps(saved_options.get("add_dirs", []))])
            command.append("-")
            heartbeat = asyncio.create_task(progress())
            self.settings.log.info("Task started id=%s runtime=%s fallback=%s sandbox=%s", request_id, runtime["version"], runtime["fallback"], sandbox)
            await asyncio.wait_for(run_child(command, args["prompt"]), args.get("timeout_seconds", self.settings.timeout))
            content = output_path.read_text(errors="replace") if output_path.exists() else "\n".join(messages)
            failed = bool(process.returncode or turn_error or not completed or not thread_id or not content)
            result = {"threadId": thread_id, "content": content,
                      "runtimeVersion": runtime["version"], "fallback": runtime["fallback"],
                      "durationSeconds": round(time.monotonic() - started, 2), "exitCode": process.returncode}
            if usage:
                result["usage"] = usage
            if failed:
                result["error"] = redact(str(turn_error or "Codex did not complete a turn with a response and thread ID"))
                result["diagnostics"] = "\n".join(stderr_lines)[-8000:]
                result["retryAdvice"] = "Inspect existing changes; no automatic retry was performed. Use threadId to continue if present."
            persist("failed" if failed else "completed")
            return self.result(result, failed)
        except (asyncio.TimeoutError, asyncio.CancelledError) as error:
            kind = "cancelled" if isinstance(error, asyncio.CancelledError) else "timed-out"
            persist(kind)
            return self.result({"error": kind, "threadId": thread_id,
                                "message": "Execution stopped. Existing changes remain; inspect before retrying."}, True)
        except BaseException:
            persist("failed")
            raise
        finally:
            if heartbeat:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
            if process:
                await stop_process(process)
            for task in output_tasks:
                task.cancel()
            await asyncio.gather(*output_tasks, return_exceptions=True)
            if work:
                work.cleanup()
            if lock:
                lock.close()
            self.running.pop(request_id, None)
            self.settings.log.info("Task finished id=%s thread=%s elapsed=%.2f", request_id, thread_id, time.monotonic() - started)

    async def call(self, request_id, params):
        try:
            name = params.get("name")
            schema = next((item for item in TOOLS if item["name"] == name), None)
            if schema is None:
                self.error(request_id, -32602, "Unknown tool")
                return
            args = params.get("arguments", {})
            validate(args, schema["inputSchema"])
            if name == "codex-status":
                result = self.result(await self.status())
            elif name == "codex-sessions":
                result = self.result(self.sessions(args.get("limit", 20)))
            else:
                meta = params.get("_meta", {})
                token = meta.get("progressToken") if isinstance(meta, dict) else None
                if token is not None and not isinstance(token, (str, int, float)):
                    raise BridgeError("Invalid progress token")
                result = await self.execute(name, args, request_id, token)
            self.response(request_id, result)
        except asyncio.CancelledError:
            self.response(request_id, self.result({"error": "cancelled"}, True))
        except (BridgeError, OSError, ValueError) as error:
            self.response(request_id, self.result({"error": redact(str(error))}, True))
        except Exception as error:
            self.settings.log.error("Internal error type=%s request=%s", type(error).__name__, request_id)
            self.response(request_id, self.result({"error": "Internal bridge error: " + type(error).__name__}, True))
        finally:
            self.tasks.pop(request_id, None)

    async def serve(self):
        reader = asyncio.StreamReader(limit=MAX_FRAME)
        protocol = asyncio.StreamReaderProtocol(reader)
        loop = asyncio.get_running_loop()
        self.transport, _ = await loop.connect_read_pipe(lambda: protocol, sys.stdin.buffer)
        current = asyncio.current_task()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, current.cancel)
        try:
            while line := await reader.readline():
                if not line.strip():
                    continue
                try:
                    request = json.loads(line, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
                except (ValueError, UnicodeDecodeError):
                    self.error(None, -32700, "Parse error")
                    continue
                if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
                    self.error(None, -32600, "Invalid Request")
                    continue
                request_id = request.get("id")
                if isinstance(request_id, bool) or (request_id is not None and not isinstance(request_id, (str, int))):
                    self.error(None, -32600, "Invalid request ID")
                    continue
                method = request.get("method")
                params = request.get("params", {})
                if not isinstance(params, dict):
                    if request_id is not None:
                        self.error(request_id, -32602, "params must be an object")
                    continue
                if method == "notifications/cancelled":
                    cancelled_id = params.get("requestId")
                    if isinstance(cancelled_id, (str, int)):
                        task = self.tasks.get(cancelled_id)
                        if task:
                            task.cancel()
                    continue
                if request_id is None:
                    continue
                if request_id in self.tasks:
                    self.error(request_id, -32600, "Duplicate active request ID")
                elif method == "initialize":
                    self.initialized = True
                    offered = params.get("protocolVersion")
                    self.response(request_id, {"protocolVersion": offered if offered in PROTOCOLS else PROTOCOLS[0],
                        "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": {"name": APP_NAME, "version": VERSION},
                        "instructions": "Use codex for a new task and codex-reply with its threadId to continue. Set cwd explicitly. Default sandbox is read-only; use workspace-write for authorized edits. Do not automatically retry failed mutating tasks. codex-status checks health; codex-sessions recovers IDs."})
                elif method == "ping":
                    self.response(request_id, {})
                elif not self.initialized:
                    self.error(request_id, -32002, "Initialize first")
                elif method == "tools/list":
                    self.response(request_id, {"tools": TOOLS})
                elif method == "tools/call":
                    if len(self.tasks) >= self.settings.parallel + 8:
                        self.error(request_id, -32000, "Too many in-flight requests")
                    else:
                        self.tasks[request_id] = asyncio.create_task(self.call(request_id, params))
                else:
                    self.error(request_id, -32601, "Method not found")
        except (asyncio.CancelledError, BrokenPipeError, ConnectionResetError):
            pass
        finally:
            tasks = list(self.tasks.values())
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.transport.close()


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", nargs="?", choices=["serve", "doctor", "pin"], default="serve")
    parser.add_argument("--version", action="version", version=VERSION)
    args = parser.parse_args()
    settings = Settings()
    if args.action == "serve":
        asyncio.run(Server(settings).serve())
    elif args.action == "doctor":
        report = asyncio.run(Server(settings).status())
        print(json.dumps(report, indent=2, ensure_ascii=False))
        if report["authentication"] not in ("logged-in", "check-succeeded"):
            sys.exit(1)
    elif args.action == "pin":
        print(json.dumps(Runtime(settings).pin(), indent=2, ensure_ascii=False))


def entrypoint():
    try:
        main()
    except (BridgeError, OSError, ValueError) as error:
        print(APP_NAME + ": " + redact(str(error)), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    entrypoint()
