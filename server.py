#!/usr/bin/env python3
"""Steady: a local-first helper for neurodivergent and disabled people.

Standard library only. Talks to a small model running on this computer through
an OpenAI-compatible endpoint (Ollama, llama.cpp server, LM Studio, etc.).
Nothing leaves the machine unless the person changes config.json to allow it,
and every outbound request is written to a log the person can read.
"""
import json
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("STEADY_CONFIG", ROOT / "config.json"))
LOOPBACK = {"127.0.0.1", "localhost", "::1", "[::1]"}

DEFAULTS = {
    "port": 8787,
    "model_url": "http://127.0.0.1:11434/v1",
    "model_name": "llama3.2:3b",
    "allow_remote_model": False,
    "data_dir": "data",
    "max_input_chars": 4000,
    "temperature": 0.3,
    "max_tokens": 500,
    "request_timeout_seconds": 120,
}

CRISIS_PATTERNS = [
    r"\bkill (myself|me)\b", r"\bend (my|it all|my life)\b", r"\bsuicid", r"\bwant to die\b",
    r"\bdon'?t want to (be here|live|exist)\b", r"\bbetter off (dead|without me)\b",
    r"\bhurt(ing)? myself\b", r"\bself[- ]?harm", r"\bcut(ting)? myself\b",
    r"\bno reason to (live|go on)\b", r"\bcan'?t go on\b", r"\btake (all )?(my|the) pills\b",
]
CRISIS_RE = re.compile("|".join(CRISIS_PATTERNS), re.I)

CRISIS_TEXT = (
    "It sounds like things might be very hard right now. Steady is a small tool and "
    "can't help with this the way a person can.\n\n"
    "If you are in danger right now, call 111.\n"
    "To talk to someone, you can call or text 1737 at any time. It is free and open 24/7.\n"
    "Lifeline: 0800 543 354 (call) or text 4357.\n"
    "Suicide Crisis Helpline: 0508 828 865 (0508 TAUTOKO).\n\n"
    "If you can, tell someone near you how you are feeling. You do not have to do the task now."
)


def load_config():
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        cfg.update(json.loads(CONFIG_PATH.read_text()))
    return cfg


CFG = load_config()
PROMPTS = json.loads((ROOT / "prompts.json").read_text())
DATA_DIR = Path(CFG["data_dir"])
if not DATA_DIR.is_absolute():
    DATA_DIR = ROOT / DATA_DIR
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "steady.db"
DB_LOCK = threading.Lock()


def model_host():
    return urlparse(CFG["model_url"]).hostname or ""


