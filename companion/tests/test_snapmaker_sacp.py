"""Snapmaker J1 / Artisan (SACP, port 8888) support without a printer: packet framing against
packets built by Snapmaker's own SDK, status decoding and state mapping, the worker and probe
against a tiny local SACP server (approval prompt included), and the CLI.

    uv run python tests/test_snapmaker_sacp.py
Prints OK or the problems found. Never touches the OS credential store.
"""
import builtins
import functools
import getpass
import json
import pathlib
import socket
import struct
import sys
import tempfile
import threading
import time
import tomllib
import types

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
from deskhub import snapmaker_sacp as sc  # noqa: E402
from deskhub.pages import enabled_pages  # noqa: E402

P = sc.Packet

# ---- packets ------------------------------------------------------------------------------
# Built with @snapmaker/snapmaker-sacp-sdk 0.1.1 (Header/Packet.toBuffer, helper.stringToBuffer),
# the code Luban sends with: its connect request with host "PC", client "Luban" and token "abc";
# Dispatcher.subscribe(0x01, 0xa0, 1000); and an empty 0x01 0x21 (get machine info).
LUBAN_CONNECT = bytes.fromhex("aa5518000102110000010001050200504305004c7562616e03006162631927")
LUBAN_SUBSCRIBE = bytes.fromhex("aa550c0001012700000200010001a0e8035c13")
LUBAN_MACHINE_INFO = bytes.fromhex("aa55080001017f000003000121defb")

check(sc.crc8(b"123456789") == 0xF4, f"crc8 check value: {sc.crc8(b'123456789'):#x}")
check(sc.crc8(LUBAN_CONNECT[:6]) == LUBAN_CONNECT[6], "crc8 of Luban's header")
check(sc.checksum(LUBAN_CONNECT[7:-2]) == struct.unpack("<H", LUBAN_CONNECT[-2:])[0], "checksum of Luban's packet")
check(sc.checksum(b"") == 0xffff and sc.checksum(b"\x01") == 0xfffe, "checksum edges")

payload = sc.sacp_string("PC") + sc.sacp_string("Luban") + sc.sacp_string("abc")
check(sc.encode(P(2, 0, 0, 1, 0x01, 0x05, payload)) == LUBAN_CONNECT, "connect packet matches Luban's")
check(sc.encode(P(1, 0, 0, 2, 0x01, 0x00, sc.subscribe_payload(0x01, 0xa0, 1000))) == LUBAN_SUBSCRIBE,
      "subscribe packet matches Luban's")
check(sc.encode(P(1, 0, 0, 3, 0x01, 0x21)) == LUBAN_MACHINE_INFO, "empty packet matches Luban's")
check(sc.connect_payload("PC", "abc").startswith(sc.sacp_string("PC")), "connect payload")

for raw, want in [(LUBAN_CONNECT, P(2, 0, 0, 1, 0x01, 0x05, payload)),
                  (LUBAN_MACHINE_INFO, P(1, 0, 0, 3, 0x01, 0x21, b""))]:
    got = sc.decode(raw)
    check(got == [want], f"decode {raw.hex()}: {got}")
p = P(0, 1, 1, 65535, 0xac, 0xa0, bytes(range(256)) * 3)
check(sc.decode(sc.encode(p)) == [p], "round trip with a long payload")
check(sc.read_string(sc.sacp_string("Cube.gcode") + b"xx", 0) == ("Cube.gcode", 12), "read_string")

# A stream: the touchscreen's stray "a" bytes, garbage, a split packet, a corrupted one, a
# truncated one, a false header with a huge length, then good packets.
a, b, c = (P(0, 1, 1, i, 0x01, 0xa0, bytes([0, i])) for i in (1, 2, 3))
bad = bytearray(sc.encode(b))
bad[-3] ^= 0xff
false_head = bytearray(b"\xaa\x55\xff\xff\x01\x00")
false_head.append(sc.crc8(false_head))
stream = (b"aaaa" + b"\x00\xaa\x13" + sc.encode(a) + bytes(bad) + sc.encode(b)[:9] + bytes(false_head)
          + b"\xaa" + sc.encode(c) + sc.encode(a))
f = sc.Framer()
got = []
for i in range(len(stream)):  # one byte at a time, as a slow socket would
    got += f.feed(stream[i:i + 1])
check(got == [a, c, a], f"framer over a messy stream: {[x.sequence for x in got]}")
check(sc.decode(stream) == [a, c, a], "same stream in one piece")
check(sc.decode(b"") == [] and sc.decode(b"\xaa\x55") == [] and sc.decode(sc.encode(a)[:-1]) == [],
      "short input")
check(sc.decode(b"\xaa\x55" * 50 + sc.encode(b)) == [b], "repeated SOF before a packet")
f = sc.Framer()
check(f.feed(sc.encode(a)[:9]) == [] and f.feed(sc.encode(a)[9:]) == [a], "packet split in two feeds")
check(len(f.buf) == 0, "nothing left over")


