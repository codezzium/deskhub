"""Anycubic LAN Mode support without a printer: report mapping, the AES and signature of the
handshake, the worker and probe against a fake printer (HTTP on one port, a tiny MQTT broker
on another), and the CLI.

    uv run python tests/test_anycubic.py
Prints OK or the problems found. Never touches the OS credential store.
"""
import asyncio
import base64
import builtins
import functools
import getpass
import hashlib
import json
import pathlib
import re
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

from deskhub import anycubic as ac  # noqa: E402
from deskhub import printer as pr  # noqa: E402
from deskhub import printer_poll as poll  # noqa: E402
from deskhub import printers_cli  # noqa: E402
from deskhub.pages import enabled_pages  # noqa: E402


# ---- AES and the signature ---------------------------------------------------------
def encrypt_block(block: bytes, keys) -> bytes:
    """The forward cipher, only to seal the fake printer's answer."""
    def xtime(a):
        return ((a << 1) ^ 0x1B) & 0xFF if a & 0x80 else a << 1
    s = [b ^ k for b, k in zip(block, keys[0])]
    for rnd in range(1, 11):
        s = [ac._SBOX[s[r + 4 * ((c + r) % 4)]] for c in range(4) for r in range(4)]
        if rnd < 10:
            out = []
            for c in range(0, 16, 4):
                a = s[c:c + 4]
                out += [xtime(a[0]) ^ xtime(a[1]) ^ a[1] ^ a[2] ^ a[3], a[0] ^ xtime(a[1]) ^ xtime(a[2]) ^ a[2] ^ a[3],
                        a[0] ^ a[1] ^ xtime(a[2]) ^ xtime(a[3]) ^ a[3], xtime(a[0]) ^ a[0] ^ a[1] ^ a[2] ^ xtime(a[3])]
            s = out
        s = [b ^ k for b, k in zip(s, keys[rnd])]
    return bytes(s)


def cbc_encrypt(plain: bytes, key: bytes, iv: bytes) -> bytes:
    pad = 16 - len(plain) % 16
    plain += bytes([pad]) * pad
    keys, prev, out = ac._expand_key(key), iv, b""
    for i in range(0, len(plain), 16):
        prev = encrypt_block(bytes(a ^ b for a, b in zip(plain[i:i + 16], prev)), keys)
        out += prev
    return out


FIPS_KEY = bytes(range(16))
check(encrypt_block(bytes.fromhex("00112233445566778899aabbccddeeff"), ac._expand_key(FIPS_KEY)).hex()
      == "69c4e0d86a7b0430d8cdb78070b4c55a", "FIPS-197 encrypt (test helper)")
check(bytes(ac._decrypt_block(bytes.fromhex("69c4e0d86a7b0430d8cdb78070b4c55a"), ac._expand_key(FIPS_KEY))).hex()
      == "00112233445566778899aabbccddeeff", "FIPS-197 decrypt")
for text in (b"", b"x" * 15, b"y" * 16, "{\"a\": \"café\"}".encode() * 9):
    check(ac.aes_cbc_decrypt(cbc_encrypt(text, b"K" * 16, b"I" * 16), b"K" * 16, b"I" * 16) == text, f"cbc {text[:8]}")
try:
    ac.aes_cbc_decrypt(cbc_encrypt(b"hello", b"K" * 16, b"I" * 16), b"L" * 16, b"I" * 16)
    check(False, "wrong key should fail the padding check")
except ValueError:
    pass
check(ac.sign("0123456789abcdefXXXXXXXXXXXXXXXX", 1700000000000, "aB3dE6") ==
      hashlib.md5((hashlib.md5(b"0123456789abcdef").hexdigest() + "1700000000000aB3dE6").encode()).hexdigest(),
      "signature")


