"""Creality (stock firmware, port 9999) printer support without a printer: state mapping from
sample states, merging partial updates, CFS colours, the worker and probe against a tiny local
websocket server, and the CLI.

    uv run python tests/test_creality.py
Prints OK or the problems found. Never touches the OS credential store.
"""
import builtins
import functools
import getpass
import json
import pathlib
import sys
import tempfile
import threading
import time
import tomllib
import types

from websockets.sync.server import serve

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

from deskhub import creality as cr  # noqa: E402
from deskhub import printer as pr  # noqa: E402
from deskhub import printer_poll as poll  # noqa: E402
from deskhub import printers_cli  # noqa: E402
from deskhub.pages import enabled_pages  # noqa: E402


# ---- sample state (a K1C mid-print, shaped like ha_creality_ws's capture: numbers as strings) --
def full(**kw):
    st = {"TotalLayer": 250, "layer": 60, "bedTemp0": "59.800000", "targetBedTemp0": 60,
          "nozzleTemp": "244.960000", "targetNozzleTemp": 245, "boxTemp": "31.2", "maxBoxTemp": 60,
          "curFeedratePct": 130, "deviceState": 1, "err": {"errcode": 0, "key": 0, "value": ""},
          "hostname": "K1C-1A2B", "model": "K1C", "printFileName": "/usr/data/printer_data/gcodes/Bracket v2.gcode",
          "printJobTime": 1800, "printLeftTime": 5400, "printProgress": 31, "state": 1, "withSelfTest": 100,
          "cfsConnect": 0, "historyList": [{"filename": "old.gcode"}] * 3}
    st.update(kw)
    return st


def msg(**kw):
    return cr.build_message("K1C", "K1C", full(**kw))


m = msg()
check(m == {"name": "K1C", "model": "K1C", "online": True, "state": "RUNNING", "pct": 31,
            "remaining_min": 90, "layer": 60, "layers": 250, "file": "Bracket v2", "nozzle": 245,
            "nozzle_target": 245, "bed": 59.8, "bed_target": 60, "chamber": 31.2, "speed": "130%",
            "error": "", "ams": []}, f"printing: {m}")
json.dumps(m)

# `state` codes (Creality Print's device manager; 9 from ha_creality_ws's K1C capture).
for code, want in [(0, "IDLE"), (1, "RUNNING"), (2, "FINISH"), (3, "FAILED"), (4, "IDLE"), (5, "PAUSE"),
                   (6, "PAUSE"), (7, "IDLE"), (8, "RUNNING"), (9, "PREPARE"), (42, "IDLE")]:
    got = msg(state=code)["state"]
    check(got == want, f"state {code}: {got}, want {want}")
m = msg(state=5)
check(m["pct"] == 31 and m["remaining_min"] == 90 and m["file"] == "Bracket v2" and m["error"] == "",
      f"paused keeps the job: {m}")
m = msg(state="1")
check(m["state"] == "RUNNING", f"state as a string: {m}")
# 100% with time left is still printing (a K1C does that for a while before it ends the job).
check(msg(printProgress=100, printLeftTime=86)["state"] == "RUNNING", "100% with time left")

# Start-of-print self-test: preparing, with the job; from the screen while idle: no job.
m = msg(withSelfTest=3, printLeftTime=0, TotalLayer=0, layer=0, printProgress=0)
check(m["state"] == "PREPARE" and m["file"] == "Bracket v2", f"self-test at print start: {m}")
m = msg(state=0, withSelfTest=40)
check(m["state"] == "PREPARE" and m["file"] == "" and m["pct"] == 0 and m["layers"] == 0 and m["speed"] == "",
      f"self-test while idle shows no job: {m}")
check(msg(state=7, withSelfTest=2)["state"] == "IDLE", "cancelled during the self-test")

# Finished, failed: the job shows. Idle, cancelled, stopping: none of the old job.
m = msg(state=2, printProgress=100, printLeftTime=0)
check(m["state"] == "FINISH" and m["pct"] == 100 and m["file"] == "Bracket v2" and m["remaining_min"] == 0
      and m["speed"] == "" and (m["layer"], m["layers"]) == (60, 250), f"complete: {m}")
m = msg(state=3)
check(m["state"] == "FAILED" and m["error"] == "Print stopped by an error" and m["file"] == "Bracket v2",
      f"failed: {m}")