# ---- payloads (as the J1 firmware writes them) ---------------------------------------------
def extruder(index, cur, tgt, key=None, status=1):
    entry = struct.pack("<BBBBBiii", 0, 0, 1, status, 0, 400, round(cur * 1000), round(tgt * 1000))
    return b"\x00" + bytes([index if key is None else key, 1]) + entry


def fdm_info(index, active, cur=0, tgt=0):
    entry = struct.pack("<BBBBBiii", 0, 0, 1, 1, 0, 400, round(cur * 1000), round(tgt * 1000))
    return b"\x00" + bytes([index, 0, int(active), 1]) + entry + b"\x01\x00\x00\x00"


def bed(cur, tgt):
    return b"\x00" + struct.pack("<BBBih", 0x10, 1, 0, round(cur * 1000), tgt)


def push(cmd_set, cmd_id, data):
    return P(0, 1, 1, 0, cmd_set, cmd_id, data)


def state(status=2, line=41000, elapsed=3000, speed=100, right_active=True, **kw):
    st = {"head": "fdm", "file": "Bracket v2.gcode", "lines": 100000, "estimate_s": 7200}
    for pk in [push(0x01, 0xa0, bytes([0, status])), push(0x10, 0xa0, extruder(0, 150, 0)),
               push(0x10, 0xa0, extruder(1, 249.6, 250)), push(0x14, 0xa0, bed(59.9, 60)),
               push(0xac, 0xa0, b"\x00" + struct.pack("<I", line)),
               push(0xac, 0xa5, b"\x00" + struct.pack("<I", elapsed)),
               push(0xac, 0xa4, b"\x00" + struct.pack("<Bh", 1, speed)),
               push(0x10, 0xa4, fdm_info(0, not right_active)), push(0x10, 0xa4, fdm_info(1, right_active))]:
        sc.update(st, pk)
    st.update(kw)
    return st


m = sc.build_message("J1", "J1", state())
check(m == {"name": "J1", "model": "J1", "online": True, "state": "RUNNING", "pct": 41, "remaining_min": 72,
            "layer": 0, "layers": 0, "file": "Bracket v2", "nozzle": 249.6, "nozzle_target": 250,
            "bed": 59.9, "bed_target": 60, "chamber": None, "speed": "100%", "error": "", "ams": []},
      f"printing on the right extruder: {m}")
json.dumps(m)
check(sc.build_message("J1", "J1", state(right_active=False))["nozzle"] == 150, "left extruder active")
st = state()
del st["active"]
check(sc.build_message("J1", "J1", st)["nozzle"] == 249.6, "no active flag: the heated one")
st["nozzles"] = {0: (25.0, 0.0), 1: (26.0, 0.0)}
check(sc.build_message("J1", "J1", st)["nozzle"] == 25, "nothing heated: the left one")

# Status codes (the J1 firmware's system_status_e).
for code, want in [(0, "IDLE"), (1, "PREPARE"), (2, "RUNNING"), (3, "PAUSE"), (4, "PAUSE"), (5, "IDLE"),
                   (6, "IDLE"), (7, "RUNNING"), (8, "FINISH"), (9, "PREPARE"), (10, "RUNNING"),
                   (11, "PREPARE"), (31, "PREPARE"), (34, "PREPARE"), (99, "IDLE")]:
    got = sc.build_message("J1", "J1", state(status=code))["state"]
    check(got == want, f"status {code}: {got}, want {want}")
m = sc.build_message("J1", "J1", state(status=4))
check(m["pct"] == 41 and m["file"] == "Bracket v2" and m["remaining_min"] == 72 and m["speed"] == "100%",
      f"paused keeps the job: {m}")
m = sc.build_message("J1", "J1", state(line=0, elapsed=40))
check(m["state"] == "PREPARE" and m["pct"] == 0 and m["remaining_min"] == 119 and m["file"] == "Bracket v2",
      f"heating before the first line: {m}")
m = sc.build_message("J1", "J1", state(status=8, line=100000))
check(m["state"] == "FINISH" and m["pct"] == 100 and m["file"] == "Bracket v2" and m["remaining_min"] == 0
      and m["speed"] == "", f"completed: {m}")

# Outside a job: none of the last job's details.
for code in (0, 5, 6, 31):
    m = sc.build_message("J1", "J1", state(status=code))
    check(m["pct"] == 0 and m["file"] == "" and m["remaining_min"] == 0 and m["speed"] == ""
          and m["nozzle"] == 249.6 and m["bed"] == 59.9, f"status {code} shows no old job: {m}")

# Exceptions: shown while paused; a stop with one up is a failure.
m = sc.build_message("J1", "J1", state(status=4, exceptions=[(2, 2049, 7), (3, 2049, 22)]))
check(m["state"] == "PAUSE" and m["error"] == "Left nozzle overheating", f"paused with exceptions: {m}")
m = sc.build_message("J1", "J1", state(status=6, exceptions=[(3, 2049, 10)]))
check(m["state"] == "FAILED" and m["error"] == "Bed temperature error" and m["file"] == "", f"failed: {m}")
m = sc.build_message("A", "Artisan", state(status=4, exceptions=[(1, 7, 42)]))
check(m["error"] == "Error 42", f"other owner: {m}")
check(sc.build_message("J1", "J1", state(exceptions=[(2, 2049, 4)]))["error"] == "",
      "an exception while printing isn't shown")

