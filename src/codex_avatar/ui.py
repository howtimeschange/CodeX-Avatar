from __future__ import annotations

import argparse
import json
import mimetypes
import os
import subprocess
import sys
import threading
import time
import webbrowser
from collections import deque
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import worker


DEFAULT_CONFIG: dict[str, Any] = {
    "worker": {
        "dws_bin": "dws",
        "dws_timeout": 60,
        "ready_timeout": 60,
        "state_file": "~/.dws/ai-autoreply/seen.jsonl",
        "codex_bin": "codex",
        "codex_model": "",
        "codex_log_dir": "~/.dws/ai-autoreply/codex-tasks",
        "codex_session_dir": "~/.dws/ai-autoreply/codex-sessions",
        "codex_timeout": 3600,
        "autofix_min_confidence": 0.85,
        "base_url": "",
        "model": "",
        "system_prompt": "",
        "temperature": 0.2,
        "max_tokens": 256,
        "max_reply_chars": 500,
        "ai_timeout": 60,
        "reply_fallback": True,
        "duration": "",
        "max_events": 0,
        "allow_any_target": False,
    },
    "defaults": {
        "channel": "dingtalk",
        "mode": "draft",
        "profile": "",
        "project": "",
        "repo": "",
        "query": "问题,反馈,bug,报错,不能用,需求",
        "filter_json": "",
        "reply_target": "auto",
        "ready_message": "",
        "send": False,
        "trigger": "mention",
        "history_context": {
            "enabled": True,
            "limit": 20,
        },
        "reply_engine": "codex",
        "classifier": "heuristic",
        "code_keywords": [],
        "test_commands": [],
        "codex_execute": False,
        "code_write_policy": "none",
        "approval_phrases": ["OK", "好的", "继续", "确认", "可以"],
        "approval_ttl_minutes": 1440,
    },
    "groups": [
        {
            "channel": "dingtalk",
            "name": "赢在AI",
            "group": "cid_REPLACE_WINYAI",
            "project": "crawshrimp",
            "repo": "/path/to/crawshrimp",
            "mode": "guarded",
            "reply_engine": "codex",
            "trigger": "mention",
            "history_context": {
                "enabled": True,
                "limit": 20,
            },
            "code_keywords": ["抓虾", "crawshrimp"],
            "query": "",
            "ready_message": "我在了",
            "code_write_policy": "approval",
            "enabled": True,
        },
        {
            "channel": "dingtalk",
            "name": "抓虾反馈群",
            "group": "cid_REPLACE_CRAWSHRIMP",
            "project": "crawshrimp",
            "repo": "/path/to/crawshrimp",
            "mode": "guarded",
            "reply_engine": "codex",
            "trigger": "mention",
            "history_context": {
                "enabled": True,
                "limit": 20,
            },
            "code_keywords": ["抓虾", "crawshrimp"],
            "query": "",
            "test_commands": ["python3 -m pytest"],
            "code_write_policy": "approval",
            "enabled": True,
        },
    ],
}


@dataclass
class LogRecord:
    ts: float
    source: str
    line: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "time": time.strftime("%H:%M:%S", time.localtime(self.ts)),
            "source": self.source,
            "line": self.line,
        }