def check_remote_policy():
    if model_host() not in LOOPBACK and not CFG["allow_remote_model"]:
        sys.exit(
            f"Refusing to start: model_url points to '{model_host()}', which is not this computer.\n"
            "Steady keeps your data on this device. To allow a remote model on purpose, "
            "set allow_remote_model to true in config.json."
        )


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with DB_LOCK, db() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS checkins (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                energy INTEGER NOT NULL,
                overwhelm INTEGER NOT NULL,
                sensory INTEGER NOT NULL,
                note TEXT NOT NULL DEFAULT ''
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS netlog (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                host TEXT NOT NULL,
                purpose TEXT NOT NULL,
                bytes_sent INTEGER NOT NULL
            )"""
        )


def log_outbound(purpose, nbytes):
    with DB_LOCK, db() as c:
        c.execute(
            "INSERT INTO netlog (ts, host, purpose, bytes_sent) VALUES (?,?,?,?)",
            (time.strftime("%Y-%m-%d %H:%M:%S"), model_host(), purpose, nbytes),
        )


def is_crisis(text):
    return bool(CRISIS_RE.search(text or ""))


def call_model(system, user):
    payload = json.dumps(
        {
            "model": CFG["model_name"],
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": CFG["temperature"],
            "max_tokens": CFG["max_tokens"],
            "stream": False,
        }
    ).encode()
    log_outbound("model request (text you typed, sent to your local model)", len(payload))
    req = urllib.request.Request(
        CFG["model_url"].rstrip("/") + "/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=CFG["request_timeout_seconds"]) as r:
        data = json.loads(r.read())
    return data["choices"][0]["message"]["content"].strip()


def model_status():
    try:
        log_outbound("model status check (no personal data)", 0)
        with urllib.request.urlopen(CFG["model_url"].rstrip("/") + "/models", timeout=2) as r:
            r.read()
        return True
    except Exception:
        return False


def fallback_breakdown(text, low_energy):
    """Plain rule-based splitting used when no local model is running."""
    parts = re.split(r"[\n;]|,\s*(?:and\s+)?|\s+and then\s+|\s+then\s+|\s+and\s+", text)
    parts = [p.strip(" .-") for p in parts if len(p.strip(" .-")) > 2]
    if not parts:
        parts = [text.strip()]
    limit = 4 if low_energy else 8
    steps = [f"{i}. {p[0].upper() + p[1:]}" for i, p in enumerate(parts[:limit], 1)]
    steps.insert(0, "0. Easiest start: put the first thing you need in front of you. That counts.")
    steps.append("Good enough version: do step 1 only. Stopping there is allowed.")
    return "\n".join(steps)


def build_prompt(mode, style, energy, size):
    system = PROMPTS["shared"] + " "
    if mode == "breakdown":
        max_steps = {"tiny": 5, "normal": 8}.get(size, 8)
        if energy == "low":
            max_steps = min(max_steps, 4)
        system += PROMPTS["breakdown"].replace("{max_steps}", str(max_steps))
        if energy == "low":
            system += " Energy today: low."
    elif mode == "plain":
        key = {"clearer": "plain_clearer", "softer": "plain_softer", "decode": "plain_decode"}.get(style)
        if not key:
            raise ValueError("unknown style")
        system += PROMPTS[key]
    else:
        raise ValueError("unknown mode")
    return system


def clamp(v):
    return max(1, min(5, int(v)))


class Handler(BaseHTTPRequestHandler):
    server_version = "Steady/0.1"

    def log_message(self, *args):
        pass

    # ---- helpers ----
    def host_ok(self):
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0] if not (self.headers.get("Host") or "").startswith("[") else "[::1]"
        return host in LOOPBACK

    def send_json(self, code, obj, extra_headers=None):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 20000:
            raise ValueError("too large")
        return json.loads(self.rfile.read(n) or b"{}")

    def guard(self, mutating):
        if not self.host_ok():
            self.send_json(403, {"error": "Steady only answers on this computer."})
            return False
        if mutating and self.headers.get("X-Steady") != "1":
            self.send_json(403, {"error": "Missing request header."})
            return False
        return True

    # ---- routes ----
    def do_GET(self):
        if not self.guard(False):
            return
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            body = (ROOT / "static" / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
                "connect-src 'self'; img-src 'self' data:",
            )
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/status":
            with DB_LOCK, db() as c:
                n = c.execute("SELECT COUNT(*) FROM checkins").fetchone()[0]
            self.send_json(
                200,
                {
                    "model_ready": model_status(),
                    "model_name": CFG["model_name"],
                    "model_host": model_host(),
                    "allow_remote_model": CFG["allow_remote_model"],
                    "data_path": str(DB_PATH),
                    "checkin_count": n,
                },
            )
        elif path == "/api/checkins":
            with DB_LOCK, db() as c:
                rows = [dict(r) for r in c.execute("SELECT * FROM checkins ORDER BY id DESC LIMIT 60")]
            self.send_json(200, {"checkins": rows})
        elif path == "/api/insights":
            self.send_json(200, self.insights())
        elif path == "/api/netlog":
            with DB_LOCK, db() as c:
                rows = [dict(r) for r in c.execute("SELECT * FROM netlog ORDER BY id DESC LIMIT 50")]
            self.send_json(200, {"netlog": rows})
        elif path == "/api/prompts":
            self.send_json(200, PROMPTS)
        elif path == "/api/export":
            with DB_LOCK, db() as c:
                data = {
                    "exported": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "checkins": [dict(r) for r in c.execute("SELECT * FROM checkins ORDER BY id")],
                    "netlog": [dict(r) for r in c.execute("SELECT * FROM netlog ORDER BY id")],
                }
            self.send_json(200, data, {"Content-Disposition": 'attachment; filename="steady-export.json"'})
        else:
            self.send_json(404, {"error": "Not found"})

    def do_POST(self):
        if not self.guard(True):
            return
        path = urlparse(self.path).path
        try:
            body = self.read_json()
        except Exception:
            return self.send_json(400, {"error": "Could not read that request."})

        if path == "/api/assist":
            return self.assist(body)
        if path == "/api/checkins":
            return self.add_checkin(body)
        if path == "/api/wipe":
            if body.get("confirm") != "DELETE":
                return self.send_json(400, {"error": "Type DELETE to confirm."})
            with DB_LOCK:
                with db() as c:
                    c.execute("DELETE FROM checkins")
                    c.execute("DELETE FROM netlog")
                conn = sqlite3.connect(DB_PATH)
                conn.execute("VACUUM")
                conn.close()
            return self.send_json(200, {"ok": True})
        self.send_json(404, {"error": "Not found"})

    def do_DELETE(self):
        if not self.guard(True):
            return
        m = re.fullmatch(r"/api/checkins/(\d+)", urlparse(self.path).path)
        if not m:
            return self.send_json(404, {"error": "Not found"})
        with DB_LOCK, db() as c:
            c.execute("DELETE FROM checkins WHERE id=?", (int(m.group(1)),))
        self.send_json(200, {"ok": True})

    # ---- handlers ----
    def assist(self, body):
        text = str(body.get("text", "")).strip()
        if not text:
            return self.send_json(400, {"error": "Write something first."})
        if len(text) > CFG["max_input_chars"]:
            return self.send_json(400, {"error": f"That is long. Please keep it under {CFG['max_input_chars']} characters."})
        # Crisis check runs first and never depends on the model.
        if is_crisis(text):
            return self.send_json(200, {"crisis": True, "text": CRISIS_TEXT, "source": "built-in"})
        mode = body.get("mode")
        style = body.get("style", "clearer")
        energy = body.get("energy", "ok")
        size = body.get("size", "normal")
        try:
            system = build_prompt(mode, style, energy, size)
        except ValueError:
            return self.send_json(400, {"error": "Unknown option."})
        try:
            out = call_model(system, text)
            return self.send_json(200, {"crisis": False, "text": out, "source": "model"})
        except Exception:
            if mode == "breakdown":
                out = fallback_breakdown(text, energy == "low")
                return self.send_json(
                    200,
                    {
                        "crisis": False,
                        "text": out,
                        "source": "basic",
                        "note": "No local model found, so this is a simple split of your own words.",
                    },
                )
            return self.send_json(
                503,
                {
                    "error": "No local model is running. Start Ollama (or another local model server) "
                    "and try again. See the README."
                },
            )

    def add_checkin(self, body):
        try:
            energy, overwhelm, sensory = clamp(body["energy"]), clamp(body["overwhelm"]), clamp(body["sensory"])
        except Exception:
            return self.send_json(400, {"error": "Please set all three sliders."})
        note = str(body.get("note", ""))[:1000]
        with DB_LOCK, db() as c:
            c.execute(
                "INSERT INTO checkins (ts, energy, overwhelm, sensory, note) VALUES (?,?,?,?,?)",
                (time.strftime("%Y-%m-%d %H:%M"), energy, overwhelm, sensory, note),
            )
        resp = {"ok": True}
        if is_crisis(note):
            resp.update({"crisis": True, "text": CRISIS_TEXT})
        self.send_json(200, resp)

    def insights(self):
        with DB_LOCK, db() as c:
            rows = [dict(r) for r in c.execute("SELECT * FROM checkins ORDER BY id DESC LIMIT 14")]
        if len(rows) < 3:
            return {"enough": False, "message": "Insights appear after 3 check-ins. They are plain averages, nothing more."}

        def avg(rs, k):
            return round(sum(r[k] for r in rs) / len(rs), 1) if rs else None

        recent, earlier = rows[:7], rows[7:]
        out = {"enough": True, "count": len(rows), "recent": {}, "earlier": {}}
        for k in ("energy", "overwhelm", "sensory"):
            out["recent"][k] = avg(recent, k)
            out["earlier"][k] = avg(earlier, k) if earlier else None
        out["message"] = "Averages of your own check-ins (1 to 5). These are numbers, not advice or a diagnosis."
        return out


def main():
    check_remote_policy()
    init_db()
    srv = ThreadingHTTPServer(("127.0.0.1", CFG["port"]), Handler)
    print(f"Steady is running at http://127.0.0.1:{CFG['port']}")
    print(f"Model: {CFG['model_name']} at {CFG['model_url']}")
    print(f"Your data lives in: {DB_PATH}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
