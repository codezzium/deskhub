"""Snapmaker 2.0 (Luban HTTP API) support without a printer: message building from sample
status, the pairing probe, the worker against a tiny local HTTP server, and the CLI.

    uv run python tests/test_snapmaker.py
Prints OK or the problems found. Never touches the OS credential store.
"""
import builtins
import contextlib
import functools
import getpass
import io
import json
import pathlib
import socket
import sys
import tempfile
import threading
import time
import tomllib
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

problems = []


def check(cond, msg):
    if not cond:
        problems.append(msg)


store: dict = {}
kr = types.ModuleType("keyring")
kr.errors = types.SimpleNamespace(PasswordDeleteError=type("PasswordDeleteError", (Exception,), {}))
kr.set_password = lambda s, k, v: store.__setitem__((s, k), v)
kr.get_password = lambda s, k: store.get((s, k))


def _del(s, k):
    if (s, k) not in store:
        raise kr.errors.PasswordDeleteError()
    del store[(s, k)]


kr.delete_password = _del
sys.modules["keyring"] = kr

from deskhub import printer as pr  # noqa: E402
from deskhub import printer_poll as poll  # noqa: E402
from deskhub import printers_cli  # noqa: E402
from deskhub import snapmaker as sm  # noqa: E402
from deskhub.pages import enabled_pages  # noqa: E402


# ---- sample status (field names as Luban's SstpHttpChannel and community clients read them) ----
def status(**over):
    s = {"status": "RUNNING", "x": 10, "y": 20, "z": 3.2, "homed": True,
         "toolHead": "TOOLHEAD_3DPRINTING_1", "nozzleTemperature": 209.6, "nozzleTargetTemperature": 210,
         "heatedBedTemperature": 59.9, "heatedBedTargetTemperature": 60, "isFilamentOut": False,
         "isEnclosureDoorOpen": False, "workSpeed": 1500, "printStatus": "Printing",
         "fileName": "Bracket v2.gcode", "totalLines": 100000, "currentLine": 41000, "estimatedTime": 7200,
         "progress": 0.415, "elapsedTime": 3000, "remainingTime": 4230,
         "moduleList": {"enclosure": True, "rotaryModule": False, "emergencyStopButton": False,
                        "airPurifier": False}}
    s.update(over)
    return {k: v for k, v in s.items() if v is not None}


m = sm.build_message("A350", "Snapmaker A350", status())
check(m == {"name": "A350", "model": "Snapmaker A350", "online": True, "state": "RUNNING", "pct": 41,
            "remaining_min": 70, "layer": 0, "layers": 0, "file": "Bracket v2", "nozzle": 209.6,
            "nozzle_target": 210, "bed": 59.9, "bed_target": 60, "chamber": None, "speed": "",
            "error": "", "ams": []}, f"running: {m}")
json.dumps(m)

m = sm.build_message("S", "M", status(progress=0, currentLine=0, remainingTime=None, elapsedTime=0))
check(m["state"] == "PREPARE" and m["pct"] == 0 and m["file"] == "Bracket v2", f"heating: {m}")
m = sm.build_message("S", "M", status(status="PAUSED", isFilamentOut=True))
check(m["state"] == "PAUSE" and m["error"] == "Filament ran out" and m["remaining_min"] == 70, f"paused: {m}")
m = sm.build_message("S", "M", status(isEnclosureDoorOpen=True))
check(m["error"] == "Enclosure door is open", f"door open: {m}")
m = sm.build_message("S", "M", status(status="STOPPED"))
check(m["state"] == "IDLE" and m["file"] == "" and m["remaining_min"] == 0, f"stopped: {m}")
m = sm.build_message("S", "M", status(status="IDLE", progress=1, currentLine=100000))
check(m["state"] == "FINISH" and m["pct"] == 100 and m["file"] == "Bracket v2" and m["remaining_min"] == 0,
      f"finished: {m}")
m = sm.build_message("S", "M", status(status="IDLE", fileName=None, progress=None, currentLine=None,
                                      totalLines=None, nozzleTargetTemperature=0))
check(m["state"] == "IDLE" and m["pct"] == 0 and m["file"] == "" and m["nozzle"] == 209.6
      and m["nozzle_target"] == 0, f"idle: {m}")
m = sm.build_message("S", "M", status(status="SOMETHING_NEW", isEnclosureDoorOpen=True))
check(m["state"] == "IDLE" and m["error"] == "", f"unknown status: {m}")
m = sm.build_message("S", "M", status(remainingTime=None, progress=0.5, elapsedTime=3000))
check(m["remaining_min"] == 50, f"remaining from elapsed: {m}")
# Dual extruder: nozzleTemperature1/2, the heated one shown.
dual = status(nozzleTemperature=None, nozzleTargetTemperature=None, nozzleTemperature1=31, nozzleTargetTemperature1=0,
              nozzleTemperature2=239.5, nozzleTargetTemperature2=240)