class DashboardState:
    def __init__(
        self,
        config_path: Path,
        *,
        python_bin: str = sys.executable,
        log_limit: int = 500,
    ) -> None:
        self.config_path = config_path.expanduser()
        self.python_bin = python_bin
        self._logs: deque[LogRecord] = deque(maxlen=log_limit)
        self._process: subprocess.Popen[str] | None = None
        self._lock = threading.RLock()

    def append_log(self, source: str, line: str) -> None:
        line = line.rstrip("\n")
        if not line:
            return
        with self._lock:
            self._logs.append(LogRecord(time.time(), source, line))

    def load_config(self) -> dict[str, Any]:
        if not self.config_path.exists():
            return json.loads(json.dumps(DEFAULT_CONFIG, ensure_ascii=False))
        with self.config_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("config root must be an object")
        data.setdefault("defaults", {})
        data.setdefault("groups", [])
        data.setdefault("worker", {})
        return data

    def save_config(self, config: dict[str, Any]) -> None:
        if not isinstance(config, dict):
            raise ValueError("config must be an object")
        if not isinstance(config.get("groups"), list):
            raise ValueError("config.groups must be an array")
        if not isinstance(config.get("defaults", {}), dict):
            raise ValueError("config.defaults must be an object")
        if not isinstance(config.get("worker", {}), dict):
            raise ValueError("config.worker must be an object")
        if isinstance(config.get("worker"), dict) and "api_key" in config["worker"]:
            raise ValueError("do not store API keys in config.worker; use DWS_AI_API_KEY in the process environment")
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.config_path.with_suffix(self.config_path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(config, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        tmp.replace(self.config_path)
        self.append_log("system", f"saved config: {self.config_path}")

    def placeholder_groups(self, config: dict[str, Any] | None = None) -> list[str]:
        config = config or self.load_config()
        out: list[str] = []
        groups = config.get("groups") or []
        if not isinstance(groups, list):
            return out
        for item in groups:
            if not isinstance(item, dict) or item.get("enabled") is False:
                continue
            group_id = str(item.get("group") or item.get("group_id") or "").strip()
            if "REPLACE" in group_id or group_id.startswith("cid_REPLACE"):
                out.append(str(item.get("name") or group_id or "unnamed"))
        return out

    def _base_worker_command(self, config: dict[str, Any] | None = None) -> list[str]:
        cmd = [
            self.python_bin,
            "-m",
            "codex_avatar",
            "--config",
            str(self.config_path),
        ]
        if config is None:
            config = self.load_config()
        worker_config = config.get("worker", {})
        if isinstance(worker_config, dict):
            self._append_worker_options(cmd, worker_config)
        return cmd

    @staticmethod
    def _append_value_flag(cmd: list[str], flag: str, value: Any) -> None:
        if value is None:
            return
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return
        cmd.extend([flag, str(value)])

    def _append_worker_options(self, cmd: list[str], worker_config: dict[str, Any]) -> None:
        value_flags = {
            "dws_bin": "--dws-bin",
            "dws_timeout": "--dws-timeout",
            "ready_timeout": "--ready-timeout",
            "state_file": "--state-file",
            "codex_bin": "--codex-bin",
            "codex_model": "--codex-model",
            "codex_log_dir": "--codex-log-dir",
            "codex_session_dir": "--codex-session-dir",
            "codex_timeout": "--codex-timeout",
            "autofix_min_confidence": "--autofix-min-confidence",
            "base_url": "--base-url",
            "model": "--model",
            "system_prompt": "--system-prompt",
            "temperature": "--temperature",
            "max_tokens": "--max-tokens",
            "max_reply_chars": "--max-reply-chars",
            "ai_timeout": "--ai-timeout",
            "duration": "--duration",
            "max_events": "--max-events",
        }
        for key, flag in value_flags.items():
            self._append_value_flag(cmd, flag, worker_config.get(key))
        if worker_config.get("allow_any_target") is True:
            cmd.append("--allow-any-target")
        if worker_config.get("reply_fallback") is False:
            cmd.append("--no-reply-fallback")

    def _subprocess_env(self) -> dict[str, str]:
        env = os.environ.copy()
        package_root = str(Path(__file__).resolve().parents[1])
        current = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = package_root if not current else f"{package_root}{os.pathsep}{current}"
        return env

    def run_config_check(self, timeout: float = 20.0) -> dict[str, Any]:
        cmd = [*self._base_worker_command(), "--config-check"]
        self.append_log("system", "running config check")
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=self._subprocess_env(),
        )
        for line in completed.stdout.splitlines():
            self.append_log("check", line)
        for line in completed.stderr.splitlines():
            self.append_log("check:stderr", line)
        return {
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "ok": completed.returncode == 0,
        }

    def start_worker(self) -> dict[str, Any]:
        with self._lock:
            if self._process and self._process.poll() is None:
                return {"started": False, "reason": "already_running", **self.runtime()}
            placeholders = self.placeholder_groups()
            if placeholders:
                names = ", ".join(placeholders)
                raise ValueError(f"replace placeholder group IDs before start: {names}")
            cmd = [*self._base_worker_command(), "--print-command"]
            self._process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                env=self._subprocess_env(),
            )
            self.append_log("system", "worker start requested")
            self._start_pump(self._process.stdout, "stdout")
            self._start_pump(self._process.stderr, "stderr")
            return {"started": True, **self.runtime()}

    def _start_pump(self, pipe: Any, source: str) -> None:
        if pipe is None:
            return

        def pump() -> None:
            try:
                for line in pipe:
                    self.append_log(source, line)
            finally:
                with self._lock:
                    proc = self._process
                    if proc is not None and proc.poll() is not None:
                        self.append_log("system", f"worker exited with {proc.returncode}")

        thread = threading.Thread(target=pump, name=f"codex-avatar-ui-{source}", daemon=True)
        thread.start()

    def stop_worker(self) -> dict[str, Any]:
        with self._lock:
            proc = self._process
            if proc is None or proc.poll() is not None:
                return {"stopped": False, "reason": "not_running", **self.runtime()}
            self.append_log("system", "worker stop requested")
            proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.append_log("system", "worker did not stop in time; killing")
            proc.kill()
            proc.wait(timeout=5)
        return {"stopped": True, **self.runtime()}

    def runtime(self) -> dict[str, Any]:
        proc = self._process
        running = bool(proc and proc.poll() is None)
        return {
            "running": running,
            "pid": proc.pid if proc else None,
            "returncode": None if running or proc is None else proc.returncode,
        }

    def stats(self, config: dict[str, Any]) -> dict[str, Any]:
        groups = [item for item in config.get("groups", []) if isinstance(item, dict)]
        enabled = [item for item in groups if item.get("enabled") is not False]
        send_enabled = [item for item in enabled if item.get("send") is True]
        codex_execute = [item for item in enabled if item.get("codex_execute") is True]
        guarded = [item for item in enabled if item.get("mode") == "guarded"]
        autofix = [item for item in enabled if item.get("mode") == "autofix"]
        return {
            "groups": len(groups),
            "enabled": len(enabled),
            "send_enabled": len(send_enabled),
            "codex_execute": len(codex_execute),
            "guarded": len(guarded),
            "autofix": len(autofix),
            "placeholders": len(self.placeholder_groups(config)),
            "logs": len(self._logs),
        }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            config = self.load_config()
            command = self._base_worker_command(config)
            return {
                "config_path": str(self.config_path),
                "config": config,
                "worker_command": command,
                "runtime": self.runtime(),
                "stats": self.stats(config),
                "logs": [item.to_dict() for item in self._logs],
            }