m = msg(state=3, err={"errcode": 2004, "key": 4, "value": ""})
check(m["error"] == "Error 2004", f"failed with a code: {m}")
m = msg(state=5, err={"errcode": 2839, "key": 2839, "value": "Filament runout"})
check(m["state"] == "PAUSE" and m["error"] == "Filament runout", f"paused with a message: {m}")
m = msg(err={"errcode": 115, "key": 115, "value": ""})
check(m["state"] == "RUNNING" and m["error"] == "", f"a lingering code while printing is not shown: {m}")
for code in (0, 4, 7):
    m = msg(state=code, printProgress=100 if code == 0 else 47)
    check(m["state"] == "IDLE" and m["pct"] == 0 and m["file"] == "" and (m["layer"], m["layers"]) == (0, 0)
          and m["remaining_min"] == 0 and m["speed"] == "" and m["nozzle"] == 245,
          f"state {code} shows no old job: {m}")

# No printProgress: from layers. File names.
st = full(TotalLayer=200, layer=50)
del st["printProgress"]
check(cr.build_message("K", "K", st)["pct"] == 25, "progress from layers")
check(cr.build_message("K", "K", full(printFileName="/mnt/UDISK/printer_data/gcodes/Cube_PLA.gcode.3mf"))["file"]
      == "Cube_PLA", "3mf file name")

# No chamber sensor (maxBoxTemp 0): boxTemp is left out.
check(msg(maxBoxTemp=0, boxTemp=0)["chamber"] is None, "no chamber sensor")
check(msg(maxBoxTemp=None)["chamber"] == 31.2, "maxBoxTemp unknown keeps boxTemp")

# Malformed and non-finite values don't crash or leak through.
m = cr.build_message("K", "X", {"state": "junk", "nozzleTemp": "nan", "bedTemp0": float("inf"),
                                "targetNozzleTemp": "hot", "printProgress": float("nan"), "layer": "nan",
                                "TotalLayer": float("inf"), "printLeftTime": "inf", "err": "junk",
                                "boxsInfo": "junk", "curFeedratePct": float("nan")})
check(m["state"] == "IDLE" and m["nozzle"] is None and m["bed"] is None and m["nozzle_target"] is None
      and m["ams"] == [], f"junk: {m}")
m = msg(printProgress="nan", printLeftTime=float("inf"), layer=float("nan"), curFeedratePct="inf")
check(m["state"] == "RUNNING" and m["remaining_min"] == 0 and m["speed"] == "" and m["layer"] == 0
      and m["pct"] == 0, f"NaN while printing: {m}")
json.dumps(m)
check(cr.build_message("K", "X", {})["state"] == "IDLE", "empty state")
check(cr.build_message("K", "X", {"err": {"errcode": "nan"}, "state": 3})["error"] == "Print stopped by an error",
      "NaN error code")

# Partial updates merge into the running state; nested values replace; big keys are dropped.
st = {}
cr.merge(st, full())
cr.merge(st, {"nozzleTemp": "200.100000", "bedTemp0": "58.000000"})
cr.merge(st, {"printProgress": 40, "layer": 80})
check(st["nozzleTemp"] == "200.100000" and st["printFileName"].endswith("Bracket v2.gcode") and st["layer"] == 80
      and "historyList" not in st, f"merge: {st}")
cr.merge(st, {"err": {"errcode": 0}})
check(st["err"] == {"errcode": 0}, "nested value replaced")
m = cr.build_message("K", "K", st)
check(m["nozzle"] == 200.1 and m["pct"] == 40 and m["layer"] == 80 and m["state"] == "RUNNING", f"merged: {m}")
cr.merge(st, {"state": 4})
m = cr.build_message("K", "K", st)
check(m["state"] == "IDLE" and m["file"] == "" and m["pct"] == 0, f"cancel after merge: {m}")

# Model names.
for raw, want in [("F012", "K2 Pro"), ("F008", "K2 Plus"), ("F021", "K2"), ("F018", "Hi"), ("F005", "Ender-3 V3 KE"),
                  ("F001", "Ender-3 V3"), ("CR-K1 Max", "K1 Max"), ("K1C", "K1C"), ("K1 SE", "K1 SE"), ("", "")]:
    check(cr.model_name(raw) == want, f"model {raw!r}: {cr.model_name(raw)!r}")