# ---- sample reports (shapes from Rinkhals' notes and anycubic_ha_local) ----------------
def info(state="busy", phase="printing", pause=0, **over):
    project = None if state == "free" else {
        "state": phase, "pause": pause, "progress": 42, "curr_layer": 120, "total_layers": 300,
        "remain_time": 83, "print_time": 60, "filename": ".3mf_temp/Benchy_PLA.gcode"}
    d = {"printerName": "Desk Kobra", "model": "Anycubic Kobra 3", "version": "2.4.1.9", "state": state,
         "project": project, "print_speed_mode": 2,
         "temp": {"curr_hotbed_temp": 59.6, "curr_nozzle_temp": 219.84, "target_hotbed_temp": 60,
                  "target_nozzle_temp": 220}}
    d.update(over)
    return {"type": "info", "action": "report", "state": "done", "code": 200, "msg": "done", "data": d}


ACE = {"type": "multiColorBox", "action": "getInfo", "code": 200, "data": {"multi_color_box": [
    {"id": 0, "model_id": 40001, "loaded_slot": 2, "status": 1, "slots": [
        {"index": 0, "type": "PLA", "color": [255, 0, 0], "status": 4},
        {"index": 1, "type": "PETG", "color": [0, 0, 0], "status": 4},
        {"index": 2, "type": "PLA", "color": [0, 128, 255], "status": 5},
        {"index": 3, "type": "", "color": [], "status": 0}]}]}}


def msg_for(*reports, name="K3", model="Kobra 3"):
    st = ac.new_state()
    for r in reports:
        ac.apply_report(st, r["type"], r)
    return ac.build_message(name, model, st)


m = msg_for(info(), ACE)
check(m == {"name": "K3", "model": "Kobra 3", "online": True, "state": "RUNNING", "pct": 42, "remaining_min": 83,
            "layer": 120, "layers": 300, "file": "Benchy_PLA", "nozzle": 219.8, "nozzle_target": 220,
            "bed": 59.6, "bed_target": 60, "chamber": None, "speed": "Standard", "error": "",
            "ams": [{"color": "#FF0000", "type": "PLA", "active": False},
                    {"color": "#000000", "type": "PETG", "active": False},
                    {"color": "#0080FF", "type": "PLA", "active": True}]}, f"printing: {m}")
json.dumps(m)

for phase in ("preheating", "auto_leveling", "flow_calibrating", "something_new"):
    check(msg_for(info(phase=phase))["state"] == "PREPARE", f"{phase} -> PREPARE")
check(msg_for(info(phase="resumed"))["state"] == "RUNNING", "resumed -> RUNNING")
m = msg_for(info(phase="paused", pause=1, print_speed_mode=3))
check(m["state"] == "PAUSE" and m["speed"] == "Sport" and m["pct"] == 42 and m["error"] == "", f"paused: {m}")
check(msg_for(info(phase="printing", pause=1))["state"] == "PAUSE", "pause flag -> PAUSE")
m = msg_for(info(phase="finished"))
check(m["state"] == "FINISH" and m["pct"] == 100 and m["remaining_min"] == 0 and m["file"] == "Benchy_PLA",
      f"finished: {m}")
m = msg_for(info(state="free", last_project={"state": "finished", "filename": "Gear.gcode", "progress": 100,
                                             "curr_layer": 50, "total_layers": 50}))
check(m["state"] == "FINISH" and m["file"] == "Gear" and m["layers"] == 50, f"free after finish: {m}")
m = msg_for(info(state="free", print_speed_mode=1, last_project={"state": "stoped", "filename": "Gear.gcode"}))
check(m["state"] == "IDLE" and m["file"] == "" and m["pct"] == 0 and m["speed"] == "Silent" and m["nozzle"] == 219.8,
      f"idle: {m}")
