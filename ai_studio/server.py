"""
ManimGL AI Studio: a local web page for chatting with DeepSeek / OpenAI / Claude to write ManimGL
scripts, then editing and running them in place.

    venv\\python.exe ai_studio\\server.py [--port 8765] [--no-browser]

Listens on 127.0.0.1 only. Every /api request must carry the token handed to the page when it is
served, so another website open in the browser cannot reach the endpoints that run code.
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

import api_reference  # noqa: E402
import prompts  # noqa: E402
from providers import ProviderError, stream_chat  # noqa: E402

STUDIO_DIR = Path(__file__).resolve().parent
PROJECT_DIR = STUDIO_DIR.parent
STATIC_DIR = STUDIO_DIR / "static"
DATA_DIR = STUDIO_DIR / "data"
CONV_DIR = DATA_DIR / "conversations"
UPLOAD_DIR = DATA_DIR / "uploads"
WORKSPACE_DIR = STUDIO_DIR / "workspace"
CONFIG_PATH = DATA_DIR / "config.json"
PYTHON = PROJECT_DIR / "venv" / "python.exe"
if not PYTHON.exists():
    PYTHON = Path(sys.executable)

DEFAULT_PORT = 8765
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_LOG_LINES = 4000
TOKEN = secrets.token_urlsafe(24)
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
SCENE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
IMAGE_TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp"}

DEFAULT_PROVIDERS = [
    {
        # deepseek-flash is DeepSeek-V4.1-Flash: 1M context, image input, thinking on by default
        "id": "deepseek", "name": "DeepSeek", "type": "openai",
        "base_url": "https://api.deepseek.com", "model": "deepseek-flash",
        "models": ["deepseek-flash", "deepseek-v4-pro"],
        "vision": True, "max_tokens": 65536, "reasoning": "deepseek",
        "env": "DEEPSEEK_API_KEY", "api_key": "",
    },
    {
        "id": "openai", "name": "OpenAI", "type": "openai",
        "base_url": "https://api.openai.com/v1", "model": "gpt-4o",
        "models": ["gpt-4o", "gpt-4.1", "o3"],
        "vision": True, "max_tokens": 0, "reasoning": "none",
        "env": "OPENAI_API_KEY", "api_key": "",
    },
    {
        "id": "claude", "name": "Claude", "type": "anthropic",
        "base_url": "", "model": "claude-opus-5-5",
        "models": ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-1"],
        "vision": True, "max_tokens": 64000, "reasoning": "claude",
        "env": "ANTHROPIC_API_KEY", "api_key": "",
    },
]
# DeepSeek names this app once defaulted to, which do not select the current Flash model
LEGACY_DEEPSEEK_MODELS = {"deepseek-chat", "deepseek-reasoner"}
# Names that mean the Flash model but are not its API id, compared lowercased: the old
# defaults, and the display name DeepSeek's docs give it
DEEPSEEK_FLASH_ALIASES = LEGACY_DEEPSEEK_MODELS | {"deepseek-v4.1-flash"}
# DeepSeek's off-peak USD per 1M tokens: (input cache hit, input cache miss, output), from
# api-docs.deepseek.com/quick_start/pricing. Peak hours cost double. The retired Flash names
# are served, and billed, as deepseek-flash.
DEEPSEEK_PRICES = {
    "deepseek-flash": (0.003, 0.15, 0.6),
    "deepseek-v4-flash": (0.003, 0.15, 0.6),
    "deepseek-v4-flash-vision-exp": (0.003, 0.15, 0.6),
    "deepseek-v4-pro": (0.022, 0.66, 1.98),
}


def deepseek_peak(now: float | None = None) -> bool:
    """01:00-04:00 and 06:00-10:00 UTC on weekdays (Chinese public holidays, off-peak, not known here)"""
    t = time.gmtime(now)
    return t.tm_wday < 5 and (1 <= t.tm_hour < 4 or 6 <= t.tm_hour < 10)


def price_usage(provider: dict, model: str, usage: dict) -> dict:
    """Adds an estimated cost, and what the cache hits saved, where the prices are known"""
    prices = DEEPSEEK_PRICES.get(model.lower()) if "deepseek.com" in (provider.get("base_url") or "") else None
    if not prices:
        return usage
    peak = deepseek_peak()
    hit, miss, out = (p * (2 if peak else 1) / 1e6 for p in prices)
    usage = dict(usage)
    usage["peak"] = peak
    usage["cost_usd"] = usage["cache_hit"] * hit + usage["cache_miss"] * miss + usage["output"] * out
    usage["saved_usd"] = usage["cache_hit"] * (miss - hit)
    return usage


# DeepSeek takes low / high / max and serves "medium" as high; Claude has all four
EFFORTS = ("low", "medium", "high", "max")

lock = threading.RLock()
jobs: dict[str, dict] = {}


# Storage

def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def load_config() -> dict:
    with lock:
        if CONFIG_PATH.exists():
            config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if migrate_config(config):
                write_json(CONFIG_PATH, config)
        else:
            config = {"providers": [dict(p) for p in DEFAULT_PROVIDERS], "default_provider": "deepseek"}
            write_json(CONFIG_PATH, config)
        return config


def default_reasoning(p: dict) -> str:
    if p.get("type") == "anthropic":
        return "claude"
    return "deepseek" if "deepseek.com" in (p.get("base_url") or "") else "none"


def migrate_config(config: dict) -> bool:
    """
    Brings a config saved by an earlier version up to date, keeping its keys: the DeepSeek
    entry moves off the legacy model names onto deepseek-flash, and every provider gets a
    reasoning style. Returns whether anything changed.
    """
    changed = False
    defaults = {p["id"]: p for p in DEFAULT_PROVIDERS}
    for p in config.get("providers", []):
        if "reasoning" not in p:
            p["reasoning"] = default_reasoning(p)
            changed = True
        if p.get("id") != "deepseek":
            continue
        d = defaults["deepseek"]
        if p.get("model") in LEGACY_DEEPSEEK_MODELS:
            # still on the old defaults throughout: take the new ones
            p.update(vision=d["vision"], max_tokens=d["max_tokens"])
        if (p.get("model") or "").lower() in DEEPSEEK_FLASH_ALIASES:
            p["model"] = d["model"]
            changed = True
        models = p.get("models") or []
        if not models or all(m.lower() in DEEPSEEK_FLASH_ALIASES for m in models):
            p["models"] = list(d["models"])
            changed = True
    return changed


def normalize_conv(conv: dict) -> dict:
    """Conversations started on a legacy DeepSeek name continue on deepseek-flash"""
    if (conv.get("model") or "").lower() in DEEPSEEK_FLASH_ALIASES:
        conv["model"] = "deepseek-flash"
    conv.setdefault("thinking", True)
    if conv.get("effort") not in EFFORTS:
        conv["effort"] = "high"
    return conv


def public_config(config: dict) -> dict:
    """The config as the page sees it: keys replaced by whether one is set and its last digits"""
    providers = []
    for p in config["providers"]:
        q = {k: v for k, v in p.items() if k != "api_key"}
        key = p.get("api_key", "")
        q["has_key"] = bool(key) or bool(os.environ.get(p.get("env") or ""))
        q["key_hint"] = f"****{key[-4:]}" if key else (f"环境变量 {p['env']}" if q["has_key"] else "")
        providers.append(q)
    return {
        "providers": providers,
        "default_provider": config.get("default_provider"),
        "skill": prompts.skill_info(),
    }


def save_config(incoming: dict) -> dict:
    with lock:
        old = {p["id"]: p for p in load_config()["providers"]}
        providers = []
        for p in incoming.get("providers", []):
            pid = str(p.get("id") or "").strip() or uuid.uuid4().hex[:8]
            if not ID_RE.match(pid):
                raise ValueError(f"无效的提供商 id：{pid}")
            prev = old.get(pid, {})
            key = p.get("api_key")
            if key == "__clear__":
                key = ""
            elif not key:
                key = prev.get("api_key", "")
            providers.append({
                "id": pid,
                "name": str(p.get("name") or pid),
                "type": "anthropic" if p.get("type") == "anthropic" else "openai",
                "base_url": str(p.get("base_url") or ""),
                "model": str(p.get("model") or ""),
                "models": [str(m) for m in p.get("models") or prev.get("models") or []],
                "vision": bool(p.get("vision")),
                "max_tokens": int(p.get("max_tokens") or 0),
                "reasoning": "claude" if p.get("type") == "anthropic"
                else ("deepseek" if p.get("reasoning") == "deepseek" else "none"),
                "env": str(p.get("env") or prev.get("env") or ""),
                "api_key": key.strip(),
            })
        if not providers:
            raise ValueError("至少需要保留一个模型提供商")
        default = incoming.get("default_provider")
        if default not in {p["id"] for p in providers}:
            default = providers[0]["id"]
        config = {"providers": providers, "default_provider": default}
        write_json(CONFIG_PATH, config)
        return config


def conv_path(conv_id: str) -> Path:
    if not ID_RE.match(conv_id):
        raise KeyError(conv_id)
    return CONV_DIR / f"{conv_id}.json"


def load_conv(conv_id: str) -> dict:
    path = conv_path(conv_id)
    if not path.exists():
        raise KeyError(conv_id)
    return normalize_conv(json.loads(path.read_text(encoding="utf-8")))


def save_conv(conv: dict) -> None:
    conv["updated"] = time.time()
    write_json(conv_path(conv["id"]), conv)


def list_convs() -> list[dict]:
    convs = []
    for path in CONV_DIR.glob("*.json"):
        try:
            c = normalize_conv(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
        convs.append({k: c.get(k) for k in ("id", "title", "created", "updated", "provider_id", "model")})
    return sorted(convs, key=lambda c: c.get("updated") or 0, reverse=True)


def new_conv(provider_id: str | None, model: str | None, use_skill: bool,
             thinking: bool = True, effort: str = "high") -> dict:
    config = load_config()
    providers = {p["id"]: p for p in config["providers"]}
    provider_id = provider_id if provider_id in providers else config["default_provider"]
    conv = {
        "id": time.strftime("%Y%m%d%H%M%S") + "_" + uuid.uuid4().hex[:6],
        "title": "新对话",
        "created": time.time(),
        "provider_id": provider_id,
        "model": model or providers[provider_id]["model"],
        "use_skill": use_skill,
        "thinking": thinking,
        "effort": effort if effort in EFFORTS else "high",
        "messages": [],
    }
    with lock:
        save_conv(conv)
    return conv


# Running scripts

def kill_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )


def start_run(conv_id: str, code: str, scene: str, mode: str, quality: str) -> dict:
    if not SCENE_RE.match(scene):
        raise ValueError("场景类名无效")
    load_conv(conv_id)  # must exist
    work = WORKSPACE_DIR / conv_id
    video_dir = work / "videos"
    video_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    script = work / f"scene_{stamp}.py"
    script.write_text(code, encoding="utf-8")

    args = [str(PYTHON), "-m", "manimlib", str(script), scene]
    output = None
    if mode == "render":
        output = f"{scene}_{stamp}.mp4"
        args += ["-w", "--video_dir", str(video_dir), "--file_name", Path(output).stem]
        args += {"l": ["-l"], "m": ["-m"], "hd": ["--hd"]}.get(quality, ["-l"])
    elif mode == "check":
        # Runs all of construct with animations skipped and saves only the last frame: a few
        # seconds to find out whether the script works, with no window
        output = f"{scene}_{stamp}_check.png"
        args += ["-w", "-s", "-l", "--video_dir", str(video_dir), "--file_name", Path(output).stem]
    env = dict(os.environ)
    env.update({
        "PYTHONNOUSERSITE": "1", "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8",
        "NO_COLOR": "1", "TERM": "dumb", "COLUMNS": "160",
    })
    proc = subprocess.Popen(
        args, cwd=work, env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    job_id = uuid.uuid4().hex[:12]
    job = {
        "id": job_id, "conv_id": conv_id, "scene": scene, "mode": mode,
        "script": script.name, "status": "running", "log": [], "video": None,
        "returncode": None, "started": time.time(), "proc": proc, "video_dir": video_dir,
        "output": output,
    }
    with lock:
        jobs[job_id] = job
    threading.Thread(target=watch_run, args=(job,), daemon=True).start()
    return public_job(job)


def watch_run(job: dict) -> None:
    proc: subprocess.Popen = job["proc"]
    assert proc.stdout is not None
    for raw in proc.stdout:
        line = ANSI_RE.sub("", raw.decode("utf-8", errors="replace")).rstrip("\r\n")
        with lock:
            job["log"].append(line)
            if len(job["log"]) > MAX_LOG_LINES:
                del job["log"][: len(job["log"]) - MAX_LOG_LINES]
    proc.wait()
    # Only this run's own file, and only if the run succeeded
    output = job["output"]
    video = output if output and proc.returncode == 0 and (job["video_dir"] / output).is_file() else None
    hint = ""
    if proc.returncode != 0 and job["status"] == "running":
        # The real signatures of the classes in the traceback, for the fix request
        try:
            hint = api_reference.lookup_for_error("\n".join(job["log"]))
        except Exception as e:  # a reference that fails to build must not hide the run's result
            hint = f"(API 签名查询失败: {e})"
    with lock:
        job["returncode"] = proc.returncode
        job["video"] = video
        job["api_hint"] = hint
        if job["status"] == "running":
            job["status"] = "done" if proc.returncode == 0 else "failed"


def public_job(job: dict) -> dict:
    out = {k: v for k, v in job.items() if k not in ("proc", "video_dir", "output")}
    out["log"] = "\n".join(job["log"])
    if job["video"]:
        key = "image_url" if job["video"].endswith(".png") else "video_url"
        out[key] = f"/videos/{job['conv_id']}/{job['video']}"
    return out


# HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "ManimAIStudio/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if "/api/run/" in (args[0] if args else ""):
            return  # the page polls running jobs twice a second
        sys.stderr.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    # helpers

    def send_json(self, data, status=HTTPStatus.OK):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, status, message):
        self.send_json({"error": message}, status)

    def read_body(self, limit=MAX_UPLOAD_BYTES) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length > limit:
            raise ValueError("请求体过大")
        return self.rfile.read(length) if length else b""

    def read_json(self) -> dict:
        body = self.read_body(limit=5 * 1024 * 1024)
        return json.loads(body.decode("utf-8")) if body else {}

    def send_file(self, path: Path, root: Path, extra_headers=None):
        try:
            path = path.resolve()
            if not path.is_relative_to(root.resolve()) or not path.is_file():
                raise FileNotFoundError
        except (OSError, FileNotFoundError):
            return self.send_error_json(HTTPStatus.NOT_FOUND, "not found")
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        size = path.stat().st_size
        start, end = 0, size - 1
        status = HTTPStatus.OK
        rng = self.headers.get("Range")
        if rng and (m := re.match(r"bytes=(\d*)-(\d*)", rng)) and size:
            if m.group(1):
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else size - 1
            elif m.group(2):
                start = max(0, size - int(m.group(2)))
            end = min(end, size - 1)
            status = HTTPStatus.PARTIAL_CONTENT
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk = f.read(min(1 << 16, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost")

    def api_ok(self) -> bool:
        return secrets.compare_digest(self.headers.get("X-Studio-Token") or "", TOKEN)

    # routing

    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def do_PUT(self):
        self.route("PUT")

    def do_DELETE(self):
        self.route("DELETE")

    def route(self, method: str):
        if not self.host_ok():
            return self.send_error_json(HTTPStatus.FORBIDDEN, "forbidden host")
        path = unquote(urlparse(self.path).path)
        try:
            if method == "GET" and path in ("/", "/index.html"):
                return self.serve_index()
            if method == "GET" and path.startswith("/static/"):
                # revalidate every time, so a restarted server's new page code is picked up
                return self.send_file(STATIC_DIR / path[len("/static/"):], STATIC_DIR,
                                      {"Cache-Control": "no-cache"})
            if method == "GET" and path.startswith("/uploads/"):
                return self.send_file(UPLOAD_DIR / path[len("/uploads/"):], UPLOAD_DIR)
            if method == "GET" and path.startswith("/videos/"):
                parts = path.split("/")
                if len(parts) == 4 and ID_RE.match(parts[2]):
                    return self.send_file(WORKSPACE_DIR / parts[2] / "videos" / parts[3], WORKSPACE_DIR)
                return self.send_error_json(HTTPStatus.NOT_FOUND, "not found")
            if method == "GET" and path == "/api/ping":
                return self.send_json({"ok": True, "app": "manim-ai-studio"})
            if path.startswith("/api/"):
                if not self.api_ok():
                    return self.send_error_json(HTTPStatus.FORBIDDEN, "missing or wrong studio token")
                return self.route_api(method, path)
            return self.send_error_json(HTTPStatus.NOT_FOUND, "not found")
        except KeyError:
            return self.send_error_json(HTTPStatus.NOT_FOUND, "对话不存在")
        except ValueError as e:
            return self.send_error_json(HTTPStatus.BAD_REQUEST, str(e))
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return
        except Exception as e:  # keep the server up, tell the page what broke
            self.log_error("error: %r", e)
            try:
                return self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, f"{type(e).__name__}: {e}")
            except OSError:
                return

    def route_api(self, method: str, path: str):
        parts = path.strip("/").split("/")[1:]  # after "api"
        if parts == ["config"]:
            if method == "GET":
                return self.send_json(public_config(load_config()))
            if method == "PUT":
                return self.send_json(public_config(save_config(self.read_json())))
        if parts == ["conversations"]:
            if method == "GET":
                return self.send_json(list_convs())
            if method == "POST":
                body = self.read_json()
                return self.send_json(new_conv(
                    body.get("provider_id"), body.get("model"), bool(body.get("use_skill", True)),
                    bool(body.get("thinking", True)), str(body.get("effort") or "high"),
                ))
        if len(parts) == 2 and parts[0] == "conversations":
            conv_id = parts[1]
            if method == "GET":
                return self.send_json(load_conv(conv_id))
            if method == "PUT":
                body = self.read_json()
                with lock:
                    conv = load_conv(conv_id)
                    for key in ("title", "provider_id", "model"):
                        if isinstance(body.get(key), str) and body[key].strip():
                            conv[key] = body[key].strip()[:200]
                    if "use_skill" in body:
                        conv["use_skill"] = bool(body["use_skill"])
                    if "thinking" in body:
                        conv["thinking"] = bool(body["thinking"])
                    if body.get("effort") in EFFORTS:
                        conv["effort"] = body["effort"]
                    save_conv(conv)
                return self.send_json(conv)
            if method == "DELETE":
                with lock:
                    conv = load_conv(conv_id)
                    for msg in conv["messages"]:
                        for name in msg.get("images") or []:
                            (UPLOAD_DIR / Path(name).name).unlink(missing_ok=True)
                    conv_path(conv_id).unlink()
                return self.send_json({"ok": True})
        if len(parts) == 4 and parts[0] == "conversations" and parts[2] == "messages" and method == "PUT":
            # Saving an edit made to a code block in place: the page sends the whole new text
            body = self.read_json()
            with lock:
                conv = load_conv(parts[1])
                index = int(parts[3])
                if not 0 <= index < len(conv["messages"]):
                    raise ValueError("消息不存在")
                conv["messages"][index]["content"] = str(body.get("content", ""))
                save_conv(conv)
            return self.send_json({"ok": True})
        if parts == ["upload"] and method == "POST":
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip()
            if ctype not in IMAGE_TYPES:
                raise ValueError("只支持 PNG / JPEG / GIF / WebP 图片")
            data = self.read_body()
            if not data:
                raise ValueError("空文件")
            UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
            name = uuid.uuid4().hex + IMAGE_TYPES[ctype]
            (UPLOAD_DIR / name).write_bytes(data)
            return self.send_json({"name": name, "url": f"/uploads/{name}"})
        if parts == ["chat"] and method == "POST":
            return self.chat(self.read_json())
        if parts == ["run"] and method == "POST":
            body = self.read_json()
            return self.send_json(start_run(
                str(body.get("conv_id") or ""), str(body.get("code") or ""),
                str(body.get("scene") or ""), body.get("mode") or "preview", body.get("quality") or "l",
            ))
        if len(parts) == 2 and parts[0] == "run" and method == "GET":
            with lock:
                job = jobs.get(parts[1])
                if not job:
                    raise KeyError(parts[1])
                return self.send_json(public_job(job))
        if len(parts) == 3 and parts[0] == "run" and parts[2] == "stop" and method == "POST":
            with lock:
                job = jobs.get(parts[1])
                if not job:
                    raise KeyError(parts[1])
                job["status"] = "stopped"
            kill_tree(job["proc"])
            return self.send_json({"ok": True})
        if parts == ["open-folder"] and method == "POST":
            conv_id = str(self.read_json().get("conv_id") or "")
            folder = WORKSPACE_DIR / conv_id if ID_RE.match(conv_id) else WORKSPACE_DIR
            folder.mkdir(parents=True, exist_ok=True)
            os.startfile(folder)  # noqa: S606 - opens Explorer on our own workspace folder
            return self.send_json({"ok": True})
        return self.send_error_json(HTTPStatus.NOT_FOUND, "unknown endpoint")

    def serve_index(self):
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8").replace("__STUDIO_TOKEN__", TOKEN)
        body = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # chat, streamed as server-sent events

    def sse(self, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False)
        self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
        self.wfile.flush()

    def chat(self, body: dict):
        conv_id = str(body.get("conv_id") or "")
        text = str(body.get("text") or "").strip()
        images = [Path(n).name for n in body.get("images") or [] if (UPLOAD_DIR / Path(n).name).is_file()]
        if not text and not images:
            raise ValueError("消息为空")
        config = load_config()
        providers = {p["id"]: p for p in config["providers"]}
        with lock:
            conv = load_conv(conv_id)
            provider_id = body.get("provider_id") or conv["provider_id"]
            if provider_id not in providers:
                raise ValueError("模型提供商不存在，请在设置里检查")
            provider = providers[provider_id]
            model = (body.get("model") or conv.get("model") or provider["model"]).strip()
            use_skill = bool(body.get("use_skill", conv.get("use_skill", True)))
            thinking_on = bool(body.get("thinking", conv["thinking"]))
            effort = body.get("effort") if body.get("effort") in EFFORTS else conv["effort"]
            history_has_images = images or any(m.get("images") for m in conv["messages"])
            if history_has_images and not provider.get("vision"):
                raise ValueError(
                    f"{provider['name']} 当前配置为不支持图片输入。请换用支持图片的模型（如 OpenAI / Claude），"
                    "或在设置里勾选“支持图片”（若该模型确实支持）。"
                )
            conv.update(provider_id=provider_id, model=model, use_skill=use_skill,
                        thinking=thinking_on, effort=effort)
            if not conv["messages"]:
                conv["title"] = (text or "图片对话").replace("\n", " ")[:30]
            conv["messages"].append({"role": "user", "content": text, "images": images, "ts": time.time()})
            save_conv(conv)
            history = [dict(m) for m in conv["messages"]]

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        answer, thinking, notices = [], [], []
        usage = None
        error = None
        disconnected = False
        system = prompts.build_system_prompt(use_skill)
        chunks = stream_chat(provider, model, system, history, UPLOAD_DIR, thinking_on, effort)
        try:
            self.sse({"type": "start", "provider": provider["name"], "model": model, "skill": use_skill,
                      "thinking": thinking_on, "effort": effort})
            for kind, chunk in chunks:
                if kind == "usage":
                    usage = price_usage(provider, model, chunk)
                    self.sse({"type": "usage", "usage": usage})
                    continue
                if kind == "text":
                    answer.append(chunk)
                elif kind == "thinking":
                    thinking.append(chunk)
                else:
                    notices.append(chunk)
                self.sse({"type": kind, "text": chunk})
        except ProviderError as e:
            error = str(e)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            disconnected = True  # the page pressed stop: keep what arrived
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
        finally:
            chunks.close()  # ends the provider's HTTP stream too when the page stopped reading

        content = "".join(answer)
        if not (content or thinking):
            # Nothing came back: take the question out again, the page puts it back in the input box
            with lock:
                conv = load_conv(conv_id)
                if conv["messages"] and conv["messages"][-1]["role"] == "user":
                    conv["messages"].pop()
                    if not conv["messages"]:
                        conv["title"] = "新对话"
                    save_conv(conv)
        else:
            with lock:
                conv = load_conv(conv_id)
                conv["messages"].append({
                    "role": "assistant", "content": content, "thinking": "".join(thinking),
                    "notices": notices + (["（已手动停止生成）"] if disconnected else []),
                    "provider": provider["name"], "model": model, "ts": time.time(),
                    "usage": usage, "skill": use_skill,
                })
                save_conv(conv)
        if not disconnected:
            try:
                if error:
                    self.sse({"type": "error", "text": error})
                self.sse({"type": "done"})
            except OSError:
                pass


def already_running(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=1) as r:
            return json.loads(r.read()).get("app") == "manim-ai-studio"
    except (OSError, ValueError):
        return False


def main():
    parser = argparse.ArgumentParser(description="ManimGL AI Studio")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    url = f"http://127.0.0.1:{args.port}/"
    if already_running(args.port):
        print(f"AI Studio is already running at {url} - opening it in the browser.")
        if not args.no_browser:
            webbrowser.open(url)
        return

    for d in (CONV_DIR, UPLOAD_DIR, WORKSPACE_DIR):
        d.mkdir(parents=True, exist_ok=True)
    load_config()
    # Build the API reference now, so the first message does not wait the few seconds it takes
    threading.Thread(target=api_reference.load, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.daemon_threads = True
    print("=" * 60)
    print(" ManimGL AI Studio")
    print(f" Open: {url}")
    print(" Close this window (or press Ctrl+C) to stop the server.")
    print("=" * 60, flush=True)
    if not args.no_browser:
        threading.Timer(0.8, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        with lock:
            for job in jobs.values():
                kill_tree(job["proc"])
        server.server_close()


if __name__ == "__main__":
    main()