# ---- CFS (K2 with one unit; boxsInfo shaped like OrcaSlicer and ha_creality_ws read it) -------
def boxes(cfs_state=1, ext=None):
    holder = {"id": 0, "state": 0, "type": 1, "materials": [ext or {
        "id": 0, "type": "", "name": "", "color": "", "state": 0, "selected": 0}]}
    unit = {"id": 1, "state": cfs_state, "type": 0, "temp": 28, "humidity": 40, "materials": [
        {"id": 3, "vendor": "Generic", "type": "PETG", "color": "#0ffa800,#0ff97e1", "state": 1, "selected": 0},
        {"id": 0, "vendor": "Creality", "type": "PLA", "color": "#0000000", "state": 1, "selected": 0},
        {"id": 1, "vendor": "Creality", "type": "PLA-CF", "color": "#0ffffff", "state": 1, "selected": 1},
        {"id": 2, "vendor": "", "type": "", "color": "", "state": 0, "selected": 0}]}
    return {"boxsInfo": {"same_material": [], "materialBoxs": [holder, unit]}}


k2 = {**full(model="F012", cfsConnect=1), **boxes()}
m = cr.build_message("K2", "K2 Pro", k2)
check(m["ams"] == [{"color": "#000000", "type": "PLA", "active": False},
                   {"color": "#FFFFFF", "type": "PLA-CF", "active": True},
                   {"color": "#FFA800", "type": "PETG", "active": False}], f"CFS slots: {m['ams']}")
m = cr.build_message("K2", "K2 Pro", {**full(cfsConnect=1), **boxes(ext={
    "id": 0, "type": "TPU", "color": "#01b04ae", "state": 1, "selected": 0})})
check(len(m["ams"]) == 4 and m["ams"][3] == {"color": "#1B04AE", "type": "TPU", "active": False},
      f"external spool after the CFS: {m['ams']}")
check(cr.build_message("K", "K", {**full(cfsConnect=0), **boxes()})["ams"] == [], "CFS unplugged")
check(cr.build_message("K", "K", {**full(cfsConnect=1), **boxes(cfs_state=0)})["ams"] == [], "inactive CFS box")
m = cr.build_message("K", "K", {**full(cfsConnect=0), **boxes(ext={
    "id": 0, "type": "PLA", "color": "#1b04ae", "state": 1, "selected": 1})})
check(m["ams"] == [{"color": "#1B04AE", "type": "PLA", "active": True}], f"spool holder only: {m['ams']}")
m = cr.build_message("K", "K", {"cfsConnect": 1, "boxsInfo": {"materialBoxs": [
    {"id": i, "state": 1, "type": 0, "materials": [
        {"id": j, "type": "PLA", "color": "#0123456", "state": 1, "selected": 0} for j in range(4)]}
    for i in (2, 1, 3)]}})
check(len(m["ams"]) == 8, f"clipped to 8: {len(m['ams'])}")
m = cr.build_message("K", "K", {"cfsConnect": 1, "boxsInfo": {"materialBoxs": [
    {"id": i, "state": 1, "type": 0, "materials": [
        {"id": j, "type": f"T{i}{j}", "color": "#0123456", "state": 1, "selected": int(i == 3 and j == 2)}
        for j in range(4)]} for i in (1, 2, 3)]}})
check(len(m["ams"]) == 8 and [s["type"] for s in m["ams"] if s["active"]] == ["T32"],
      f"active slot on unit 3 kept: {m['ams']}")
m = cr.build_message("K", "K", {"cfsConnect": 1, "boxsInfo": {"materialBoxs": [
    {"id": 1, "state": 1, "type": 0, "materials": [{"id": 0, "type": "PLA", "color": "zz", "state": 1},
                                                   {"id": 1, "type": None, "color": "#0abc", "state": 1},
                                                   {"id": 2, "type": "PLA", "color": "#0123456"}, "junk"]},
    "junk", {"id": 2, "type": 0, "state": 1, "materials": "junk"}]}})
check(m["ams"] == [{"color": "#808080", "type": "PLA", "active": False}], f"odd boxes: {m['ams']}")