check(msg_for(info(state="busy", project=None))["state"] == "IDLE", "busy without a job (drying) -> IDLE")
# A clog pauses the job with a reason; resuming clears it.
CLOG = {"type": "print", "action": "pause", "state": "paused", "code": 11518, "msg": "filament stuck", "data": {}}
m = msg_for(info(phase="paused", pause=1), CLOG)
check(m["state"] == "PAUSE" and m["error"] == "Filament is stuck", f"clog: {m}")
RESUMED = {"type": "print", "action": "resume", "state": "resumed", "code": 200, "data": {}}
check(msg_for(info(phase="paused", pause=1), CLOG, RESUMED)["error"] == "", "resume clears the reason")
# A fault stops the job -> FAILED until the next one; a stop by the user is just IDLE.
FAULT = {"type": "print", "action": "stop", "state": "stoped", "code": 10122, "msg": "x", "data": {}}
m = msg_for(info(state="free"), FAULT)
check(m["state"] == "FAILED" and m["error"] == "Nozzle heating fault", f"failed: {m}")
check(msg_for(info(state="free"), {**FAULT, "code": 10601})["state"] == "IDLE", "user stop -> IDLE")
check(msg_for(info(state="free"), {**FAULT, "code": 19999, "msg": "Odd thing"})["error"] == "Odd thing", "unknown code")
check(msg_for(info(state="free"), FAULT, {"type": "print", "state": "preheating", "code": 200, "data": {}},
              info(phase="preheating"))["state"] == "PREPARE", "new job clears FAILED")
# Pushed temperature and progress reports fold into the last info.
m = msg_for(info(), {"type": "tempature", "data": {"curr_nozzle_temp": 221, "target_nozzle_temp": 225}},
            {"type": "print", "state": "printing", "code": 200,
             "data": {"progress": 50, "curr_layer": 150, "remain_time": 60, "taskid": "1"}})
check((m["nozzle"], m["nozzle_target"], m["bed"], m["pct"], m["layer"], m["remaining_min"], m["layers"]) ==
      (221, 225, 59.6, 50, 150, 60, 300), f"pushes merged: {m}")
# S1 Max chamber; two ACE units with a partial update; Kobra X built-in box (id -1) last.
m = msg_for(info(temp={"curr_nozzle_temp": 30, "curr_hotbed_temp": 25, "curr_chamber_temp": 41}), ACE,
            {"type": "multiColorBox", "data": {"multi_color_box": [
                {"id": -1, "loaded_slot": -1, "slots": [{"index": 0, "type": "TPU", "color": [1, 2, 3]}]},
                {"id": 1, "loaded_slot": -1, "slots": [{"index": 0, "type": "ABS", "color": [9, 9, 9], "status": 4}]},
                {"id": 0, "slots": [{"index": 3, "type": "ASA", "color": [255, 255, 255], "status": 4}]}]}})
check(m["chamber"] == 41 and [a["type"] for a in m["ams"]] == ["PLA", "PETG", "PLA", "ASA", "ABS", "TPU"]
      and [a["active"] for a in m["ams"]] == [False, False, True, False, False, False], f"ACE units: {m['ams']}")
# No ACE: the external spool.
m = msg_for(info(), {"type": "extfilbox", "data": {"type": "PETG", "color": [0, 255, 0], "loaded": 1}})
check(m["ams"] == [{"color": "#00FF00", "type": "PETG", "active": True}], f"external spool: {m['ams']}")
big = {"type": "multiColorBox", "data": {"multi_color_box": [
    {"id": i, "slots": [{"index": j, "type": "PLA", "color": [1, 1, 1]} for j in range(4)]} for i in range(3)]}}
check(len(msg_for(info(), big)["ams"]) == 8, "slots capped at 8")
check(msg_for()["state"] == "IDLE", "no report yet")
# Odd shapes and NaN/Infinity (json.loads accepts both) are ignored, not raised.
odd = json.loads('{"type": "tempature", "data": {"curr_nozzle_temp": NaN, "target_nozzle_temp": Infinity}}')
m = msg_for(info(temp="hot"), odd,
            {"type": "multiColorBox", "data": {"multi_color_box": {"id": 0}}},
            {"type": "multiColorBox", "data": {"multi_color_box": [{"id": 0, "slots": "x"}, "junk"]}},
            {"type": "print", "data": {"progress": 5}}, {"type": "info", "data": None})
check(m["state"] == "RUNNING" and m["nozzle"] is None and m["nozzle_target"] is None and m["ams"] == [],
      f"odd shapes: {m}")