# No file info: no progress, but the time estimate still works from the elapsed time.
m = sc.build_message("J1", "J1", state(lines=None, estimate_s=None, file=None))
check(m["state"] == "RUNNING" and m["pct"] == 0 and m["remaining_min"] == 0 and m["file"] == "",
      f"no file info: {m}")

# Laser and CNC heads: state and progress, no temperatures.
m = sc.build_message("A", "Artisan", state(head="laser", file="logo.nc"))
check(m["state"] == "RUNNING" and m["pct"] == 41 and m["file"] == "logo" and m["nozzle"] is None
      and m["bed"] is None and m["nozzle_target"] is None, f"laser: {m}")


def modules(*ids):
    return b"\x00" + bytes([len(ids)]) + b"".join(
        struct.pack("<BHBBIB", k, i, 0, 0, 0, 1) + sc.sacp_string("1.0") for k, i in enumerate(ids))


check(sc.head_kind(modules(0, 0, 513)) == "fdm", "J1 modules")
check(sc.head_kind(modules(14, 16, 7)) == "laser" and sc.head_kind(modules(15)) == "cnc", "laser and CNC")
check(sc.head_kind(modules(13, 14)) == "fdm" and sc.head_kind(b"\x00\x05junk") == "fdm"
      and sc.head_kind(b"") == "fdm" and sc.head_kind(b"\x06") == "fdm", "odd module lists")
check(sc.machine_type(b"\x00\x04\x01") == 4 and sc.machine_type(b"\x01\x04") is None
      and sc.machine_type(b"") is None, "machine type")

# A dual head sending both extruders in one packet (Luban's Artisan branch), and junk.
st = {}
two = b"\x00\x08\x02" + b"".join(struct.pack("<BBBBBiii", i, 0, 0, 1, 0, 400, t * 1000, t * 1000)
                                  for i, t in ((0, 200), (1, 210)))
sc.update(st, push(0x10, 0xa0, two))
check(st["nozzles"] == {0: (200, 200), 1: (210, 210)}, f"two extruders in one packet: {st}")
sc.update(st, push(0x10, 0xa0, extruder(1, 99, 0, key=0x09)))
check(st["nozzles"][1] == (99, 0), "extruder from the module key")
for junk in [b"", b"\x05\x02\x03\x04\x05\x06", b"\x00", b"\x00\x01\x01"]:  # failed results, too short
    for cs, ci in [(0x10, 0xa0), (0x10, 0xa4), (0x14, 0xa0), (0xac, 0xa0), (0xac, 0xa5), (0xac, 0xa4), (0x99, 0x01)]:
        sc.update(st, push(cs, ci, junk))
for junk in [b"", b"\x05\x02", b"\x00"]:
    sc.update(st, push(0x01, 0xa0, junk))
check(st["nozzles"][0] == (200, 200) and not {"status", "speed", "bed", "line", "elapsed_s", "active"} & set(st),
      f"junk ignored: {st}")
check(sc.build_message("J", "J", {})["state"] == "IDLE", "empty state")
check(sc.exceptions(b"\x00\x02" + struct.pack("<BHBBHB", 1, 2049, 4, 2, 2049, 7) + b"\x00")
      == [(1, 2049, 4), (2, 2049, 7)] and sc.exceptions(b"\x00\x05") == [] and sc.exceptions(b"") == [],
      "exception list")
check(sc.file_info(P(0, 2, 1, 1, 0xac, 0x1a, b"\x00" + sc.sacp_string("a.gcode") + struct.pack("<II", 9, 60)))
      == {"name": "a.gcode", "lines": 9, "estimate_s": 60}, "screen file info")
check(sc.file_info(P(0, 1, 1, 1, 0xac, 0x00, b"\x00" + sc.sacp_string("ab12") + sc.sacp_string("b.gcode")
                     + b"\x00" * 30)) == {"name": "b.gcode"}, "controller file info")
check(sc.file_info(None) == {} and sc.file_info(P(0, 1, 1, 1, 0xac, 0x00, b"\x09")) == {}
      and sc.file_info(P(0, 1, 1, 1, 0xac, 0x1a, b"\x00\x05")) == {}, "no file info")


# ---- the worker's job bookkeeping, against a stub link ------------------------------------
class StubLink:
    def __init__(self):
        self.pkts, self.asked, self.answers = [], [], {}
        self.heard = time.monotonic()

    def next(self, timeout):
        return self.pkts.pop(0) if self.pkts else None

    def request(self, receiver, cmd_set, cmd_id, payload=b"", timeout=0, keep=None):
        self.asked.append((cmd_set, cmd_id))
        return self.answers.get((cmd_set, cmd_id))


