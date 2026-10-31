#!/usr/bin/env python3
"""End-to-end test using a fake local model. Run: python3 tests/test_e2e.py"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODEL_PORT, APP_PORT = 18434, 18787
model_calls = []


class FakeModel(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
        self.wfile.write(b'{"data":[]}')

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        model_calls.append(body)
        out = {"choices": [{"message": {"content": "1. Open the letter.\n2. Read the first line.\nGood enough version: open it."}}]}
        data = json.dumps(out).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
        self.wfile.write(data)


def req(path, method="GET", body=None, headers=None, host=None):
    h = {"X-Steady": "1", "Content-Type": "application/json"}
    h.update(headers or {})
    if host:
        h["Host"] = host
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(f"http://127.0.0.1:{APP_PORT}{path}", data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def start_app(cfg, tmp):
    p = Path(tmp) / "config.json"
    p.write_text(json.dumps(cfg))
    env = dict(os.environ, STEADY_CONFIG=str(p))
    proc = subprocess.Popen([sys.executable, str(ROOT / "server.py")], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return proc


def wait_up():
    for _ in range(50):
        try:
            req("/api/status")
            return
        except Exception:
            time.sleep(0.1)
    raise RuntimeError("server did not start")


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        check.failed = True
check.failed = False


def main():
    fm = HTTPServer(("127.0.0.1", MODEL_PORT), FakeModel)
    threading.Thread(target=fm.serve_forever, daemon=True).start()
    tmp = tempfile.mkdtemp()
    cfg = {"port": APP_PORT, "model_url": f"http://127.0.0.1:{MODEL_PORT}/v1", "model_name": "fake", "data_dir": str(Path(tmp) / "data")}

    # 1. Remote model refused unless explicitly allowed
    bad = start_app(dict(cfg, model_url="http://example.com/v1"), tmp)
    out = bad.communicate(timeout=10)[0].decode()
    check("refuses to start with a remote model by default", bad.returncode != 0 and "Refusing to start" in out)

    proc = start_app(cfg, tmp)
    try:
        wait_up()
        s, j = req("/api/status")
        check("status reports model ready", s == 200 and j["model_ready"] is True)

        s, j = req("/api/assist", "POST", {"mode": "breakdown", "text": "deal with the council letter", "energy": "low"})
        check("breakdown uses local model", s == 200 and j["source"] == "model" and "1. Open the letter." in j["text"])
        check("low energy reaches the prompt", "Energy today: low" in model_calls[-1]["messages"][0]["content"])
        check("safety text is in the system prompt", "1737" in model_calls[-1]["messages"][0]["content"])

        n = len(model_calls)
        s, j = req("/api/assist", "POST", {"mode": "breakdown", "text": "I want to kill myself"})
        check("crisis text returns resources", j.get("crisis") is True and "1737" in j["text"])
        check("crisis text never reaches the model", len(model_calls) == n)

        for style in ("clearer", "softer", "decode"):
            s, j = req("/api/assist", "POST", {"mode": "plain", "style": style, "text": "Can we talk later?"})
            check(f"plain mode '{style}' works", s == 200 and j["source"] == "model")

        s, j = req("/api/assist", "POST", {"mode": "plain", "style": "nope", "text": "hi"})
        check("unknown style rejected", s == 400)
        s, j = req("/api/assist", "POST", {"mode": "breakdown", "text": "x" * 5000})
        check("overlong input rejected", s == 400)

        s, j = req("/api/assist", "POST", {"mode": "breakdown", "text": "x"}, headers={"X-Steady": "0"})
        check("requests without header refused", s == 403)
        s, j = req("/api/status", host="evil.example.com")
        check("wrong Host header refused", s == 403)

        s, j = req("/api/checkins", "POST", {"energy": 2, "overwhelm": 4, "sensory": 5, "note": "loud day"})
        check("check-in saved", s == 200 and j["ok"])
        for i in range(3):
            req("/api/checkins", "POST", {"energy": 3, "overwhelm": 3, "sensory": 3})
        s, j = req("/api/checkins")
        check("check-ins listed", len(j["checkins"]) == 4)
        s, j = req("/api/insights")
        check("insights computed", j["enough"] is True)
        s, j = req("/api/checkins", "POST", {"energy": 2, "overwhelm": 4, "sensory": 5, "note": "I want to die"})
        check("crisis in check-in note shows resources", j.get("crisis") is True)

        s, j = req("/api/netlog")
        check("outbound log only lists loopback", s == 200 and j["netlog"] and all(r["host"] == "127.0.0.1" for r in j["netlog"]))
        s, j = req("/api/export")
        check("export contains check-ins", len(j["checkins"]) == 5)

        s, j = req("/api/wipe", "POST", {"confirm": "no"})
        check("wipe needs confirmation", s == 400)
        s, j = req("/api/wipe", "POST", {"confirm": "DELETE"})
        s2, j2 = req("/api/checkins")
        check("wipe deletes everything", j["ok"] and j2["checkins"] == [])
    finally:
        proc.terminate()
        fm.shutdown()

    # 2. Basic mode without a model
    cfg2 = dict(cfg, model_url="http://127.0.0.1:1/v1")
    proc2 = start_app(cfg2, tmp)
    try:
        wait_up()
        s, j = req("/api/assist", "POST", {"mode": "breakdown", "text": "wash dishes, call the dentist and then tidy desk"})
        check("basic mode splits steps with no model", s == 200 and j["source"] == "basic" and "Call the dentist" in j["text"])
        s, j = req("/api/assist", "POST", {"mode": "plain", "style": "clearer", "text": "hello"})
        check("plain mode reports no model clearly", s == 503 and "No local model" in j["error"])
    finally:
        proc2.terminate()

    print("\nALL PASSED" if not check.failed else "\nSOME FAILED")
    sys.exit(1 if check.failed else 0)


if __name__ == "__main__":
    main()