nan = json.loads('{"type": "info", "data": {"state": "busy", "print_speed_mode": NaN, "temp": {"curr_hotbed_temp":'
                 ' -Infinity}, "project": {"state": "printing", "progress": NaN, "remain_time": Infinity,'
                 ' "curr_layer": NaN}}}')
m = msg_for(nan)
check((m["pct"], m["remaining_min"], m["layer"], m["bed"], m["speed"]) == (0, 0, 0, None, ""), f"NaN: {m}")
json.dumps(m, allow_nan=False)
check(re.fullmatch(r"[0-9A-F]{32}", ac.controller_id("1.2.3.4", 18910)) is not None
      and ac.controller_id("1.2.3.4", 18910) == ac.controller_id("1.2.3.4", 18910)
      and ac.controller_id("1.2.3.4", 18910) != ac.controller_id("1.2.3.5", 18910), "stable did")

# ---- a fake printer: HTTP handshake + a tiny MQTT broker ---------------------------------
TOKEN = "0123456789abcdefFEDCBA9876543210"
MODEL_ID, DEVICE_ID, USER, PASSWORD = "20025", "c0ffee00c0ffee00c0ffee00c0ffee00", "user_lan", "pw-SeCrEt-77"


class Broker:
    """MQTT 3.1.1 at QoS 0 (CONNECT, SUBSCRIBE, PUBLISH, PING); answers queries with reports."""

    def __init__(self):
        self.password = PASSWORD
        self.reports = {"info": info(), "multiColorBox": ACE}
        self.requests, self.subs, self.writers = [], [], []
        self.ready = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()
        self.ready.wait(5)

    def _run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        server = self.loop.run_until_complete(asyncio.start_server(self._client, "127.0.0.1", 0))
        self.port = server.sockets[0].getsockname()[1]
        self.ready.set()
        self.loop.run_forever()

    @staticmethod
    async def _packet(r):
        head = await r.readexactly(1)
        n, mult = 0, 1
        while True:
            b = (await r.readexactly(1))[0]
            n += (b & 0x7F) * mult
            mult *= 128
            if not b & 0x80:
                break
        return head[0], await r.readexactly(n)

    @staticmethod
    def _pub(topic, payload):
        body = len(topic).to_bytes(2, "big") + topic.encode() + payload
        n, out = len(body), bytearray()
        while True:
            b, n = n % 128, n // 128
            out.append(b | (0x80 if n else 0))
            if not n:
                break
        return b"\x30" + bytes(out) + body

    def report_topic(self, kind):
        return f"anycubic/anycubicCloud/v1/printer/public/{MODEL_ID}/{DEVICE_ID}/{kind}/report"

    async def _client(self, r, w):
        try:
            while True:
                head, body = await self._packet(r)
                kind = head >> 4
                if kind == 1:
                    flags, i, fields = body[7], 10, []
                    for _ in range(1 + bool(flags & 0x80) + bool(flags & 0x40)):
                        n = int.from_bytes(body[i:i + 2], "big")
                        fields.append(body[i + 2:i + 2 + n].decode())
                        i += 2 + n
                    ok = fields[1:] == [USER, self.password]
                    w.write(bytes([0x20, 2, 0, 0 if ok else 4]))
                    if not ok:
                        await w.drain()
                        return
                    self.writers.append(w)
                elif kind == 8:
                    n = int.from_bytes(body[2:4], "big")
                    self.subs.append(body[4:4 + n].decode())
                    w.write(bytes([0x90, 3]) + body[:2] + b"\x00")
                elif kind == 3:
                    n = int.from_bytes(body[:2], "big")
                    topic, payload = body[2:2 + n].decode(), body[2 + n:]
                    self.requests.append((topic, json.loads(payload)))
                    q = topic.rsplit("/", 1)[-1]
                    if topic.startswith(f"anycubic/anycubicCloud/v1/web/printer/{MODEL_ID}/{DEVICE_ID}/") \
                            and q in self.reports:
                        w.write(self._pub(self.report_topic(q), json.dumps(self.reports[q]).encode()))
                elif kind == 12:
                    w.write(b"\xd0\x00")
                elif kind == 14:
                    return
                await w.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass

    def push(self, report):
        data = self._pub(self.report_topic(report["type"]), json.dumps(report).encode())
        for w in self.writers:
            self.loop.call_soon_threadsafe(w.write, data)