m = sm.build_message("S", "M", dual)
check((m["nozzle"], m["nozzle_target"]) == (239.5, 240), f"dual, second active: {m}")
m = sm.build_message("S", "M", {**dual, "nozzleTargetTemperature2": 0})
check((m["nozzle"], m["nozzle_target"]) == (31, 0), f"dual, both cold: {m}")
m = sm.build_message("S", "M", status(nozzleTemperature=None, nozzleTargetTemperature=None,
                                      nozzle1Temperature=205, nozzle1TargetTemperature=205))
check((m["nozzle"], m["nozzle_target"]) == (205, 205), f"nozzle1Temperature spelling: {m}")
# Laser and CNC heads: state and progress, no temperatures.
for head in ("TOOLHEAD_LASER_1", "TOOLHEAD_CNC_1"):
    m = sm.build_message("S", "M", status(toolHead=head))
    check(m["state"] == "RUNNING" and m["pct"] == 41 and m["nozzle"] is None and m["bed"] is None
          and m["bed_target"] is None, f"{head}: {m}")
check(sm._model("Snapmaker 2.0 A350") == "Snapmaker A350" and sm._model("a250") == "Snapmaker A250"
      and sm._model("A350T") == "Snapmaker A350T" and sm._model(None) == "Snapmaker", "model from series")


# ---- a tiny Luban touchscreen ----------------------------------------------------
class Fake:
    status = status()
    approved: set = set()     # tokens the user tapped Yes for
    pending: dict = {}        # token -> status polls seen while the prompt is up
    sessions: set = set()     # tokens with a live connect
    approve_after = 2         # status polls before the user taps Yes; None: never
    decline = False           # the user taps No
    hang = 0.0                # seconds a status request takes (a slow or stuck screen)
    connects: list = []
    disconnects: list = []
    statuses: list = []
    n = 0


def fake_reset(**kw):
    Fake.approved, Fake.pending, Fake.sessions = set(), {}, set()
    Fake.approve_after, Fake.decline, Fake.hang = 2, False, 0.0
    Fake.connects, Fake.disconnects, Fake.statuses = [], [], []
    for k, v in kw.items():
        setattr(Fake, k, v)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body=None):
        data = json.dumps(body).encode() if body is not None else b""
        self.send_response(code)
        if data:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _form(self):
        n = int(self.headers.get("Content-Length") or 0)
        return {k: v[0] for k, v in parse_qs(self.rfile.read(n).decode()).items()}

    def do_POST(self):
        url, form = urlsplit(self.path), self._form()
        token = form.get("token", "")
        if url.path == "/api/v1/connect":
            Fake.connects.append(token)
            if not token:
                Fake.n += 1
                token = f"tok-{Fake.n}-secret"
                Fake.pending[token] = 0
            elif token not in Fake.approved and token not in Fake.pending:
                return self._send(401, {"msg": "Unauthorized"})
            Fake.sessions.add(token)
            return self._send(200, {"token": token, "readonly": False, "series": "Snapmaker 2.0 A350",
                                    "headType": 1, "hasEnclosure": True})
        if url.path == "/api/v1/disconnect":
            Fake.disconnects.append(token)
            Fake.sessions.discard(token)
            Fake.pending.pop(token, None)
            return self._send(200, {})
        self._send(404)

    def do_GET(self):
        url = urlsplit(self.path)
        if url.path != "/api/v1/status":
            return self._send(404)
        token = parse_qs(url.query).get("token", [""])[0]
        Fake.statuses.append(token)
        if Fake.hang:
            time.sleep(Fake.hang)
            return self._send(204)
        if token in Fake.pending:
            if Fake.decline:
                del Fake.pending[token]
                return self._send(401)
            Fake.pending[token] += 1
            if Fake.approve_after is None or Fake.pending[token] < Fake.approve_after:
                return self._send(204)
            del Fake.pending[token]
            Fake.approved.add(token)
        if token not in Fake.approved or token not in Fake.sessions:
            return self._send(401)
        self._send(200, Fake.status)


srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
PORT = srv.server_address[1]
HERE = {"host": "127.0.0.1", "port": PORT}


def wait_for(cond, secs=20):
    end = time.time() + secs
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def quiet_probe(*a, **k):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        r = sm.probe(*a, **k)
    return r, out.getvalue()


