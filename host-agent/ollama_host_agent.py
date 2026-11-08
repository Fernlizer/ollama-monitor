#!/usr/bin/env python3
"""Ollama Monitor — host agent (stdlib only, no dependencies).

Runs on the macOS HOST and exposes the few operations a Docker container
cannot perform against the host:

  GET  /health   -> {"ok": true}
  GET  /metrics  -> {"proc": {...}, "system": {...}, "disk_bytes": N}
  POST /service  -> {"action": "start" | "stop" | "restart"}

The dashboard container calls this over http://host.docker.internal:9021.
Bind address is 127.0.0.1 only (it can quit/restart applications).
"""

import json
import os
import re
import subprocess
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = "127.0.0.1"
PORT = int(os.environ.get("HOST_AGENT_PORT", "9021"))
OLLAMA_APP_NAME = os.environ.get("OLLAMA_APP_NAME", "Ollama")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
MODEL_DIR = os.path.expanduser(os.environ.get("OLLAMA_MODEL_DIR", "~/.ollama/models"))
DISK_CACHE_TTL = 300.0

_disk_cache = {"bytes": None, "ts": 0.0}


def _run(cmd, timeout=15):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def ollama_alive(timeout=1.5):
    try:
        urllib.request.urlopen(f"{OLLAMA_URL}/api/version", timeout=timeout)
        return True
    except Exception:
        return False


def ollama_pid():
    try:
        out = _run(["pgrep", "-f", "ollama serve"]).stdout.strip()
        return int(out.splitlines()[0]) if out else None
    except Exception:
        return None


def _parse_etime(etime):
    m = re.match(r"^(?:(\d+)-)?(?:(\d+):)?(\d+):(\d+)$", etime.strip())
    if not m:
        return 0
    d, h, mm, ss = (int(g) if g else 0 for g in m.groups())
    return d * 86400 + h * 3600 + mm * 60 + ss


def proc_metrics(pid):
    try:
        out = _run(["ps", "-p", str(pid), "-o", "%cpu=,rss=,etime="]).stdout.strip()
        if not out:
            return {"pid": pid, "cpu_pct": 0.0, "rss_bytes": 0, "uptime_s": 0}
        cpu, rss, etime = out.split()
        return {
            "pid": pid,
            "cpu_pct": round(float(cpu), 1),
            "rss_bytes": int(rss) * 1024,
            "uptime_s": _parse_etime(etime),
        }
    except Exception:
        return {"pid": pid, "cpu_pct": 0.0, "rss_bytes": 0, "uptime_s": 0}


def system_memory():
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

        def pages(label):
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


def disk_usage():
    now = time.time()
    if _disk_cache["bytes"] is not None and now - _disk_cache["ts"] < DISK_CACHE_TTL:
        return _disk_cache["bytes"]
    try:
        out = _run(["du", "-sk", MODEL_DIR], timeout=60).stdout.strip()
        size = int(out.split()[0]) * 1024
        _disk_cache.update(bytes=size, ts=now)
        return size
    except Exception:
        return _disk_cache["bytes"] or 0


def service_action(action):
    """start | stop | restart the Ollama.app on the host."""
    if action not in {"start", "stop", "restart"}:
        raise ValueError("action ต้องเป็น start, stop หรือ restart")

    def wait_state(up, timeout):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if ollama_alive() == up:
                return True
            time.sleep(1.0)
        return False

    result = {"action": action, "app": OLLAMA_APP_NAME}

    if action in {"stop", "restart"}:
        _run(["osascript", "-e", f'quit app "{OLLAMA_APP_NAME}"'])
        result["stopped"] = wait_state(False, 25)
        if not result["stopped"]:
            _run(["pkill", "-f", "ollama serve"])  # fallback for CLI-managed serve
            result["stopped"] = wait_state(False, 10)
        if not result["stopped"]:
            raise RuntimeError("หยุด Ollama ไม่สำเร็จ (กระบวนการยังไม่หยุด)")

    if action in {"start", "restart"}:
        _run(["open", "-a", OLLAMA_APP_NAME])
        result["started"] = wait_state(True, 40)
        if not result["started"]:
            raise RuntimeError("เปิด Ollama ไม่สำเร็จ (เซิร์ฟเวอร์ยังไม่ตอบสนอง)")

    result["running"] = ollama_alive()
    return result


class Handler(BaseHTTPRequestHandler):
    def _send(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._send({"ok": True, "app": OLLAMA_APP_NAME})
        elif self.path == "/metrics":
            pid = ollama_pid()
            self._send({
                "proc": proc_metrics(pid) if pid else {"pid": None, "cpu_pct": 0.0, "rss_bytes": 0, "uptime_s": 0},
                "system": system_memory(),
                "disk_bytes": disk_usage(),
            })
        else:
            self._send({"error": "not found"}, 404)

    def do_POST(self):
        if self.path != "/service":
            self._send({"error": "not found"}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            self._send(service_action(payload.get("action", "")))
        except ValueError as e:
            self._send({"error": str(e)}, 400)
        except Exception as e:
            self._send({"error": str(e)}, 500)

    def log_message(self, fmt, *args):
        print(f"[host-agent] {self.address_string()} {fmt % args}")


if __name__ == "__main__":
    print(f">> Ollama Monitor host agent ที่ http://{HOST}:{PORT}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