broker = Broker()


class Fake:
    mode = "lan"    # lan | cloud | old | refuse
    ctrl_calls = 0
    bad_sign = 0
    dids: list = []
    stale_login = False   # hand out a login the broker no longer takes


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body):
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path != "/info":
            return self._send({"code": 404})
        d = {"deviceType": "fdm", "modelId": MODEL_ID, "modelName": "Anycubic Kobra 3 V2", "cn": "9B20-XYZ",
             "ctrlType": "cloud" if Fake.mode == "cloud" else "lan", "ip": "10.9.9.9", "deviceName": "Kobra 3 V2",
             "token": TOKEN, "ctrlInfoUrl": f"http://10.9.9.9:{HTTP_PORT}/ctrl"}
        if Fake.mode == "old":
            del d["token"], d["ctrlInfoUrl"]
        self._send(d)

    def do_POST(self):
        url = urlsplit(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        Fake.ctrl_calls += 1
        Fake.dids.append(q.get("did"))
        if url.path != "/ctrl" or Fake.mode == "refuse":
            return self._send({"code": 10001, "message": "refused", "data": None})
        expect = hashlib.md5((hashlib.md5(TOKEN[:16].encode()).hexdigest() + q["ts"] + q["nonce"]).encode())
        if q.get("sign") != expect.hexdigest() or len(q.get("did", "")) != 32:
            Fake.bad_sign += 1
            return self._send({"code": 10002, "message": "sign error", "data": None})
        creds = {"broker": f"mqtts://10.9.9.9:{broker.port}", "username": USER, "password": "stale-login" if Fake.stale_login else broker.password,
                 "deviceId": DEVICE_ID, "modeId": MODEL_ID}
        iv = "IVIVIVIVIVIVIVIV"
        sealed = base64.b64encode(cbc_encrypt(json.dumps(creds).encode(), TOKEN[16:32].encode(), iv.encode()))
        self._send({"code": 200, "message": "success", "data": {"info": sealed.decode(), "token": iv}})


srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
HTTP_PORT = srv.server_address[1]
ac.USE_TLS = False


def wait_for(cond, secs=8):
    end = time.time() + secs
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


# The handshake: login decrypted, the request sent to the entered host (not 10.9.9.9).
inf, creds = ac.handshake("127.0.0.1", HTTP_PORT)
check(creds["username"] == USER and creds["password"] == PASSWORD and creds["deviceId"] == DEVICE_ID
      and creds["broker_port"] == broker.port and inf["modelId"] == MODEL_ID, "handshake")
check(Fake.bad_sign == 0, "signature accepted")

# End to end: the service, the worker, reports and pushes.
ac.AnycubicWorker.POLL_S = 0.1
entry = {"name": "Kobra", "type": "anycubic", "host": "127.0.0.1", "port": HTTP_PORT}
svc = pr.PrinterService([entry, {"name": "x", "type": "anycubic"}], secret_getter=lambda s, k: None)
check(len(svc.workers) == 1 and type(svc.workers[0]).__name__ == "AnycubicWorker", "worker from the registry")
svc.start()
check(wait_for(lambda: svc.message()["printers"][0]["state"] == "RUNNING"), "anycubic: state arrives")
p = svc.message()["printers"][0]
check(p["model"] == "Anycubic" and p["pct"] == 42 and p["file"] == "Benchy_PLA", f"service message: {p}")
check(wait_for(lambda: len(svc.message()["printers"][0]["ams"]) == 3), "ACE slots arrive")
check(f"anycubic/anycubicCloud/v1/printer/public/{MODEL_ID}/{DEVICE_ID}/#" in broker.subs, f"subscribed: {broker.subs}")
sent = {(t.rsplit("/", 1)[-1], b["action"]) for t, b in broker.requests}
check({("info", "query"), ("multiColorBox", "getInfo")} <= sent, f"queries: {sent}")
check(all(b["type"] == t.rsplit("/", 1)[-1] and b["msgid"] and b["timestamp"] for t, b in broker.requests),
      "query envelope")
broker.push({"type": "print", "action": "workReport", "state": "printing", "code": 200,
             "data": {"progress": 77, "curr_layer": 200, "remain_time": 20}})
check(wait_for(lambda: svc.message()["printers"][0]["pct"] == 77), "progress push merged")
broker.reports["info"] = info(phase="finished")
broker.push(broker.reports["info"])
check(wait_for(lambda: svc.message()["printers"][0]["state"] == "FINISH"), "finish arrives")
w = svc.workers[0]
check(w.message(now=time.monotonic() + pr.STALE_S + 1)["state"] == "OFFLINE", "stale -> OFFLINE")
# The printer restarts with a new login: the worker shakes hands again.
calls = Fake.ctrl_calls
broker.password = "new-login-after-restart"
for wr in list(broker.writers):
    broker.loop.call_soon_threadsafe(wr.close)
broker.writers.clear()
broker.reports["info"] = info(phase="paused", pause=1)
check(wait_for(lambda: svc.message()["printers"][0]["state"] == "PAUSE", 15), "reconnects with a new login")
check(Fake.ctrl_calls > calls, "handshake repeated")
check(len(Fake.dids) >= 3 and set(Fake.dids) == {ac.controller_id("127.0.0.1", HTTP_PORT)},
      f"same did on every handshake: {set(Fake.dids)}")
# A report that blows up is dropped; paho's network thread keeps going.
real_apply = ac.apply_report


def explode(state, kind, report):
    if report.get("boom"):
        raise RuntimeError("bad report")
    real_apply(state, kind, report)


ac.apply_report = explode
broker.push({"type": "print", "boom": True, "data": {}})
broker.push({"type": "print", "state": "printing", "code": 200, "data": {"progress": 64}})
check(wait_for(lambda: svc.message()["printers"][0]["pct"] == 64), "thread survives a bad report")
ac.apply_report = real_apply
svc.stop()
broker.password = PASSWORD
broker.reports["info"] = info()

# Failures: one log line each, no credential in it, OFFLINE.
logs = []
handler = type("H", (poll.logging.Handler,), {"emit": lambda self, rec: logs.append(rec.getMessage())})()
poll.log.addHandler(handler)
ac.AnycubicWorker.RETRY_S = 0.1
dead = pr.PrinterService([{"name": "Gone", "type": "anycubic", "host": "127.0.0.1", "port": 1}],
                         secret_getter=lambda s, k: None)
dead.start()
check(wait_for(lambda: len(logs) >= 1), "unreachable logs a line")
time.sleep(0.5)
check(len(logs) == 1 and dead.message()["printers"][0]["state"] == "OFFLINE", f"unreachable: {logs}")
dead.stop()
logs.clear()
Fake.mode = "cloud"
cloud = pr.PrinterService([entry], secret_getter=lambda s, k: None)
cloud.start()
check(wait_for(lambda: any("LAN Mode" in line for line in logs)), f"cloud mode hint: {logs}")
cloud.stop()
Fake.mode = "lan"
logs.clear()
Fake.stale_login = True   # the broker refuses the login the handshake gave
bad = pr.PrinterService([entry], secret_getter=lambda s, k: None)
bad.start()
check(wait_for(lambda: any("refused" in line for line in logs)), f"refused login logged: {logs}")
check(not any(s in line for line in logs for s in (PASSWORD, USER, TOKEN, "stale-login")),
      f"no credential logged: {logs}")
check(bad.message()["printers"][0]["state"] == "OFFLINE", "refused -> OFFLINE")
bad.stop()
Fake.stale_login = False
# After a failed connect the next handshake waits HANDSHAKE_BACKOFF_S (30 s), not RETRY_S.
Fake.mode = "refuse"
calls = Fake.ctrl_calls
slow = pr.PrinterService([entry], secret_getter=lambda s, k: None)
slow.start()
check(wait_for(lambda: Fake.ctrl_calls > calls), "first handshake")
time.sleep(1.5)
check(Fake.ctrl_calls == calls + 1, f"handshakes backed off: {Fake.ctrl_calls - calls}")
t0 = time.time()
slow.stop()
slow.workers[0].join(3)
check(not slow.workers[0].is_alive() and time.time() - t0 < 3, "stop interrupts the backoff")
Fake.mode = "lan"
poll.log.removeHandler(handler)

# The setup probe.
r = ac.probe({"host": "127.0.0.1", "port": HTTP_PORT}, None)
check(r.ok and r.name == "Desk Kobra" and r.model == "Kobra 3 V2" and r.secret is None, f"probe ok: {r}")
Fake.stale_login = True
r = ac.probe({"host": "127.0.0.1", "port": HTTP_PORT}, None)
check(not r.ok and "refused" in r.message and "stale-login" not in r.message, f"probe bad login: {r}")
Fake.stale_login = False
Fake.mode = "refuse"
r = ac.probe({"host": "127.0.0.1", "port": HTTP_PORT}, None)
check(not r.ok and "refused the LAN Mode handshake" in r.message, f"probe handshake refused: {r}")
Fake.mode = "cloud"
r = ac.probe({"host": "127.0.0.1", "port": HTTP_PORT}, None)
check(not r.ok and "cloud mode" in r.message, f"probe cloud mode: {r}")
Fake.mode = "old"
r = ac.probe({"host": "127.0.0.1", "port": HTTP_PORT}, None)
check(not r.ok and "handshake" in r.message, f"probe unknown handshake: {r}")
Fake.mode = "lan"
r = ac.probe({"host": "127.0.0.1", "port": 1}, None, timeout=1)
check(not r.ok and "LAN Mode" in r.message, f"probe unreachable: {r}")

check("printer" in enabled_pages({"printers": [entry]}), "printer page auto-added for anycubic")

# ---- the CLI --------------------------------------------------------------------
tmp = pathlib.Path(tempfile.mkdtemp()) / "config.toml"
tmp.write_text('# my config\n[hub]\nhost = "x"\n', encoding="utf-8")
ADD = functools.partial(printers_cli.cmd_add_polled, spec=ac.SPEC)


def run(cmd, answers, passwords):
    a, p = iter(answers), iter(passwords)
    builtins.input = lambda prompt="": next(a)
    getpass.getpass = lambda prompt="": next(p)
    cmd(tmp)


store.clear()
run(ADD, ["127.0.0.1", str(HTTP_PORT), "", ""], [])   # no password asked
cfg = tomllib.loads(tmp.read_text())
check(cfg["printers"] == [{"name": "Desk Kobra", "type": "anycubic", "host": "127.0.0.1", "port": HTTP_PORT,
                           "model": "Kobra 3 V2"}], f"anycubic saved: {cfg.get('printers')}")
check(store == {} and PASSWORD not in tmp.read_text() and USER not in tmp.read_text(),
      "nothing in the credential store or config: LAN Mode logs in per connection")
run(ADD, ["127.0.0.1", str(HTTP_PORT), "", ""], [])
check(len(tomllib.loads(tmp.read_text())["printers"]) == 1, "duplicate host refused")
ac.SPEC.probe = lambda *a, **k: poll.ProbeResult(False, "No answer (stubbed)")
run(ADD, ["192.168.1.80", "", "y", "", ""], [])
cfg = tomllib.loads(tmp.read_text())
check(cfg["printers"][1] == {"name": "Anycubic", "type": "anycubic", "host": "192.168.1.80"},
      f"default port and model left out: {cfg['printers'][1]}")
check("Anycubic LAN 192.168.1.80:18910" in printers_cli.describe(cfg["printers"][1]), "describe")
run(printers_cli.cmd_remove, ["2"], [])   # no stored credential: no question asked
check(len(tomllib.loads(tmp.read_text())["printers"]) == 1, "removed")

srv.shutdown()
print("\nPROBLEMS:" if problems else "\nOK", problems if problems else "")