def controller_file(name):
    return P(0, 1, 1, 0, 0xac, 0x00, b"\x00" + sc.sacp_string("ab") + sc.sacp_string(name))


def pump(w, link, status=None, line=None, elapsed=None):
    if status is not None:
        link.pkts.append(push(0x01, 0xa0, bytes([0, status])))
    if line is not None:
        link.pkts.append(push(0xac, 0xa0, b"\x00" + struct.pack("<I", line)))
    if elapsed is not None:
        link.pkts.append(push(0xac, 0xa5, b"\x00" + struct.pack("<I", elapsed)))
    w._exc_at = time.monotonic()  # leave the exception requests out of this
    w._pump(link)
    return sc.build_message("J1", "J1", w._state)


bw = sc.SnapmakerSacpWorker({"name": "J1", "host": "127.0.0.1"}, lambda s, k: None)
link = StubLink()
link.answers[(0xac, 0x00)] = controller_file("Old.gcode")
pump(bw, link, status=2, line=500, elapsed=600)
check(bw._state.get("file") == "Old.gcode", f"first job's file: {bw._state}")
pump(bw, link, status=8, line=1000, elapsed=1200)
# Completed straight to printing (STARTING fell between polls): the new job's file is asked for.
link.answers[(0xac, 0x00)] = controller_file("New.gcode")
link.asked.clear()
m = pump(bw, link, status=2)
check(m["file"] == "New" and (0xac, 0x00) in link.asked, f"8 -> 2 asks again: {m}, {link.asked}")
check(m["state"] == "PREPARE", f"the old job's line doesn't carry over: {m}")
m = pump(bw, link, status=2, line=50, elapsed=30)
check(m["state"] == "RUNNING" and m["file"] == "New", f"new job runs: {m}")
# The job clock goes back with the status still printing (a whole job between polls).
link.answers[(0xac, 0x00)] = controller_file("Third.gcode")
m = pump(bw, link, status=2, line=60, elapsed=5)
check(m["file"] == "Third", f"clock going back asks again: {m}")
m = pump(bw, link, status=2, line=70, elapsed=10)
check(m["file"] == "Third" and m["state"] == "RUNNING", f"same job goes on: {m}")
# Idle drops the counters too, so the next job starts from PREPARE.
pump(bw, link, status=0, line=70, elapsed=10)
check(not {"line", "elapsed_s", "file"} & set(bw._state), f"idle forgets the job: {bw._state}")
link.answers[(0xac, 0x00)] = controller_file("Fourth.gcode")
m = pump(bw, link, status=1)
check(m["state"] == "PREPARE" and m["file"] == "Fourth" and m["remaining_min"] == 0, f"starting: {m}")

# Exceptions are forgotten once the printer stops answering for them.
bw._state["exceptions"] = [(3, 2049, 7)]
for _ in range(sc.EXCEPTION_MISSES):
    bw._exc_at = 0.0
    bw._pump(link)
check("exceptions" not in bw._state, f"stale exceptions dropped: {bw._state}")


# ---- a tiny J1 on port 8888 (touchscreen and controller in one) ----------------------------
class Fake:
    approve = "auto"       # auto (if the token is known), prompt, refuse, off, ignore
    known: set = set()
    tapped = threading.Event()
    answer = 0             # what tapping gives: 0 Connect, 200 Refuse
    machine = 4
    status = 2
    line = 41000
    screen_file = True     # answer 0xac 0x1a like Luban expects
    garbage = False
    exceptions: list = []
    drop_next = False      # close the socket at the next request (the touchscreen ending the session)
    end_session = False    # send 0x01 0x06, as Disconnect on the touchscreen does
    mute_conn = -1         # this connection goes silent (Wi-Fi lost, no FIN)
    acks: list = []
    connections = 0
    tokens: list = []
    requests: list = []
    subs: list = []
    stop_flag = threading.Event()
    clients: list = []


def reply(conn, req, data, attr=1):
    conn.sendall(sc.encode(P(0, req.receiver, attr, req.sequence, req.cmd_set, req.cmd_id, data)))


def pusher(conn, subs, done, me):
    """Pushes every subscribed command, as the controller's subscribe loop does."""
    payloads = {(0x01, 0xa0): lambda: [bytes([0, Fake.status])],
                (0x10, 0xa0): lambda: [extruder(0, 150, 0), extruder(1, 249.6, 250)],
                (0x14, 0xa0): lambda: [bed(59.9, 60)],
                (0xac, 0xa0): lambda: [b"\x00" + struct.pack("<I", Fake.line)],
                (0xac, 0xa5): lambda: [b"\x00" + struct.pack("<I", 3000)],
                (0xac, 0xa4): lambda: [b"\x00" + struct.pack("<Bh", 1, 120)],
                (0x10, 0xa4): lambda: [fdm_info(0, False), fdm_info(1, True)]}
    n = 0
    try:
        while not done.is_set():
            out = b""
            if Fake.end_session:
                Fake.end_session = False
                out += sc.encode(P(0, 2, 0, 77, 0x01, 0x06, b""))
            for key in list(subs) if Fake.mute_conn != me else []:
                for data in payloads.get(key, list)():
                    n += 1
                    # The heartbeat as a request, the rest as ACKs: either must be read.
                    attr = 0 if key == (0x01, 0xa0) else 1
                    out += sc.encode(P(0, 1, attr, n, key[0], key[1], data))
            if Fake.garbage:
                out = b"\x00garbage\xaa\x55\x07" + sc.encode(push(0x01, 0xa0, b"\x00\x04"))[:10] + out
            if out:
                conn.sendall(out)
            done.wait(0.2)
    except OSError:
        pass


