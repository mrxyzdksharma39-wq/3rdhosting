"""Trusted-code process hosting. This is NOT a security sandbox (Linux only)."""
from __future__ import annotations
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from collections import deque
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN_RE = re.compile(r"\d{5,15}:[A-Za-z0-9_-]{30,60}")
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_. -]{0,99}\Z")
KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
MAX_UPLOAD = 10 * 1024 * 1024
MAX_EXPANDED = 40 * 1024 * 1024
RESERVED = {"HOST_BOT_TOKEN", "OWNER_ID", "PORT", "DATA_DIR", "ALLOW_UNSANDBOXED_EXECUTION",
            "PATH", "HOME", "TMPDIR", "VIRTUAL_ENV", "LD_PRELOAD", "LD_LIBRARY_PATH"}

class HostingError(Exception):
    """Safe, user-facing error (never include raw exceptions or secrets)."""

@dataclass(frozen=True)
class Settings:
    token: str
    owner: int
    data_dir: Path
    port: int
    execution: bool

    @classmethod
    def from_env(cls):
        token = os.environ.get("HOST_BOT_TOKEN", "").strip()
        if not TOKEN_RE.fullmatch(token):
            raise HostingError("HOST_BOT_TOKEN is missing or malformed. No legacy BOT_TOKEN fallback is used.")
        try:
            owner = int(os.environ.get("OWNER_ID", "0"))
            port = int(os.environ.get("PORT", "10000"))
        except ValueError:
            raise HostingError("OWNER_ID and PORT must be integers.") from None
        if owner <= 0 or not 1 <= port <= 65535:
            raise HostingError("OWNER_ID must be positive and PORT must be 1..65535.")
        flag = os.environ.get("ALLOW_UNSANDBOXED_EXECUTION", "false").lower()
        if flag not in {"true", "false"}:
            raise HostingError("ALLOW_UNSANDBOXED_EXECUTION must be true or false.")
        return cls(token, owner, Path(os.environ.get("DATA_DIR", "user_files")).resolve(), port, flag == "true")

def redact(text, secrets=()):
    text = TOKEN_RE.sub("[REDACTED_TOKEN]", str(text))
    for secret in sorted(set(secrets), key=len, reverse=True):
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return text

def atomic_json(path, data):
    path = Path(path)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with open(tmp, "x", encoding="utf-8") as f:
            os.chmod(tmp, 0o600)
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)

def filename(value):
    if not isinstance(value, str) or not NAME_RE.fullmatch(value) or value in {".", ".."}:
        raise HostingError("Use a simple filename, at most 100 ASCII characters, without directory separators.")
    return value

def parse_env(path):
    """Small explicit dotenv subset: KEY=value, optional quotes. No expansion/escape processing."""
    result = {}
    if not path.is_file() or path.stat().st_size > 65536:
        raise HostingError("Project needs a .env file (maximum 64 KiB) with its own BOT_TOKEN.")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise HostingError("Project .env must be readable UTF-8.") from None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or not KEY_RE.fullmatch(key) or key in result:
            raise HostingError("Invalid or duplicate .env key. Use KEY=value lines.")
        if key in RESERVED or key.startswith(("PYTHON", "PIP_", "LD_", "DYLD_")):
            raise HostingError("Project .env contains a reserved host/runtime key.")
        if value.startswith(("'", '"')):
            if len(value) < 2 or value[-1] != value[0]:
                raise HostingError("Unclosed .env quote.")
            value = value[1:-1]
        if "\x00" in value:
            raise HostingError("NUL bytes are not allowed in .env.")
        result[key] = value
    return result