# ---- the probe: pairing --------------------------------------------------------
fake_reset(approve_after=3)
r, said = quiet_probe(HERE, None, wait=5, interval=0.05)
check(r.ok and r.secret == "tok-1-secret" and r.model == "Snapmaker A350", f"probe ok: {r}")
check("Tap Yes on the printer's touchscreen" in said, f"probe asks for the tap: {said!r}")
check(Fake.connects == [""] and Fake.statuses == ["tok-1-secret"] * 3 and Fake.disconnects == [],
      f"probe flow: {Fake.connects} {Fake.statuses} {Fake.disconnects}")

fake_reset(approve_after=None)
t = time.monotonic()
r, _ = quiet_probe(HERE, None, wait=0.3, interval=0.05)
check(not r.ok and r.secret is None and "Nobody tapped Yes" in r.message and time.monotonic() - t < 10,
      f"probe timeout: {r}")
check(Fake.disconnects == ["tok-2-secret"] and not Fake.pending, "timeout clears the prompt")

fake_reset(decline=True)
r, _ = quiet_probe(HERE, None, wait=5, interval=0.05)
check(not r.ok and r.secret is None and "turned the connection down" in r.message, f"probe declined: {r}")

r, _ = quiet_probe({"host": "127.0.0.1", "port": 1}, None, timeout=1, wait=1)
check(not r.ok and "8080" in r.message and "J1" not in r.message, f"probe unreachable: {r}")
sacp = socket.socket()
sacp.bind(("127.0.0.1", 0))
sacp.listen()
sm.SACP_PORT, real_sacp = sacp.getsockname()[1], sm.SACP_PORT
r, _ = quiet_probe({"host": "127.0.0.1", "port": 1}, None, timeout=1, wait=1)
check(not r.ok and "J1" in r.message and "SACP" in r.message, f"probe on a J1: {r}")
sm.SACP_PORT = real_sacp
sacp.close()

# ---- the worker ----------------------------------------------------------------
TOKEN = "tok-stored-777"
KEY = (poll.SERVICE, poll.secret_key("snapmaker", "127.0.0.1", PORT))
fake_reset(approved={TOKEN}, status=status())
store[KEY] = TOKEN
logs = []
handler = type("H", (poll.logging.Handler,), {"emit": lambda self, rec: logs.append(rec.getMessage())})()
poll.log.addHandler(handler)
poll.log.setLevel(poll.logging.INFO)  # for the "connected" lines
sm.SnapmakerWorker.POLL_S = 0.05
sm.SnapmakerWorker.RETRY_S = 0.05
sm.SnapmakerWorker.TIMEOUT = 1.0  # a hung status request (Fake.hang) times out
entry = {"name": "A350", "type": "snapmaker", "host": "127.0.0.1", "port": PORT, "model": "Snapmaker A350"}
svc = pr.PrinterService([entry], secret_getter=lambda s, k: store.get((s, k)))
check(type(svc.workers[0]).__name__ == "SnapmakerWorker", "snapmaker worker picked by type")
svc.start()
check(wait_for(lambda: svc.message()["printers"][0]["state"] == "RUNNING"), "snapmaker: state arrives")
p = svc.message()["printers"][0]
check(p["pct"] == 41 and p["file"] == "Bracket v2" and p["model"] == "Snapmaker A350", f"service message: {p}")
Fake.status = status(status="PAUSED")
check(wait_for(lambda: svc.message()["printers"][0]["state"] == "PAUSE"), "snapmaker: update arrives")
check(Fake.connects == [TOKEN], f"one connect per session: {Fake.connects}")

def connects_settle(secs=0.5):
    """The connect count once it stops changing for `secs` (polling every 0.05 s), or after 20 s."""
    n, since = len(Fake.connects), time.monotonic()
    give_up = since + 20
    while time.monotonic() - since < secs and time.monotonic() < give_up:
        time.sleep(0.05)
        if len(Fake.connects) != n:
            n, since = len(Fake.connects), time.monotonic()
    return n


# The printer drops the session: 401 on status, the worker connects again.
Fake.sessions.clear()
check(wait_for(lambda: len(Fake.connects) == 2 and svc.message()["printers"][0]["online"]
               and Fake.sessions == {TOKEN}), f"reconnect after a dropped session: {Fake.connects}")
# The worker logs "connected" after the first good status of the new session, and that ends the
# outage; a prompt shown before then would be the same outage and get no hint of its own.
check(wait_for(lambda: logs[-1:] == ["printer A350: connected"]), f"outage over: {logs}")
# The printer forgot the token and puts up a prompt for it: tapping Yes brings it back.
logs.clear()
Fake.approve_after = None
Fake.pending[TOKEN] = 0
check(wait_for(lambda: any("tap Yes" in line for line in logs)), f"waiting for approval hint: {logs}")
connects = len(Fake.connects)
# A status request that times out while the prompt is up doesn't connect again (that would
# put up a new prompt).
w = svc.workers[0]
Fake.hang = 2.0
n_status = len(Fake.statuses)
check(wait_for(lambda: len(Fake.statuses) >= n_status + 3), "status keeps being asked while it times out")
Fake.hang = 0.0
check(connects_settle() == connects, f"no new connect while the prompt is up: {Fake.connects}")
Fake.approve_after = 1
check(wait_for(lambda: logs[-1:] == ["printer A350: connected"]), f"back after the tap: {logs}")
check(len(Fake.connects) == connects, "the tap needed no new connect")
svc.stop()
w.join(20)
check(Fake.disconnects == [TOKEN], f"disconnect on stop: {Fake.disconnects}")