def client(conn):
    Fake.connections += 1
    me = Fake.connections
    Fake.clients.append(conn)
    framer, subs, done = sc.Framer(), [], threading.Event()
    approved = False
    try:
        while not Fake.stop_flag.is_set():
            try:
                data = conn.recv(4096)
            except TimeoutError:
                continue
            if not data:
                break
            for req in framer.feed(data):
                if req.attribute == 1:
                    Fake.acks.append((req.cmd_set, req.cmd_id, req.payload))
                    continue
                Fake.requests.append((req.receiver, req.cmd_set, req.cmd_id))
                key = (req.cmd_set, req.cmd_id)
                if key == (0x01, 0x05):
                    host, off = sc.read_string(req.payload, 0)
                    name, off = sc.read_string(req.payload, off)
                    token, _ = sc.read_string(req.payload, off)
                    Fake.tokens.append((host, name, token))
                    if Fake.approve == "ignore":
                        continue
                    if Fake.approve == "off":
                        reply(conn, req, b"\xc9\x00\x00")
                        continue
                    if Fake.approve != "auto" or token not in Fake.known:
                        while not Fake.tapped.wait(0.2):  # the prompt is up
                            conn.sendall(b"a")
                        Fake.tapped.clear()
                        if Fake.answer:
                            reply(conn, req, bytes([Fake.answer]) + b"\x00\x00")
                            return
                    Fake.known.add(token)
                    approved = True
                    reply(conn, req, b"\x00\x00\x00")
                    threading.Thread(target=pusher, args=(conn, subs, done, me), daemon=True).start()
                elif not approved or Fake.mute_conn == me:
                    continue  # the screen passes nothing on before approval
                elif Fake.drop_next:
                    Fake.drop_next = False
                    return
                elif key == (0x01, 0x21):
                    reply(conn, req, b"\x00" + struct.pack("<BBI", Fake.machine, 1, 7) + sc.sacp_string("1.2.3")
                          + sc.sacp_string("SN0001"))
                elif key == (0x01, 0x20):
                    reply(conn, req, modules(0, 0, 513))
                elif key == (0x01, 0x00):
                    subs.append((req.payload[0], req.payload[1]))
                    Fake.subs.append((req.payload[0], req.payload[1]))
                    reply(conn, req, b"\x00")
                elif key == (0x01, 0x01):
                    reply(conn, req, b"\x00")
                elif key == (0xac, 0x1a) and Fake.screen_file:
                    reply(conn, req, b"\x00" + sc.sacp_string("Bracket v2.gcode") + struct.pack("<II", 100000, 7200))
                elif key == (0xac, 0x00):
                    reply(conn, req, b"\x00" + sc.sacp_string("0f" * 16) + sc.sacp_string("Hinge.gcode") + b"\x00" * 20)
                elif key == (0x04, 0x02):
                    reply(conn, req, b"\x00" + bytes([len(Fake.exceptions)])
                          + b"".join(struct.pack("<BHB", *e) for e in Fake.exceptions) + b"\x00")
                elif key == (0x01, 0x06):
                    reply(conn, req, b"\x00")
    except OSError:
        pass
    finally:
        done.set()
        conn.close()


srv = socket.create_server(("127.0.0.1", 0))
PORT = srv.getsockname()[1]


def accept_loop(s):
    while True:
        try:
            conn, _ = s.accept()
        except OSError:
            return
        conn.settimeout(0.2)
        threading.Thread(target=client, args=(conn,), daemon=True).start()


threading.Thread(target=accept_loop, args=(srv,), daemon=True).start()


def wait_for(cond, secs=10):
    end = time.time() + secs
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


logs = []
handler_ = type("H", (poll.logging.Handler,), {"emit": lambda self, rec: logs.append(rec.getMessage())})()
poll.log.addHandler(handler_)

# ---- probe --------------------------------------------------------------------------------
Fake.approve = "prompt"
threading.Timer(1.0, Fake.tapped.set).start()
r = sc.probe({"host": "127.0.0.1", "port": PORT}, None, timeout=2, wait=5)
check(r.ok and r.model == "J1" and r.name == "Snapmaker J1" and r.secret and "J1" in r.message,
      f"probe with approval: {r}")
token = r.secret
check(Fake.tokens[-1][1] == "DeskHub" and Fake.tokens[-1][2] == token, f"connect payload: {Fake.tokens[-1]}")
check(wait_for(lambda: (2, 0x01, 0x06) in Fake.requests, 3), "probe says goodbye")