def extract_zip(data, dest):
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            members = z.infolist()
            if not members or len(members) > 1000 or sum(i.file_size for i in members) > MAX_EXPANDED:
                raise HostingError("ZIP empty or exceeds 1000 entries / 40 MiB expanded limit.")
            seen = set()
            for info in members:
                name = info.filename
                p = PurePosixPath(name)
                mode = info.external_attr >> 16
                if (not name or "\\" in name or "\x00" in name or p.is_absolute()
                    or any(part in {"..", ".venv", ".runtime", ".git"} for part in p.parts)
                    or any(":" in part for part in p.parts)
                    or (stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR})
                    or info.flag_bits & 1):
                    raise HostingError("ZIP contains unsafe paths, special files, or encrypted entries.")
                normalized = str(p).casefold()
                if normalized in seen:
                    raise HostingError("ZIP contains duplicate paths.")
                seen.add(normalized)
                if info.file_size > 1024 * 1024 and info.file_size / max(info.compress_size, 1) > 200:
                    raise HostingError("ZIP compression ratio exceeds limit.")
                target = dest.joinpath(*p.parts)
                if not target.resolve().is_relative_to(dest.resolve()):
                    raise HostingError("Unsafe ZIP path.")
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True, mode=0o700)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    with z.open(info) as src, open(target, "xb") as dst:
                        shutil.copyfileobj(src, dst)
                    target.chmod(0o600)
    except HostingError:
        raise
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile, NotImplementedError):
        raise HostingError("Invalid, corrupt, or unsupported ZIP project.") from None

def detect_entry(folder, preferred=None):
    if preferred:
        p = folder / preferred
        if p.is_file() and p.suffix == ".py":
            return preferred
    candidates = [x for x in ("bot.py", "main.py") if (folder / x).is_file()]
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        raise HostingError("Both bot.py and main.py exist. Keep one entry point at project root.")
    candidates = list(folder.glob("*.py"))
    if len(candidates) == 1:
        return candidates[0].name
    raise HostingError("Missing or ambiguous entry point. Put a single bot.py or main.py at project root.")