class CodeXAvatarHandler(BaseHTTPRequestHandler):
    server_version = "CodeXAvatarUI/0.1"

    @property
    def state(self) -> DashboardState:
        return self.server.state  # type: ignore[attr-defined]

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send_static("index.html")
            return
        if parsed.path == "/api/state":
            self._send_json(self.state.snapshot())
            return
        if parsed.path == "/api/logs":
            self._send_json({"logs": self.state.snapshot()["logs"], "runtime": self.state.runtime()})
            return
        if parsed.path.startswith("/static/"):
            self._send_static(parsed.path.removeprefix("/static/"))
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/config":
                payload = self._read_json()
                config = payload.get("config")
                if not isinstance(config, dict):
                    raise ValueError("config must be an object")
                self.state.save_config(config)
                self._send_json({"ok": True, **self.state.snapshot()})
                return
            if parsed.path == "/api/config-check":
                query = parse_qs(parsed.query)
                timeout = float((query.get("timeout") or ["20"])[0])
                self._send_json(self.state.run_config_check(timeout=timeout))
                return
            if parsed.path == "/api/start":
                self._send_json(self.state.start_worker())
                return
            if parsed.path == "/api/stop":
                self._send_json(self.state.stop_worker())
                return
            self.send_error(HTTPStatus.NOT_FOUND)
        except Exception as e:
            self.state.append_log("error", str(e))
            self._send_json({"ok": False, "error": str(e)}, status=HTTPStatus.BAD_REQUEST)

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("content-length", "0"))
        raw = self.rfile.read(length).decode("utf-8")
        if not raw.strip():
            return {}
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("request body must be an object")
        return data

    def _send_json(self, payload: dict[str, Any], *, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_static(self, name: str) -> None:
        safe = name.strip("/") or "index.html"
        if ".." in Path(safe).parts:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            data = resources.files("codex_avatar").joinpath("static", safe).read_bytes()
        except FileNotFoundError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        ctype = mimetypes.guess_type(safe)[0] or "application/octet-stream"
        if safe.endswith(".js"):
            ctype = "text/javascript"
        self.send_response(HTTPStatus.OK)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class CodeXAvatarServer(ThreadingHTTPServer):
    def __init__(self, server_address: tuple[str, int], state: DashboardState) -> None:
        super().__init__(server_address, CodeXAvatarHandler)
        self.state = state


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the CodeX Avatar local web console")
    parser.add_argument("--config", default=str(worker.default_config_file()), help="config JSON path")
    parser.add_argument("--host", default="127.0.0.1", help="bind host")
    parser.add_argument("--port", type=int, default=8787, help="bind port")
    parser.add_argument("--open", action="store_true", help="open the dashboard in a browser")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    state = DashboardState(Path(args.config))
    server = CodeXAvatarServer((args.host, args.port), state)
    host, port = server.server_address[:2]
    url = f"http://{host}:{port}/"
    print(f"CodeX Avatar UI: {url}", flush=True)
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        state.stop_worker()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