Fake.approve = "prompt"
Fake.answer = 200
threading.Timer(0.5, Fake.tapped.set).start()
r = sc.probe({"host": "127.0.0.1", "port": PORT}, None, timeout=2, wait=5)
check(not r.ok and "turned the connection down" in r.message and r.secret == Fake.tokens[-1][2],
      f"probe refused (the token still comes back, for 'save anyway'): {r}")
Fake.answer = 0

Fake.approve = "off"
r = sc.probe({"host": "127.0.0.1", "port": PORT}, None, timeout=2)
check(not r.ok and "Allow Remote" in r.message and r.secret, f"probe with remote off: {r}")

# The prompt stays up (stray bytes keep coming) but nobody taps: the leftover prompt is mentioned.
sc.SILENT_S = 1.5
Fake.approve = "prompt"
r = sc.probe({"host": "127.0.0.1", "port": PORT}, None, timeout=1, wait=2.5)
check(not r.ok and "Nobody tapped Connect within" in r.message and "Tap Refuse" in r.message and r.secret,
      f"probe nobody tapped: {r}")
Fake.tapped.clear()

# The socket is ignored (Luban connected, or an old prompt up): no stray bytes, a different message.
Fake.approve = "ignore"
started = time.monotonic()
r = sc.probe({"host": "127.0.0.1", "port": PORT}, None, timeout=1, wait=30)
check(not r.ok and "isn't answering" in r.message and "Luban" in r.message and r.secret
      and time.monotonic() - started < 6, f"probe ignored: {r}")

Fake.approve, Fake.machine = "auto", 3
r = sc.probe({"host": "127.0.0.1", "port": PORT}, token, timeout=2, wait=2)
check(r.ok and r.model == "Artisan" and r.secret == token, f"probe with a known token: {r}")
Fake.machine = 5
r = sc.probe({"host": "127.0.0.1", "port": PORT}, token, timeout=2, wait=2)
check(r.ok and r.model == "" and "isn't a J1" in r.message, f"probe of another machine: {r}")
Fake.machine = 4

r = sc.probe({"host": "127.0.0.1", "port": 1}, None, timeout=1)
check(not r.ok and "No answer from 127.0.0.1:1" in r.message, f"probe unreachable: {r}")

# ---- the worker ---------------------------------------------------------------------------
sc.SnapmakerSacpWorker.POLL_S = 0.1
sc.SnapmakerSacpWorker.RETRY_S = 0.1
sc.EXCEPTIONS_S = 0.5
key = poll.secret_key("snapmaker-sacp", "127.0.0.1", PORT)
store[(poll.SERVICE, key)] = token
entry = {"name": "J1", "type": "snapmaker-sacp", "host": "127.0.0.1", "port": PORT, "model": "J1"}
svc = pr.PrinterService([entry, {"name": "x", "type": "snapmaker-sacp"}],
                        secret_getter=lambda s, k: store.get((s, k)))
check(len(svc.workers) == 1 and type(svc.workers[0]).__name__ == "SnapmakerSacpWorker", "worker built, no-host skipped")
before = Fake.connections
svc.start()
check(wait_for(lambda: svc.message()["printers"][0]["state"] == "RUNNING"), "worker: state arrives")
check(wait_for(lambda: svc.message()["printers"][0]["file"] == "Bracket v2"), "worker: file arrives")
check(wait_for(lambda: svc.message()["printers"][0]["pct"] == 41), "worker: progress arrives")
p = svc.message()["printers"][0]
check(p["pct"] == 41 and p["nozzle"] == 249.6 and p["nozzle_target"] == 250 and p["bed"] == 59.9
      and p["speed"] == "120%" and p["remaining_min"] == 72 and p["model"] == "J1", f"worker message: {p}")
check({s[:2] for s in sc.SUBSCRIPTIONS} <= set(Fake.subs), f"subscriptions: {Fake.subs}")
check((1, 0x01, 0x21) in Fake.requests and (2, 0xac, 0x1a) in Fake.requests, "machine and file info asked")

Fake.exceptions, Fake.status = [(3, 2049, 7)], 4
check(wait_for(lambda: svc.message()["printers"][0]["error"] == "Left nozzle temperature error"),
      f"exception shows when paused: {svc.message()['printers'][0]}")
Fake.exceptions, Fake.status = [], 0
check(wait_for(lambda: svc.message()["printers"][0]["state"] == "IDLE"), "back to idle")
p = svc.message()["printers"][0]
check(p["file"] == "" and p["pct"] == 0 and p["speed"] == "" and p["error"] == "", f"idle shows no job: {p}")

# A new job on a screen that doesn't answer 0xac 0x1a: the name comes from the controller.
Fake.screen_file, Fake.status = False, 2
check(wait_for(lambda: svc.message()["printers"][0]["file"] == "Hinge"), "file name from the controller")
p = svc.message()["printers"][0]
check(p["state"] == "RUNNING" and p["pct"] == 0, f"no line count, no progress: {p}")
check(Fake.connections == before + 1, f"one connection kept open: {Fake.connections - before}")