# ---- a tiny K2 Pro on port 9999 ---------------------------------------------------------
class Fake:
    state = {**full(model="F012", hostname="K2Pro-1A2B", cfsConnect=1)}
    boxes = boxes()
    push_on_connect = True
    answer_get = True
    drop_next = False
    connections = 0
    oks = 0
    gets: list = []


def handler(ws):
    Fake.connections += 1
    if Fake.push_on_connect:
        ws.send(json.dumps(Fake.state))
    ws.send(json.dumps({"ModeCode": "heart_beat"}))
    ws.send("ok")  # the printer's own bare "ok" must be skipped
    for raw in ws:
        if raw == "ok":
            Fake.oks += 1
            continue
        req = json.loads(raw)
        Fake.gets.append(req)
        if Fake.drop_next:
            Fake.drop_next = False
            ws.close()
            return
        params = req.get("params", {})
        if not Fake.answer_get:
            continue
        if params.get("ReqPrinterPara"):
            ws.send(json.dumps(Fake.state))
            ws.send(json.dumps({"nozzleTemp": "245.100000"}))  # a partial push in between
        if params.get("boxsInfo"):
            ws.send(json.dumps(Fake.boxes))


srv = serve(handler, "127.0.0.1", 0, compression=None)
threading.Thread(target=srv.serve_forever, daemon=True).start()
PORT = srv.socket.getsockname()[1]

# Something that is a websocket but not a printer.
quiet = serve(lambda ws: [None for _ in ws], "127.0.0.1", 0, compression=None)
threading.Thread(target=quiet.serve_forever, daemon=True).start()
QUIET = quiet.socket.getsockname()[1]


def wait_for(cond, secs=8):
    end = time.time() + secs
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


cr.CrealityWorker.POLL_S = 0.1
cr.CrealityWorker.RETRY_S = 0.1
cr.ASK_S = 0.3
entry = {"name": "K2", "type": "creality", "host": "127.0.0.1", "port": PORT, "model": "K2 Pro"}
svc = pr.PrinterService([entry, {"name": "x", "type": "creality"}], secret_getter=lambda s, k: store.get((s, k)))
check(len(svc.workers) == 1 and type(svc.workers[0]).__name__ == "CrealityWorker", "worker built, no-host skipped")
svc.start()
check(wait_for(lambda: svc.message()["printers"][0]["state"] == "RUNNING"), "creality: state arrives")
check(wait_for(lambda: len(svc.message()["printers"][0]["ams"]) == 3), "creality: CFS arrives")
p = svc.message()["printers"][0]
check(p["chamber"] == 31.2 and p["layers"] == 250 and p["model"] == "K2 Pro" and p["remaining_min"] == 90
      and p["nozzle"] == 245.1 and p["ams"][1]["active"], f"service message: {p}")
check(Fake.oks >= 1, "answers the heartbeat with ok")
check(any(g == {"method": "get", "params": {"ReqPrinterPara": 1}} for g in Fake.gets)
      and any(g == {"method": "get", "params": {"boxsInfo": 1}} for g in Fake.gets), f"requests: {Fake.gets[:3]}")
Fake.state = {**Fake.state, "state": 5}
check(wait_for(lambda: svc.message()["printers"][0]["state"] == "PAUSE"), "creality: update arrives")
check(Fake.connections == 1, f"websocket kept open between polls: {Fake.connections}")

# The printer drops the connection: reconnect and keep going.
logs = []
handler_ = type("H", (poll.logging.Handler,), {"emit": lambda self, rec: logs.append(rec.getMessage())})()
poll.log.addHandler(handler_)
Fake.drop_next = True
check(wait_for(lambda: Fake.connections >= 2), f"reconnects after a drop: {Fake.connections}")
Fake.state = {**Fake.state, "state": 2, "printProgress": 100, "printLeftTime": 0}
check(wait_for(lambda: svc.message()["printers"][0]["state"] == "FINISH"), "state after reconnect")
w = svc.workers[0]
check(w.message(now=time.monotonic() + pr.STALE_S + 1)["state"] == "OFFLINE", "stale -> OFFLINE")
svc.stop()
w.join(3)

# A printer that waits to be asked (nothing pushed on connect) still works.
Fake.push_on_connect = False
Fake.state = full(model="F012")
asked = pr.PrinterService([entry], secret_getter=lambda s, k: None)
asked.start()
check(wait_for(lambda: asked.message()["printers"][0]["state"] == "RUNNING"), "works when only asked")
asked.stop()
asked.workers[0].join(3)
Fake.push_on_connect = True

