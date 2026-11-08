"""Ollama Monitor — FastAPI backend.

Proxies the local Ollama API, adds service control (start/stop/restart the
Ollama.app), model control (load/unload/keep-alive), log tailing, and a
lightweight in-memory metrics history for the dashboard charts.

Bind address is 127.0.0.1 by default (control endpoints are powerful).
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import time
from collections import deque
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434").rstrip("/")
OLLAMA_APP_NAME = os.environ.get("OLLAMA_APP_NAME", "Ollama")
LOG_DIR = Path(os.environ.get("OLLAMA_LOG_DIR", Path.home() / ".ollama" / "logs"))
MODEL_DIR = Path(os.environ.get("OLLAMA_MODEL_DIR", Path.home() / ".ollama" / "models"))
PORT = int(os.environ.get("PORT", "9020"))
# When running in Docker, host-specific work (process metrics, disk usage,
# service control) is delegated to the host agent, because a container cannot
# see or signal host processes. See host-agent/ollama_host_agent.py.
HOST_AGENT_URL = os.environ.get("HOST_AGENT_URL", "").rstrip("/")
USE_AGENT = bool(HOST_AGENT_URL)

HISTORY_MAX = 1800          # 2s tick -> ~1 hour of history
SAMPLE_INTERVAL = 2.0
DISK_CACHE_TTL = 300.0

app = FastAPI(title="Ollama Monitor", version="1.0.0")

_history: deque[dict] = deque(maxlen=HISTORY_MAX)
_disk_cache: dict = {"bytes": None, "ts": 0.0}
_client: httpx.AsyncClient | None = None


# ---------------------------------------------------------------- helpers

def _run(cmd: list[str], timeout: int = 15) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def ollama_pid() -> int | None:
    """PID of the running `ollama serve` process, if any."""
    try:
        out = _run(["pgrep", "-f", "ollama serve"]).stdout.strip()
        return int(out.splitlines()[0]) if out else None
    except Exception:
        return None


def proc_metrics(pid: int) -> dict:
    """CPU%% and RSS of the ollama serve process."""
    try:
        out = _run(["ps", "-p", str(pid), "-o", "%cpu=,rss=,etime="]).stdout.strip()
        if not out:
            return {"cpu_pct": 0.0, "rss_bytes": 0, "uptime_s": 0}
        cpu, rss, etime = out.split()
        return {
            "cpu_pct": round(float(cpu), 1),
            "rss_bytes": int(rss) * 1024,
            "uptime_s": _parse_etime(etime),
        }
    except Exception:
        return {"cpu_pct": 0.0, "rss_bytes": 0, "uptime_s": 0}


def _parse_etime(etime: str) -> int:
    """Parse ps etime ([[dd-]hh:]mm:ss) to seconds."""
    m = re.match(r"^(?:(\d+)-)?(?:(\d+):)?(\d+):(\d+)$", etime.strip())
    if not m:
        return 0
    d, h, mm, ss = (int(g) if g else 0 for g in m.groups())
    return d * 86400 + h * 3600 + mm * 60 + ss


def system_memory() -> dict:
    """Total/used/swap memory via sysctl + vm_stat (macOS)."""
    total = 0
    try:
        total = int(_run(["sysctl", "-n", "hw.memsize"]).stdout.strip())
    except Exception:
        pass
    page_size, used = 16384, 0
    try:
        out = _run(["vm_stat"]).stdout
        m = re.search(r"page size of (\d+) bytes", out)
        if m:
            page_size = int(m.group(1))

        def pages(label: str) -> int:
            mm = re.search(rf"{label}:\s+(\d+)\.", out)
            return int(mm.group(1)) if mm else 0

        used = (
            pages("Pages active")
            + pages("Pages wired down")
            + pages("Pages occupied by compressor")
        ) * page_size
    except Exception:
        pass
    swap_used = 0
    try:
        out = _run(["sysctl", "-n", "vm.swapusage"]).stdout
        m = re.search(r"used = ([\d.]+)([MG])", out)
        if m:
            swap_used = int(float(m.group(1)) * (1024 ** 2 if m.group(2) == "M" else 1024 ** 3))
    except Exception:
        pass
    return {
        "mem_total": total,
        "mem_used": used,
        "mem_pct": round(used / total * 100, 1) if total else 0.0,
        "swap_used": swap_used,
    }


def disk_usage() -> int:
    """Size of the Ollama model directory (cached 5 min)."""
    now = time.time()
    if _disk_cache["bytes"] is not None and now - _disk_cache["ts"] < DISK_CACHE_TTL:
        return _disk_cache["bytes"]
    try:
        out = _run(["du", "-sk", str(MODEL_DIR)], timeout=60).stdout.strip()
        size = int(out.split()[0]) * 1024
        _disk_cache.update(bytes=size, ts=now)
        return size
    except Exception:
        return _disk_cache["bytes"] or 0


async def ollama_get(path: str, timeout: float = 5.0) -> dict | None:
    """GET from Ollama; returns None when the server is unreachable."""
    assert _client is not None
    try:
        r = await _client.get(f"{OLLAMA_URL}{path}", timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


async def agent_get(path: str, timeout: float = 10.0) -> dict | None:
    assert _client is not None
    try:
        r = await _client.get(f"{HOST_AGENT_URL}{path}", timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


async def agent_post(path: str, body: dict, timeout: float = 120.0) -> dict:
    assert _client is not None
    try:
        r = await _client.post(f"{HOST_AGENT_URL}{path}", json=body, timeout=timeout)
        data = r.json()
        if r.status_code >= 400:
            raise HTTPException(r.status_code, data.get("error", "host agent error"))
        return data
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"ติดต่อ host agent ไม่ได้: {e}")


async def sample() -> dict:
    """One metrics sample: running models + process + system memory."""
    ps = await ollama_get("/api/ps")
    running = (ps or {}).get("models", [])
    if USE_AGENT:
        m = await agent_get("/metrics") or {}
        proc = m.get("proc", {"cpu_pct": 0.0, "rss_bytes": 0, "uptime_s": 0})
        sysm = m.get("system", {"mem_total": 0, "mem_used": 0, "mem_pct": 0.0, "swap_used": 0})
        disk = m.get("disk_bytes", 0)
    else:
        pid = ollama_pid()
        proc = proc_metrics(pid) if pid else {"cpu_pct": 0.0, "rss_bytes": 0, "uptime_s": 0}
        sysm = system_memory()
        disk = disk_usage()
    snap = {
        "ts": time.time(),
        "running_count": len(running),
        "model_mem": sum(m.get("size", 0) for m in running),
        "model_vram": sum(m.get("size_vram", 0) for m in running),
        "proc": proc,
        "sys": sysm,
        "disk": disk,
    }
    _history.append(snap)
    return snap


async def sampler_loop() -> None:
    while True:
        try:
            await sample()
        except Exception:
            pass
        await asyncio.sleep(SAMPLE_INTERVAL)


@app.on_event("startup")
async def _startup() -> None:
    global _client
    _client = httpx.AsyncClient()
    app.state.sampler = asyncio.create_task(sampler_loop())


@app.on_event("shutdown")
async def _shutdown() -> None:
    app.state.sampler.cancel()
    if _client:
        await _client.aclose()


# ---------------------------------------------------------------- schemas

class ServiceAction(BaseModel):
    action: str  # start | stop | restart


class LoadRequest(BaseModel):
    name: str
    keep_alive: str = "15m"


class UnloadRequest(BaseModel):
    name: str


# ---------------------------------------------------------------- API

@app.get("/api/overview")
async def overview() -> dict:
    version = await ollama_get("/api/version")
    ps = await ollama_get("/api/ps")
    tags = await ollama_get("/api/tags")
    running = (ps or {}).get("models", [])
    pid = ollama_pid()
    snap = await sample()
    return {
        "ollama": {
            "running": version is not None,
            "version": (version or {}).get("version", "—"),
            "url": OLLAMA_URL,
            "pid": snap["proc"].get("pid") if USE_AGENT else pid,
            "uptime_s": snap["proc"]["uptime_s"],
        },
        "models": {
            "total": len((tags or {}).get("models", [])),
            "disk_bytes": snap["disk"],
        },
        "running": {
            "count": len(running),
            "mem_bytes": sum(m.get("size", 0) for m in running),
            "vram_bytes": sum(m.get("size_vram", 0) for m in running),
        },
        "process": snap["proc"],
        "system": snap["sys"],
        "history": list(_history)[-300:],
    }


@app.get("/api/models")
async def models() -> dict:
    tags = await ollama_get("/api/tags")
    if tags is None:
        raise HTTPException(503, "Ollama ไม่ตอบสนอง")
    return tags


@app.get("/api/running")
async def running() -> dict:
    ps = await ollama_get("/api/ps")
    if ps is None:
        raise HTTPException(503, "Ollama ไม่ตอบสนอง")
    return ps


@app.post("/api/models/load")
async def load_model(req: LoadRequest) -> dict:
    """Warm-load (or extend keep-alive of) a model without generating tokens."""
    assert _client is not None
    try:
        r = await _client.post(
            f"{OLLAMA_URL}/api/generate",
            json={"model": req.name, "prompt": "", "keep_alive": req.keep_alive, "stream": False},
            timeout=300.0,
        )
        r.raise_for_status()
        return r.json()
    except httpx.HTTPError as e:
        raise HTTPException(502, f"โหลดโมเดลไม่สำเร็จ: {e}")


@app.post("/api/models/unload")
async def unload_model(req: UnloadRequest) -> dict:
    assert _client is not None
    try:
        r = await _client.post(
            f"{OLLAMA_URL}/api/generate",
            json={"model": req.name, "prompt": "", "keep_alive": 0, "stream": False},
            timeout=60.0,
        )
        r.raise_for_status()
        return r.json()
    except httpx.HTTPError as e:
        raise HTTPException(502, f"ปลดโหลดโมเดลไม่สำเร็จ: {e}")


@app.get("/api/logs")
async def logs(lines: int = Query(200, ge=1, le=2000)) -> dict:
    """Tail of the newest Ollama server log."""
    try:
        candidates = sorted(LOG_DIR.glob("server*.log"), key=lambda p: p.stat().st_mtime)
        if not candidates:
            return {"file": None, "lines": []}
        f = candidates[-1]
        with open(f, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            block = 4096
            data = b""
            while size > 0 and data.count(b"\n") <= lines:
                step = min(block, size)
                size -= step
                fh.seek(size)
                data = fh.read(step) + data
        text = data.decode("utf-8", errors="replace").splitlines()[-lines:]
        return {"file": f.name, "lines": text}
    except Exception as e:
        raise HTTPException(500, f"อ่าน log ไม่ได้: {e}")


@app.post("/api/service")
async def service_control(req: ServiceAction) -> dict:
    """Start / stop / restart the Ollama.app service on this machine."""
    action = req.action.lower()
    if action not in {"start", "stop", "restart"}:
        raise HTTPException(400, "action ต้องเป็น start, stop หรือ restart")

    # In Docker mode the host agent owns this (a container cannot signal host apps).
    if USE_AGENT:
        return await agent_post("/service", {"action": action})

    async def wait_state(up: bool, timeout: float = 25.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            alive = (await ollama_get("/api/version", timeout=1.5)) is not None
            if alive == up:
                return True
            await asyncio.sleep(1.0)
        return False

    result: dict = {"action": action, "app": OLLAMA_APP_NAME}

    if action in {"stop", "restart"}:
        _run(["osascript", "-e", f'quit app "{OLLAMA_APP_NAME}"'])
        result["stopped"] = await wait_state(False)
        if not result["stopped"]:
            # fallback for CLI-managed `ollama serve`
            _run(["pkill", "-f", "ollama serve"])
            result["stopped"] = await wait_state(False, timeout=10.0)
        if not result["stopped"]:
            raise HTTPException(500, "หยุด Ollama ไม่สำเร็จ (กระบวนการยังไม่หยุด)")

    if action in {"start", "restart"}:
        _run(["open", "-a", OLLAMA_APP_NAME])
        result["started"] = await wait_state(True, timeout=40.0)
        if not result["started"]:
            raise HTTPException(500, "เปิด Ollama ไม่สำเร็จ (เซิร์ฟเวอร์ยังไม่ตอบสนอง)")

    result["running"] = (await ollama_get("/api/version")) is not None
    return result


# ---------------------------------------------------------------- frontend

@app.get("/")
async def index() -> FileResponse:
    return FileResponse(Path(__file__).parent / "static" / "index.html")


app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=PORT)