# Garbage and truncated frames in the stream: still reads.
Fake.garbage, Fake.status = True, 8
check(wait_for(lambda: svc.message()["printers"][0]["state"] == "FINISH"), "reads through garbage")
Fake.garbage = False

w = svc.workers[0]

# The printer goes silent (Wi-Fi lost): reconnect with the stored token straight away, no prompt.
sc.STALE_STATUS_S = 2.0
Fake.status = 2
Fake.mute_conn = Fake.connections
check(wait_for(lambda: Fake.connections >= before + 2), "reconnects after the printer went silent")
check(wait_for(lambda: svc.message()["printers"][0]["state"] == "RUNNING"), "state after reconnect")
sc.STALE_STATUS_S = 10.0

# Disconnect tapped on the touchscreen (0x01 0x06): answered, then no reconnect for 15 minutes.
logs.clear()
n = Fake.connections
Fake.end_session = True
check(wait_for(lambda: any(a[:2] == (0x01, 0x06) for a in Fake.acks)), f"0x01 0x06 answered: {Fake.acks}")
check(wait_for(lambda: any("disconnected on the touchscreen; reconnecting in 15 min" in x for x in logs)),
      f"session end logged: {logs}")
time.sleep(1.5)
check(Fake.connections == n and w._hold and w._hold[0] - time.monotonic() > 14 * 60,
      f"no reconnect after Disconnect: {Fake.connections - n}, {w._hold}")
reset_at = time.monotonic()
w._hold = (0.0, w._hold[1])  # the 15 minutes are up
check(wait_for(lambda: w.last_at > reset_at), "reconnects and polls after the hold")
check(Fake.connections == n + 1, f"one new connection: {Fake.connections - n}")

# The touchscreen closes a session it had let in: taken the same way.
n = Fake.connections
Fake.drop_next = True
check(wait_for(lambda: w._hold is not None and w._hold[0] - time.monotonic() > 14 * 60), "socket closed: hold")
time.sleep(1)
check(Fake.connections == n, f"no reconnect after the screen closed the session: {Fake.connections - n}")
reset_at = time.monotonic()
w._hold = (0.0, w._hold[1])
check(wait_for(lambda: w.last_at > reset_at), "reconnects and polls after that hold")
check(Fake.connections == n + 1, f"one new connection after that hold: {Fake.connections - n}")

# Offline once the server stops.
logs.clear()
Fake.stop_flag.set()
srv.close()
for conn in Fake.clients:
    try:
        conn.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
check(wait_for(lambda: len(logs) >= 1, 15), "a lost printer logs a line")
check(w.message(now=time.monotonic() + pr.STALE_S + 1)["state"] == "OFFLINE", "stale -> OFFLINE")
time.sleep(0.5)
check(len(logs) == 1, f"one log line per outage: {logs}")
svc.stop()
w.join(3)

# A new server where the touchscreen asks: NotApproved with a hint, one socket, then it works.
Fake.stop_flag.clear()
srv = socket.create_server(("127.0.0.1", 0))
PORT2 = srv.getsockname()[1]
threading.Thread(target=accept_loop, args=(srv,), daemon=True).start()
Fake.approve = "prompt"
logs.clear()
entry2 = {"name": "J1s", "type": "snapmaker-sacp", "host": "127.0.0.1", "port": PORT2}
store[(poll.SERVICE, poll.secret_key("snapmaker-sacp", "127.0.0.1", PORT2))] = "token-2"
asking = pr.PrinterService([entry2], secret_getter=lambda s, k: store.get((s, k)))
before = Fake.connections
asking.start()
check(wait_for(lambda: len(logs) >= 1), "waiting for approval logs a line")
check(logs and "tap Connect" in logs[0], f"approval hint: {logs}")
time.sleep(3.0)  # longer than SILENT_S: the stray bytes keep the socket open
check(Fake.connections == before + 1, f"one socket while the prompt is up: {Fake.connections - before}")
check(asking.message()["printers"][0]["state"] == "OFFLINE", "offline until approved")
Fake.tapped.set()
check(wait_for(lambda: asking.message()["printers"][0]["state"] == "RUNNING"), "works after Connect")
check(asking.message()["printers"][0]["model"] == "J1", "default model")
asking.stop()
asking.workers[0].join(3)

# Refused from the worker: stops asking.
Fake.answer = 200
logs.clear()
store[(poll.SERVICE, poll.secret_key("snapmaker-sacp", "127.0.0.1", PORT2))] = "token-3"
refused = pr.PrinterService([entry2], secret_getter=lambda s, k: store.get((s, k)))
before = Fake.connections
refused.start()
time.sleep(0.5)
Fake.tapped.set()
check(wait_for(lambda: any("refused" in x and "restart the companion" in x for x in logs)), f"refusal logged: {logs}")
time.sleep(1)
check(Fake.connections == before + 1, f"no new prompts after a refusal: {Fake.connections - before}")
refused.stop()
Fake.answer = 0