class Manager:
    def __init__(self, settings):
        self.settings = settings
        self.root = settings.data_dir
        self.lock = threading.RLock()
        self.running = {}
        self.jobs = {}
        self.logs = {}
        self.secrets = {}
        self.closed = False
        self.instance_lock = None
        try:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.root.chmod(0o700)
            self.projects_dir = self.root / "projects"
            self.projects_dir.mkdir(exist_ok=True, mode=0o700)
            self.db = self.root / "state.json"
            if self.db.exists():
                state = json.loads(self.db.read_text(encoding="utf-8"))
                if not isinstance(state, dict) or state.get("version") != 1:
                    raise ValueError()
                self.users = state["users"]
                self.projects = state["projects"]
                if not isinstance(self.users, dict) or not isinstance(self.projects, dict):
                    raise ValueError()
                self.validate_state()
            else:
                self.users, self.projects = {}, {}
                self.save()
            # Actually exercise write/rename permissions even when state already exists.
            probe = self.root / ("probe-" + uuid.uuid4().hex)
            atomic_json(probe, {})
            probe.unlink()
        except (OSError, ValueError, KeyError, TypeError):
            raise HostingError("Storage is unreadable, unwritable, or state.json is invalid. Restore a backup; data was not reset.") from None

    def validate_state(self):
        for uid, user in self.users.items():
            if not uid.isdigit() or not isinstance(user, dict) or not isinstance(user.get("files"), list):
                raise ValueError()
            if not isinstance(user.get("approved"), bool) or not isinstance(user.get("rejected"), bool):
                raise ValueError()
        for pid, p in self.projects.items():
            if not re.fullmatch(r"[a-f0-9]{12}", pid) or not isinstance(p, dict):
                raise ValueError()
            if p["owner"] not in self.users or pid not in self.users[p["owner"]]["files"]:
                raise ValueError()
            filename(p["name"])
            rel = Path(p["root"])
            if rel.is_absolute() or ".." in rel.parts or not rel.parts or rel.parts[0] != pid:
                raise ValueError()
            filename(p["entry"])
        for uid, u in self.users.items():
            if any(pid not in self.projects or self.projects[pid]["owner"] != uid for pid in u["files"]):
                raise ValueError()

    def save(self):
        with self.lock:
            atomic_json(self.db, {"version": 1, "users": self.users, "projects": self.projects})

    def ensure_user(self, uid):
        with self.lock:
            key = str(int(uid))
            if int(uid) <= 0:
                raise HostingError("Invalid user.")
            if key not in self.users:
                self.users[key] = {"files": [], "approved": int(uid) == self.settings.owner, "rejected": False}
                self.save()
            return self.users[key]

    def approved(self, uid):
        return int(uid) == self.settings.owner or self.ensure_user(uid)["approved"]

    def authorize(self, actor, target, approved):
        if int(actor) != self.settings.owner:
            raise HostingError("Owner only.")
        if int(target) == self.settings.owner:
            raise HostingError("Owner access cannot be revoked.")
        with self.lock:
            user = self.ensure_user(target)
            user.update(approved=approved, rejected=not approved)
            if not approved:
                for pid in list(user["files"]):
                    self.stop(target, pid, bypass=True)
            self.save()

    def project(self, uid, pid):
        if not self.approved(uid):
            raise HostingError("Owner approval required.")
        p = self.projects.get(pid)
        if not p or p["owner"] != str(uid):
            raise HostingError("Project not found for this user.")
        return p

    def folder(self, p):
        path = (self.projects_dir / p["root"]).resolve()
        if not path.is_relative_to(self.projects_dir.resolve()) or not path.is_dir():
            raise HostingError("Project directory is missing or invalid.")
        return path

    def upload(self, uid, name, data):
        filename(name)
        if not isinstance(data, bytes) or not 0 < len(data) <= MAX_UPLOAD:
            raise HostingError("Upload must be nonempty and at most 10 MiB.")
        if Path(name).suffix.lower() not in {".py", ".zip"}:
            raise HostingError("Upload .py or .zip. Send .env/requirements.txt from the selected project's menu.")
        with self.lock:
            if not self.approved(uid):
                raise HostingError("Owner approval required.")
            u = self.ensure_user(uid)
            if int(uid) != self.settings.owner and len(u["files"]) >= 2:
                raise HostingError("Limit: 2 projects. Use a project configuration button to update its settings.")
            pid = uuid.uuid4().hex[:12]
            base = self.projects_dir / pid
            base.mkdir(mode=0o700)
            content = base / "source"
            content.mkdir(mode=0o700)
            try:
                if name.lower().endswith(".zip"):
                    extract_zip(data, content)
                    entries = list(content.iterdir())
                    if len(entries) == 1 and entries[0].is_dir():
                        content = entries[0]
                    entry = detect_entry(content)
                else:
                    (content / name).write_bytes(data)
                    (content / name).chmod(0o600)
                    entry = detect_entry(content, name)
                p = {"owner": str(uid), "name": name, "root": str(content.relative_to(self.projects_dir)),
                     "entry": entry, "desired": False}
                self.projects[pid] = p
                u["files"].append(pid)
                try:
                    self.save()
                except Exception:
                    u["files"].remove(pid)
                    self.projects.pop(pid)
                    raise
                return pid
            except Exception:
                shutil.rmtree(base, ignore_errors=True)
                raise

    def configure(self, uid, pid, name, data):
        if name not in {".env", "requirements.txt"} or not 0 < len(data) <= 65536:
            raise HostingError("Upload exactly .env or requirements.txt (maximum 64 KiB).")
        with self.lock:
            p = self.project(uid, pid)
            if pid in self.running or pid in self.jobs:
                raise HostingError("Stop the project and finish installation before changing its configuration.")
            folder = self.folder(p)
            tmp = folder / ("config-" + uuid.uuid4().hex)
            try:
                tmp.write_bytes(data)
                tmp.chmod(0o600)
                if name == ".env":
                    config = parse_env(tmp)
                    self.validate_token(config)
                else:
                    data.decode("utf-8")
                os.replace(tmp, folder / name)
            except UnicodeError:
                raise HostingError("Configuration must be UTF-8.") from None
            finally:
                tmp.unlink(missing_ok=True)

    def validate_token(self, config):
        token = config.get("BOT_TOKEN", "")
        if not TOKEN_RE.fullmatch(token):
            raise HostingError("Set a valid-shaped BOT_TOKEN in this project's .env. Hard-coded-only tokens require the same explicit .env declaration.")
        if token.split(":", 1)[0] == self.settings.token.split(":", 1)[0] or self.settings.token in config.values():
            raise HostingError("A project cannot use the hosting bot account or token.")
        return token

    def base_env(self, folder):
        runtime = folder / ".runtime"
        runtime.mkdir(exist_ok=True, mode=0o700)
        return {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(runtime), "TMPDIR": str(runtime),
                "LANG": "C.UTF-8", "PYTHONUNBUFFERED": "1", "PYTHONNOUSERSITE": "1"}

    def child_env(self, folder):
        config = parse_env(folder / ".env")
        self.validate_token(config)
        result = self.base_env(folder)
        result.update(config)
        pid = folder.relative_to(self.projects_dir).parts[0]
        self.secrets.setdefault(pid, set()).update(config.values())
        result["VIRTUAL_ENV"] = str(self.projects_dir / pid / ".venv")
        result["PATH"] = str(self.projects_dir / pid / ".venv" / "bin") + ":" + result["PATH"]
        return result

    def python(self, pid):
        return self.projects_dir / pid / ".venv" / "bin" / "python"

    def safe_log(self, pid, text):
        secrets = [self.settings.token, *self.secrets.get(pid, ())]
        try:
            secrets.extend(parse_env(self.folder(self.projects[pid]) / ".env").values())
        except (HostingError, OSError):
            pass
        self.logs.setdefault(pid, deque(maxlen=100)).append(redact(text, secrets)[:2000])

    def log_text(self, uid, pid):
        self.project(uid, pid)
        with self.lock:
            return "\n".join(self.logs.get(pid, []))[-3000:] or "No logs in this process lifetime."

    def require_execution(self):
        if self.closed:
            raise HostingError("Host is shutting down.")
        if not self.settings.execution:
            raise HostingError("Execution disabled. Owner must acknowledge trusted-code risk with ALLOW_UNSANDBOXED_EXECUTION=true.")

    def install(self, uid, pid, packages=()):
        with self.lock:
            self.require_execution()
            p = self.project(uid, pid)
            if pid in self.running or pid in self.jobs:
                raise HostingError("Project is running or an installation is already active.")
            if self.jobs:
                raise HostingError("Another installation is active. Retry after it finishes.")
            if len(packages) > 30 or any(not re.fullmatch(r"[A-Za-z0-9_.-]+", x) for x in packages):
                raise HostingError("Invalid package selection.")
            self.jobs[pid] = None
        try:
            folder = self.folder(p)
            python = self.python(pid)
            if not python.exists():
                # ensurepip is local; never runs a network installation globally.
                result = self.run_install(pid, [sys.executable, "-I", "-m", "venv", str(python.parent.parent)], folder)
                if result != 0:
                    raise HostingError("Virtual environment creation failed. Inspect Logs and available disk space.")
            args = [str(python), "-I", "-m", "pip", "--isolated", "--disable-pip-version-check", "install", "--no-input"]
            if packages:
                args += list(packages)
            elif (folder / "requirements.txt").is_file():
                args += ["-r", "requirements.txt"]
            else:
                self.safe_log(pid, "Created isolated virtual environment; no requirements.txt present.")
                return "Environment ready; no requirements.txt to install."
            result = self.run_install(pid, args, folder)
            if result != 0:
                raise HostingError("Dependency installation failed. Inspect Logs; fix requirements.txt and retry.")
            result = self.run_install(pid, [str(python), "-I", "-m", "pip", "--isolated", "check"], folder)
            if result != 0:
                raise HostingError("Dependency compatibility check failed. Inspect Logs.")
            return "Dependencies installed and pip check passed in this project's environment."
        finally:
            with self.lock:
                self.jobs.pop(pid, None)

    def run_install(self, pid, args, folder):
        with self.lock:
            if self.closed:
                raise HostingError("Host is shutting down.")
            proc = subprocess.Popen(args, cwd=folder, env=self.base_env(folder), stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, start_new_session=True)
            self.jobs[pid] = proc
        reader = threading.Thread(target=self.capture, args=(pid, proc), daemon=True)
        reader.start()
        try:
            return proc.wait(timeout=300)
        except subprocess.TimeoutExpired:
            self.kill_group(proc)
            raise HostingError("Installation timed out after 300 seconds.") from None
        finally:
            self.kill_group(proc)
            reader.join(timeout=2)

    def capture(self, pid, proc):
        # Bounded reads: a user cannot allocate unlimited RAM with one enormous line.
        try:
            while True:
                chunk = proc.stdout.readline(4096)
                if not chunk:
                    break
                text = chunk.decode("utf-8", errors="replace")
                # A partial overlong line might split a secret across chunks; suppress it.
                if not chunk.endswith(b"\n") and len(chunk) == 4096:
                    while chunk and not chunk.endswith(b"\n"):
                        chunk = proc.stdout.readline(4096)
                    text = "[overlong log line suppressed]"
                with self.lock:
                    self.safe_log(pid, text.rstrip())
        finally:
            proc.stdout.close()

    @staticmethod
    def kill_group(proc):
        # start_new_session gives the project its own process group, including normal children.
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=2)

    def start(self, uid, pid):
        with self.lock:
            self.require_execution()
            p = self.project(uid, pid)
            if pid in self.running or pid in self.jobs:
                raise HostingError("Project is already running or installing.")
            folder = self.folder(p)
            env = self.child_env(folder)
            bot_id = env["BOT_TOKEN"].split(":", 1)[0]
            if any(item[1] == bot_id for item in self.running.values()):
                raise HostingError("This Telegram account already has a running project on this host.")
            python = self.python(pid)
            if not python.is_file():
                raise HostingError("Install dependencies first to create this project's isolated environment.")
            entry = folder / p["entry"]
            if not entry.is_file() or not entry.resolve().is_relative_to(folder):
                raise HostingError("Project entry point is missing or unsafe.")
            proc = subprocess.Popen([str(python), "-u", str(entry)], cwd=folder, env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
            self.running[pid] = (proc, bot_id)
            p["desired"] = True
            try:
                self.save()
            except Exception:
                self.kill_group(proc)
                self.running.pop(pid, None)
                raise
            threading.Thread(target=self.watch, args=(pid, proc), daemon=True).start()
            return "Process launched. Check Status and Logs; launch alone does not confirm Telegram connectivity."

    def watch(self, pid, proc):
        reader = threading.Thread(target=self.capture, args=(pid, proc), daemon=True)
        reader.start()
        # Do not wait for EOF before noticing parent exit: a child can hold stdout open.
        code = proc.wait()
        self.kill_group(proc)
        reader.join(timeout=2)
        with self.lock:
            if self.running.get(pid, (None,))[0] is proc:
                self.running.pop(pid, None)
                self.safe_log(pid, f"Process exited with code {code}.")

    def stop(self, uid, pid, bypass=False, preserve=False):
        with self.lock:
            p = self.projects[pid] if bypass else self.project(uid, pid)
            if not preserve:
                p["desired"] = False
                self.save()
            item = self.running.get(pid)
            job = self.jobs.get(pid)
            if job is not None:
                self.kill_group(job)
            if item:
                self.kill_group(item[0])
                if self.running.get(pid) is item:
                    self.running.pop(pid, None)
            return "Stopped project and ordinary child processes."

    def restart(self, uid, pid):
        with self.lock:
            self.stop(uid, pid)
            return self.start(uid, pid)

    def status(self, uid, pid):
        self.project(uid, pid)
        with self.lock:
            if pid in self.jobs:
                return "Installing"
            item = self.running.get(pid)
            return "Running" if item and item[0].poll() is None else "Stopped"

    def restore(self):
        results = []
        for pid, p in list(self.projects.items()):
            if p.get("desired") and self.approved(p["owner"]):
                try:
                    self.start(p["owner"], pid)
                    results.append((pid, "launched"))
                except (HostingError, OSError):
                    self.safe_log(pid, "Restore failed. Recheck configuration and dependencies; manual start required.")
                    results.append((pid, "not launched"))
        return results

    def acquire_instance_lock(self):
        import fcntl
        self.instance_lock = open(self.root / "host.lock", "a")
        try:
            fcntl.flock(self.instance_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.instance_lock.close()
            self.instance_lock = None
            raise HostingError("Another host instance is already using this data directory.") from None

    def close(self):
        with self.lock:
            self.closed = True
            for pid, p in list(self.projects.items()):
                self.stop(p["owner"], pid, bypass=True, preserve=True)
            if self.instance_lock:
                self.instance_lock.close()
                self.instance_lock = None

def health_server(port, stopping):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            ok = not stopping.is_set()
            self.send_response((200 if ok else 503) if self.path == "/healthz" else 404)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ok\n" if ok else b"stopping\n")
        def log_message(self, *args):
            pass
    class HealthServer(ThreadingHTTPServer):
        def server_bind(self):
            # Avoid HTTPServer's unnecessary reverse-DNS lookup: container hostnames
            # can be invalid DNS labels, and health serving must not depend on DNS.
            import socketserver
            socketserver.TCPServer.server_bind(self)
            self.server_name = "health"
            self.server_port = self.server_address[1]
    server = HealthServer(("0.0.0.0", port), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