# A websocket that never sends status: offline, one log line, no crash.
logs.clear()
mute = pr.PrinterService([{"name": "Mute", "type": "creality", "host": "127.0.0.1", "port": QUIET}],
                         secret_getter=lambda s, k: None)
mute.start()
check(wait_for(lambda: len(logs) >= 1, 12), "silent websocket logs a line")
check(mute.message()["printers"][0]["state"] == "OFFLINE" and "no status" in logs[0], f"silent: {logs}")
mute.stop()

# Unreachable: offline, one log line, no crash.
logs.clear()
dead = pr.PrinterService([{"name": "Gone", "type": "creality", "host": "127.0.0.1", "port": 1}],
                         secret_getter=lambda s, k: None)
dead.start()
check(wait_for(lambda: len(logs) >= 1, 12), "unreachable logs a line")
time.sleep(0.5)
check(len(logs) == 1 and dead.message()["printers"][0]["state"] == "OFFLINE", f"unreachable: {logs}")
dead.stop()
poll.log.removeHandler(handler_)

# ---- probe ----------------------------------------------------------------------------
Fake.state = full(model="F012", hostname="K2Pro-1A2B")
r = cr.probe({"host": "127.0.0.1", "port": PORT}, None)
check(r.ok and r.name == "K2Pro-1A2B" and r.model == "K2 Pro" and r.secret is None and "K2 Pro" in r.message,
      f"probe: {r}")
Fake.state = full(model="CR-K1 Max", hostname="K1Max-0000")
Fake.push_on_connect = False
r = cr.probe({"host": "127.0.0.1", "port": PORT}, None)
check(r.ok and r.name == "K1Max-0000" and r.model == "K1 Max", f"probe when asked: {r}")
Fake.push_on_connect = True
r = cr.probe({"host": "127.0.0.1", "port": QUIET}, None, timeout=1)
check(not r.ok and "no printer status" in r.message, f"probe: not a printer: {r}")
r = cr.probe({"host": "127.0.0.1", "port": 1}, None, timeout=1)
check(not r.ok and "No websocket" in r.message and "127.0.0.1:1" in r.message, f"probe unreachable: {r}")

check("printer" in enabled_pages({"printers": [entry]}), "printer page auto-added for creality")

# ---- the CLI --------------------------------------------------------------------------
tmp = pathlib.Path(tempfile.mkdtemp()) / "config.toml"
tmp.write_text('# my config\n[hub]\nhost = "x"\n', encoding="utf-8")
ADD = functools.partial(printers_cli.cmd_add_polled, spec=cr.SPEC)


def run(cmd, answers, passwords):
    a, p = iter(answers), iter(passwords)
    builtins.input = lambda prompt="": next(a)
    getpass.getpass = lambda prompt="": next(p)
    cmd(tmp)


cr.DEFAULT_PORT = PORT  # the CLI never asks for a port; point the probe at the fake
Fake.state = full(model="F012", hostname="K2Pro-1A2B")
run(ADD, ["127.0.0.1", "", ""], [])  # host, then accept the suggested name and model
cfg = tomllib.loads(tmp.read_text())
check(cfg["printers"] == [{"name": "K2Pro-1A2B", "type": "creality", "host": "127.0.0.1", "model": "K2 Pro"}],
      f"creality saved: {cfg.get('printers')}")
check(not store, f"no credential stored: {store}")
check("Creality 127.0.0.1:9999" in printers_cli.describe(cfg["printers"][0]), "describe")
run(ADD, ["127.0.0.1", "", ""], [])
check(len(tomllib.loads(tmp.read_text())["printers"]) == 1, "duplicate host refused")
cr.SPEC.probe = lambda *a, **k: poll.ProbeResult(False, "No answer (stubbed)")
run(ADD, ["192.168.1.91", "y", "", ""], [])
cfg = tomllib.loads(tmp.read_text())
check(cfg["printers"][1] == {"name": "Creality", "type": "creality", "host": "192.168.1.91"},
      f"default model left out: {cfg['printers'][1]}")
check(not store, "still no credential")

srv.shutdown()
quiet.shutdown()
print("\nPROBLEMS:" if problems else "\nOK", problems if problems else "")