# The token is revoked (or No was tapped): one hint, one try to connect, then no more prompts.
fake_reset(approved={TOKEN}, status=status())
logs.clear()
svc = pr.PrinterService([entry], secret_getter=lambda s, k: store.get((s, k)))
svc.start()
check(wait_for(lambda: svc.message()["printers"][0]["online"]), "second service online")
Fake.approved.clear()
Fake.sessions.clear()
check(wait_for(lambda: any(sm.TOKEN_HINT in line for line in logs)), f"401 hint: {logs}")
check(connects_settle() == 2, f"rejected token: connect stops: {Fake.connects}")
check(svc.workers[0]._rejected, "rejection latched")
Fake.approved.add(TOKEN)  # even if it would work now, nothing is sent until a restart
check(connects_settle() == 2, f"latched until a restart: {Fake.connects}")
check(sum(sm.TOKEN_HINT in line for line in logs) == 1, f"hint logged once: {logs}")
check(not any(TOKEN in line for line in logs), "token not logged")
svc.stop()
svc.workers[0].join(20)

# No token stored: no connect (that would ask for a new token on the touchscreen).
fake_reset()
store.clear()
logs.clear()
notoken = pr.PrinterService([entry], secret_getter=lambda s, k: store.get((s, k)))
notoken.start()
check(wait_for(lambda: any("no Snapmaker token" in line for line in logs)), f"no token: {logs}")
check(Fake.connects == [] and notoken.message()["printers"][0]["state"] == "OFFLINE", "no token: nothing sent")
notoken.stop()

# Unreachable: offline, one log line, no crash.
logs.clear()
dead = pr.PrinterService([{"name": "Gone", "type": "snapmaker", "host": "127.0.0.1", "port": 1}],
                         secret_getter=lambda s, k: "x")
dead.start()
check(wait_for(lambda: len(logs) >= 1), "unreachable logs a line")
time.sleep(0.3)
check(len(logs) == 1 and dead.message()["printers"][0]["state"] == "OFFLINE", f"unreachable: {logs}")
dead.stop()
poll.log.removeHandler(handler)
check("printer" in enabled_pages({"printers": [entry]}), "printer page auto-added for snapmaker")

# ---- the CLI --------------------------------------------------------------------
tmp = pathlib.Path(tempfile.mkdtemp()) / "config.toml"
tmp.write_text('# my config\n[hub]\nhost = "x"\n', encoding="utf-8")
check(sm.SPEC.secret_prompt is None and sm.SPEC.default_port == 8080 and "U1" in sm.SPEC.intro
      and "Moonraker" in sm.SPEC.intro, "spec")
sm.SPEC.probe = functools.partial(sm.probe, wait=5, interval=0.05)
ADD = functools.partial(printers_cli.cmd_add_polled, spec=sm.SPEC)


def run(cmd, answers):
    a = iter(answers)
    builtins.input = lambda prompt="": next(a)
    getpass.getpass = lambda prompt="": problems.append("asked for a secret") or ""
    with contextlib.redirect_stdout(io.StringIO()) as out:
        cmd(tmp)
    return out.getvalue()


fake_reset(approve_after=2)
said = run(ADD, ["127.0.0.1", str(PORT), "", ""])
cfg = tomllib.loads(tmp.read_text())
token = store.get((poll.SERVICE, poll.secret_key("snapmaker", "127.0.0.1", PORT)))
check(cfg["printers"] == [{"name": "Snapmaker A350", "type": "snapmaker", "host": "127.0.0.1", "port": PORT,
                           "model": "Snapmaker A350"}], f"snapmaker saved: {cfg.get('printers')}")
check(token and token.startswith("tok-") and token not in tmp.read_text() and token not in said,
      f"token in the credential store only: {token}")
check("Tap Yes" in said, "CLI shows the tap prompt")
check("Snapmaker 127.0.0.1" in printers_cli.describe(cfg["printers"][0]), "describe")
run(printers_cli.cmd_remove, ["1", "y"])
check(not store and not tomllib.loads(tmp.read_text()).get("printers"), "remove deletes the token")

srv.shutdown()
print("\nPROBLEMS:" if problems else "\nOK", problems if problems else "")
