"""
A compact API reference for the manimlib in this checkout, read off its own source with inspect.

Models write ManimGL from memory, mixing in Manim Community names, and the skill was written for
upstream ManimGL, while this checkout renders through wgpu (WGSL rather than GLSL, for one). So
the reference sent to the model, and the signatures pulled out when a script fails, come from
the code that will actually run it.
"""
from __future__ import annotations

import inspect
import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path

STUDIO_DIR = Path(__file__).resolve().parent
MANIMLIB_DIR = STUDIO_DIR.parent / "manimlib"
CACHE_PATH = STUDIO_DIR / "data" / "api_reference.json"

# The methods worth listing in full, on the classes scripts call most
METHOD_CLASSES = ["Mobject", "VMobject", "CameraFrame", "Scene", "CoordinateSystem", "Axes", "Surface"]
MAX_DEFAULT_LEN = 28
_build_lock = threading.Lock()


def source_stamp() -> str:
    """Changes whenever any file of manimlib does"""
    newest = max((p.stat().st_mtime for p in MANIMLIB_DIR.rglob("*.py")), default=0)
    return f"{newest:.0f}"


def short(value) -> str:
    text = repr(value)
    if re.match(r"^<function (\w+)", text):
        return re.match(r"^<function (\w+)", text).group(1)
    if len(text) > MAX_DEFAULT_LEN:
        text = text[: MAX_DEFAULT_LEN - 3] + "..."
    return text


def describe_params(func) -> str:
    try:
        sig = inspect.signature(func)
    except (TypeError, ValueError):
        return "..."
    parts = []
    for p in list(sig.parameters.values()):
        if p.name == "self":
            continue
        if p.kind is p.VAR_POSITIONAL:
            parts.append("*" + p.name)
        elif p.kind is p.VAR_KEYWORD:
            parts.append("**" + p.name)
        elif p.default is inspect.Parameter.empty:
            parts.append(p.name)
        else:
            parts.append(f"{p.name}={short(p.default)}")
    return ", ".join(parts)


def build() -> dict:
    """{"classes": {name: line}, "methods": {class: [lines]}} for everything `from manimlib import *` gives"""
    import manimlib  # noqa: F401  (heavy: only imported when the cache is stale)
    from manimlib.animation.animation import Animation
    from manimlib.camera.camera_frame import CameraFrame
    from manimlib.mobject.mobject import Mobject
    from manimlib.scene.scene import Scene

    namespace: dict = {}
    exec("from manimlib import *", namespace)
    classes: dict[str, str] = {}
    owners = (Mobject, Animation, Scene)
    for name, obj in sorted(namespace.items()):
        if not inspect.isclass(obj) or name.startswith("_") or not issubclass(obj, owners):
            continue
        if obj.__module__.split(".")[0] != "manimlib":
            continue
        parent = next((b.__name__ for b in obj.__bases__), "")
        classes[name] = f"{name}({describe_params(obj.__init__)})  <- {parent}"

    methods: dict[str, list[str]] = {}
    for cls_name in METHOD_CLASSES:
        cls = namespace.get(cls_name) or (CameraFrame if cls_name == "CameraFrame" else None)
        if cls is None:
            continue
        lines = []
        for name, func in cls.__dict__.items():
            if name.startswith("_") or not callable(func):
                continue
            lines.append(f".{name}({describe_params(func)})")
        methods[cls_name] = lines
    return {"classes": classes, "methods": methods}


def read_cache(stamp: str) -> dict | None:
    if CACHE_PATH.exists():
        try:
            cached = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
            if cached.get("stamp") == stamp:
                return cached
        except ValueError:
            pass
    return None


def write_cache() -> None:
    data = build()
    data["stamp"] = source_stamp()
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def load() -> dict:
    """
    The reference, rebuilt when manimlib has changed. Building imports manimlib, which reads
    sys.argv for its own options, so it runs in a separate interpreter rather than in the server.
    """
    with _build_lock:
        return _load()


def _load() -> dict:
    stamp = source_stamp()
    cached = read_cache(stamp)
    if cached is None:
        env = dict(os.environ, PYTHONNOUSERSITE="1", PYTHONIOENCODING="utf-8")
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--build"],
            cwd=STUDIO_DIR, env=env, capture_output=True, text=True, encoding="utf-8",
            timeout=180, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        cached = read_cache(stamp)
        if cached is None:
            raise RuntimeError("building the API reference failed: " + (result.stderr or result.stdout)[-800:])
    return cached


def reference_text() -> str:
    """The whole reference, for the system prompt"""
    data = load()
    out = ["## 本机 manimlib 的真实 API（从源码自动提取，优先级最高）",
           "类名(构造参数) <- 父类。没列出的关键字参数会经 **kwargs 传给父类；父类也不接受的参数就会报错。"]
    out += sorted(data["classes"].values())
    for cls_name, lines in data["methods"].items():
        out.append(f"\n### {cls_name} 的方法")
        out += lines
    return "\n".join(out)


def lookup_for_error(log: str, code: str = "") -> str:
    """
    The signatures a failed run most likely needs: classes named in the traceback's last lines
    and in the failing source line, followed up their parent chain.
    """
    data = load()
    classes = data["classes"]
    tail = "\n".join(log.splitlines()[-40:])
    names = set(re.findall(r"\b([A-Z][A-Za-z0-9_]+)\b", tail))
    names |= set(re.findall(r"\b([A-Z][A-Za-z0-9_]+)\.__init__", tail))
    found: list[str] = []
    seen: set[str] = set()

    def add(name: str, depth: int = 0):
        if name in seen or name not in classes or depth > 4:
            return
        seen.add(name)
        found.append(classes[name])
        parent = classes[name].rsplit("<- ", 1)[-1].strip()
        if "**" in classes[name]:
            add(parent, depth + 1)

    for name in sorted(names):
        add(name)
    # A class named in the error is often the wrong one to have used: list a few of its direct
    # subclasses too (Surface -> ParametricSurface, Sphere, ...)
    named = [n for n in sorted(names) if n in classes]
    for name in named:
        children = [c for c, line in classes.items() if line.rsplit("<- ", 1)[-1].strip() == name]
        for child in children[:6]:
            if child not in seen:
                seen.add(child)
                found.append(classes[child])
    for cls_name, lines in data["methods"].items():
        for attr in re.findall(r"has no attribute '(\w+)'|\.(\w+)\(\) got an unexpected", tail):
            attr = next(a for a in attr if a)
            found += [f"{cls_name}{line}" for line in lines if line.startswith(f".{attr}(")]
    return "\n".join(dict.fromkeys(found))


if __name__ == "__main__":
    if sys.argv[1:] == ["--build"]:
        sys.argv = sys.argv[:1]  # leave manimlib an empty command line to parse
        write_cache()
    else:
        sys.stdout.reconfigure(encoding="utf-8")
        print(reference_text())