# The touchscreen ignores the socket (Luban connected): give up after the silence, then back off.
Fake.approve = "ignore"
logs.clear()
ignored = pr.PrinterService([entry2], secret_getter=lambda s, k: "token-4")
before = Fake.connections
ignored.start()
check(wait_for(lambda: len(logs) >= 1), "ignored socket logs a line")
check(logs and "isn't answering" in logs[0] and "Luban" in logs[0], f"ignored hint: {logs}")
time.sleep(1)
wi = ignored.workers[0]
check(Fake.connections == before + 1 and wi._hold and 50 < wi._hold[0] - time.monotonic() <= 60,
      f"backs off a minute: {Fake.connections - before}, {wi._hold}")
wi._hold = (0.0, wi._hold[1])
check(wait_for(lambda: wi._hold is not None and wi._hold[0] - time.monotonic() > 100), "then two minutes")
check(Fake.connections == before + 2, f"one socket per try: {Fake.connections - before}")
ignored.stop()
ignored.workers[0].join(3)

# No token stored: a hint, no connection.
logs.clear()
before = Fake.connections
none = pr.PrinterService([entry2], secret_getter=lambda s, k: None)
none.start()
check(wait_for(lambda: len(logs) >= 1), "no token logs a line")
check("no Snapmaker token" in logs[0] and Fake.connections == before, f"no token: {logs}")
none.stop()

# Unreachable: offline, one log line, no crash.
logs.clear()
dead = pr.PrinterService([{"name": "Gone", "type": "snapmaker-sacp", "host": "127.0.0.1", "port": 1}],
                         secret_getter=lambda s, k: "t")
dead.start()
check(wait_for(lambda: len(logs) >= 1, 12), "unreachable logs a line")
time.sleep(0.5)
check(len(logs) == 1 and dead.message()["printers"][0]["state"] == "OFFLINE", f"unreachable: {logs}")
dead.stop()
poll.log.removeHandler(handler_)

check("printer" in enabled_pages({"printers": [entry]}), "printer page auto-added for snapmaker-sacp")

# ---- the CLI --------------------------------------------------------------------------------
tmp = pathlib.Path(tempfile.mkdtemp()) / "config.toml"
tmp.write_text('# my config\n[hub]\nhost = "x"\n', encoding="utf-8")
ADD = functools.partial(printers_cli.cmd_add_polled, spec=sc.SPEC)


def run(cmd, answers, passwords=()):
    a, p = iter(answers), iter(passwords)
    builtins.input = lambda prompt="": next(a)
    getpass.getpass = lambda prompt="": next(p)
    cmd(tmp)


sc.DEFAULT_PORT = PORT2  # the CLI never asks for a port; point the probe at the fake
Fake.approve = "auto"
Fake.known.clear()
threading.Timer(1.0, Fake.tapped.set).start()  # a new token: the touchscreen asks
store.clear()
run(ADD, ["127.0.0.1", "", ""])  # host, then accept the suggested name and model
cfg = tomllib.loads(tmp.read_text())
check(cfg["printers"] == [{"name": "Snapmaker J1", "type": "snapmaker-sacp", "host": "127.0.0.1"}],
      f"saved: {cfg.get('printers')}")
saved = store.get((poll.SERVICE, "snapmaker-sacp:127.0.0.1:8888"))
check(saved and saved == Fake.tokens[-1][2], f"token stored: {store}")
check("Snapmaker 127.0.0.1:8888" in printers_cli.describe(cfg["printers"][0]), "describe")
run(printers_cli.cmd_remove, ["1", "y"])
check(not store and tomllib.loads(tmp.read_text()).get("printers", []) == [], "removed with its token")

# Already configured: no connection test, so no prompt on the touchscreen. (A fresh file: adding
# after removing the last printer trips over the `printers = []` left behind.)
tmp.write_text('[hub]\nhost = "x"\n', encoding="utf-8")
threading.Timer(1.0, Fake.tapped.set).start()
run(ADD, ["127.0.0.1", "", ""])
n = len(Fake.tokens)
run(ADD, ["127.0.0.1"])
check(len(Fake.tokens) == n and len(tomllib.loads(tmp.read_text())["printers"]) == 1, "duplicate not probed")

# The test fails (remote connections off) but it's saved anyway: the token goes with it, so the
# worker has one to connect with.
tmp.write_text('[hub]\nhost = "x"\n', encoding="utf-8")
Fake.approve = "off"
store.clear()
run(ADD, ["127.0.0.1", "y", "", ""])
cfg = tomllib.loads(tmp.read_text())
check(len(cfg["printers"]) == 1 and store.get((poll.SERVICE, "snapmaker-sacp:127.0.0.1:8888")) == Fake.tokens[-1][2],
      f"saved anyway with a token: {cfg.get('printers')}, {store}")

Fake.stop_flag.set()
srv.close()
print("\nPROBLEMS:" if problems else "\nOK", problems if problems else "")
