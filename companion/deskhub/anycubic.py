"""Anycubic Kobra printers in LAN Mode, for the board's printer page.

Covers the Kobra 3 / 3 V2 / 3 Max, Kobra S1 / S1 Max, Kobra 4 and Kobra X on stock firmware,
with or without ACE Pro units. LAN Mode (printer screen: Settings > Network > LAN Mode) opens a
small HTTP service on port 18910 and an MQTT broker on 9883. The broker's login is not shown
anywhere; it comes from a handshake any client can do:

  GET  http://printer:18910/info   -> modelId, token (32 chars), ctrlInfoUrl, ctrlType
  POST <ctrlInfoUrl>?ts&nonce&sign&did, sign = md5(md5(token[:16]) + ts + nonce)
       -> data.info: AES-128-CBC (key token[16:32], IV data.token), PKCS#7
       -> {"broker": "mqtts://ip:9883", "username", "password", "deviceId"}

The login changes when the printer restarts, so a lost connection runs the handshake again.
Reports arrive on anycubic/anycubicCloud/v1/printer/public/<modelId>/<deviceId>/<type>/report;
queries go to .../web/printer/<modelId>/<deviceId>/<type>. Worked out from the community
projects anycubic_ha_local, anycubic-lan, kobra-connect and Rinkhals' MQTT notes; nothing here
comes from Anycubic. The Kobra 2 series has no LAN Mode on stock firmware (the Pro, Plus and
Max only talk to Anycubic's cloud; the Kobra 2 and Neo have no network), so it isn't covered.
"""
import base64
import hashlib
import json
import math
import random
import re
import ssl
import string
import threading
import time
from urllib.parse import urlencode, urlsplit

import httpx
import paho.mqtt.client as mqtt

from .calendar_feed import _safe_error
from .printer import MAX_AMS
from .printer import _num as _printer_num
from .printer_poll import BackendSpec, PollingWorker, ProbeResult, host_port, log, make_message

DEFAULT_PORT = 18910
PREFIX = "anycubic/anycubicCloud/v1"
USE_TLS = True        # the broker is TLS with a self-signed certificate; tests turn it off
OPEN_WAIT_S = 8.0     # after connecting, how long to wait for the first status report
INFO_S = 10.0         # the printer pushes temperatures and progress, but `info` only on request
BOX_S = 30.0          # the ACE answers only when asked
HANDSHAKE_BACKOFF_S = 30.0  # after a failed connect: each handshake may register a controller

SPEEDS = {1: "Silent", 2: "Standard", 3: "Sport"}
# project.state words (the firmware spells "stoped").
_PREPARE = {"preheating", "auto_leveling", "vibrating", "flow_calibrating", "downloading", "checking"}
_RUNNING = {"printing", "resuming", "resumed", "stopping"}
_PAUSED = {"pausing", "paused"}
_STOPPED = {"stoped", "stopped"}
_USER_CODES = {10401, 10601}  # paused / stopped by the user
_REASONS = {10107: "Filament broken", 10402: "Filament broken", 10121: "Bed heating fault",
            10122: "Nozzle heating fault", 11518: "Filament is stuck", 11519: "Filament tangled",
            11520: "ACE filament fault", 11801: "Spaghetti detected"}


def _num(v) -> float | None:
    """printer._num, but NaN and Infinity (json.loads accepts both) are unknown."""
    f = _printer_num(v)
    return f if f is not None and math.isfinite(f) else None


def _int(v) -> int:
    f = _num(v)
    return int(f) if f is not None else 0


class AnycubicError(Exception):
    """A failure the user can do something about; the text is shown as is."""


# ---- AES-128 decryption (the handshake's only use of it; no crypto package is installed) ----
def _rotl8(x: int, n: int) -> int:
    return ((x << n) | (x >> (8 - n))) & 0xFF


def _make_sbox() -> list[int]:
    sbox, p, q = [0] * 256, 1, 1
    while True:
        p = (p ^ (p << 1) ^ (0x1B if p & 0x80 else 0)) & 0xFF        # p * 3
        q ^= q << 1
        q ^= q << 2
        q ^= q << 4
        q &= 0xFF
        if q & 0x80:
            q ^= 0x09                                                # q / 3
        sbox[p] = q ^ _rotl8(q, 1) ^ _rotl8(q, 2) ^ _rotl8(q, 3) ^ _rotl8(q, 4) ^ 0x63
        if p == 1:
            break
    sbox[0] = 0x63
    return sbox


def _gmul(a: int, b: int) -> int:
    out = 0
    while b:
        if b & 1:
            out ^= a
        a = ((a << 1) ^ 0x1B) & 0xFF if a & 0x80 else a << 1
        b >>= 1
    return out


_SBOX = _make_sbox()
_INV_SBOX = [0] * 256
for _i, _v in enumerate(_SBOX):
    _INV_SBOX[_v] = _i
_M9, _M11, _M13, _M14 = ([_gmul(x, k) for x in range(256)] for k in (9, 11, 13, 14))


def _expand_key(key: bytes) -> list[list[int]]:
    """AES-128 key schedule -> 11 round keys of 16 bytes."""
    w = list(key)
    rcon = 1
    while len(w) < 176:
        t = w[-4:]
        if len(w) % 16 == 0:
            t = [_SBOX[t[1]] ^ rcon, _SBOX[t[2]], _SBOX[t[3]], _SBOX[t[0]]]
            rcon = _gmul(rcon, 2)
        w += [w[-16 + i] ^ t[i] for i in range(4)]
    return [w[i:i + 16] for i in range(0, 176, 16)]


def _decrypt_block(block: bytes, keys: list[list[int]]) -> list[int]:
    s = [b ^ k for b, k in zip(block, keys[10])]
    for rnd in range(9, -1, -1):
        s = [_INV_SBOX[s[r + 4 * ((c - r) % 4)]] for c in range(4) for r in range(4)]
        s = [b ^ k for b, k in zip(s, keys[rnd])]
        if rnd:
            out = []
            for c in range(0, 16, 4):
                a0, a1, a2, a3 = s[c:c + 4]
                out += [_M14[a0] ^ _M11[a1] ^ _M13[a2] ^ _M9[a3], _M9[a0] ^ _M14[a1] ^ _M11[a2] ^ _M13[a3],
                        _M13[a0] ^ _M9[a1] ^ _M14[a2] ^ _M11[a3], _M11[a0] ^ _M13[a1] ^ _M9[a2] ^ _M14[a3]]
            s = out
    return s


def aes_cbc_decrypt(data: bytes, key: bytes, iv: bytes) -> bytes:
    """AES-128-CBC with PKCS#7 padding."""
    if len(key) != 16 or len(iv) != 16 or not data or len(data) % 16:
        raise ValueError("bad AES input")
    keys, prev, out = _expand_key(key), iv, bytearray()
    for i in range(0, len(data), 16):
        block = data[i:i + 16]
        out += bytes(a ^ b for a, b in zip(_decrypt_block(block, keys), prev))
        prev = block
    pad = out[-1]
    if not 1 <= pad <= 16 or out[-pad:] != bytes([pad]) * pad:
        raise ValueError("bad padding")
    return bytes(out[:-pad])


# ---- the LAN handshake ------------------------------------------------------------
def sign(token: str, ts: int, nonce: str) -> str:
    first = hashlib.md5(token[:16].encode()).hexdigest()
    return hashlib.md5((first + str(ts) + nonce).encode()).hexdigest()


def _base(host: str, port: int) -> str:
    return f"http://{host_port(host, port)}"


def controller_id(host: str, port: int) -> str:
    """The `did` this PC shows the printer: the same on every handshake with it, so reconnects
    do not look like a new controller each time."""
    return hashlib.md5(f"deskhub-anycubic:{host}:{port}".encode()).hexdigest().upper()


def handshake(host: str, port: int = DEFAULT_PORT, timeout: float = 6) -> tuple[dict, dict]:
    """-> (the /info descriptor, the broker login {broker, username, password, deviceId})."""
    with httpx.Client(timeout=timeout, follow_redirects=False) as http:
        try:
            r = http.get(f"{_base(host, port)}/info")
            r.raise_for_status()
            info = r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise ConnectionError(f"no LAN Mode answer on {host_port(host, port)} ({_safe_error(e)})") from None
        if not isinstance(info, dict):
            raise AnycubicError("The device at this address did not answer like an Anycubic printer.")
        if info.get("ctrlType") == "cloud":
            raise AnycubicError("The printer is in cloud mode. Turn on LAN Mode on the printer "
                                "(Settings > Network > LAN Mode).")
        token, ctrl_url = info.get("token"), info.get("ctrlInfoUrl")
        if not (isinstance(token, str) and len(token) >= 32 and isinstance(ctrl_url, str) and info.get("modelId")):
            raise AnycubicError("This printer answered with a LAN handshake the companion doesn't know. "
                                "Updating the printer's firmware may help.")
        # ctrlInfoUrl names the printer's own address; keep its port and path but send the
        # request to the host the user entered.
        try:
            parts = urlsplit(ctrl_url)
            ctrl_port = parts.port or port
        except ValueError:
            raise AnycubicError("The printer's LAN Mode answer could not be read.") from None
        ts = int(time.time() * 1000)
        nonce = "".join(random.choices(string.ascii_letters + string.digits, k=6))
        did = controller_id(host, port)
        query = urlencode({"ts": ts, "nonce": nonce, "sign": sign(token, ts, nonce), "did": did})
        path = parts.path if parts.path.startswith("/") else "/" + parts.path
        url = f"{_base(host, ctrl_port)}{path}?{parts.query + '&' if parts.query else ''}{query}"
        try:
            r = http.post(url)
            r.raise_for_status()
            ctrl = r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise ConnectionError(f"the LAN Mode handshake failed ({_safe_error(e)})") from None
    sealed = ctrl.get("data") if isinstance(ctrl, dict) else None
    if not isinstance(ctrl, dict) or ctrl.get("code") != 200:
        code = ctrl.get("code") if isinstance(ctrl, dict) else None
        raise AnycubicError(f"The printer refused the LAN Mode handshake (code {code}). Check that LAN Mode is "
                            "on, and close Anycubic Slicer Next if it is connected to this printer.")
    try:
        iv = str(sealed["token"]).encode()[:16].ljust(16, b"\0")
        creds = json.loads(aes_cbc_decrypt(base64.b64decode(sealed["info"]), token[16:32].encode(), iv))
        m = re.match(r"mqtts?://[^/]*?:(\d+)", str(creds["broker"]))
        creds["broker_port"] = int(m.group(1)) if m else 9883
        for k in ("username", "password", "deviceId"):
            creds[k] = str(creds[k])
    except (KeyError, TypeError, ValueError):
        raise AnycubicError("The printer's LAN Mode answer could not be read.") from None
    return info, creds


# ---- reports -> the board's entry ---------------------------------------------------
def new_state() -> dict:
    return {"info": {}, "boxes": {}, "ext": {}, "reason": None}


def _merge_known(dst: dict, src: dict):
    """Copy what a partial report says; a missing value, None or -1 (unknown) keeps the old one."""
    for k, v in src.items():
        if v is not None and v != -1:
            dst[k] = v


def apply_report(state: dict, kind: str, report: dict):
    """Fold one report (the whole JSON envelope) into `state`."""
    data = report.get("data")
    if kind == "info" and isinstance(data, dict):
        state["info"] = data
    elif kind == "tempature" and isinstance(data, dict):  # the firmware's spelling
        if not isinstance(state["info"].get("temp"), dict):
            state["info"]["temp"] = {}
        _merge_known(state["info"]["temp"], data)
    elif kind == "print":
        phase = str(report.get("state") or "")
        code = report.get("code")
        if phase in _PREPARE | (_RUNNING - {"stopping"}) or phase in ("start", "started"):
            state["reason"] = None
        elif phase in _PAUSED | _STOPPED and isinstance(code, int) and code not in (200, *_USER_CODES):
            msg = report.get("msg")
            text = _REASONS.get(code) or (msg if isinstance(msg, str) and msg.isascii() and msg else f"Error {code}")
            state["reason"] = {"stopped": phase in _STOPPED, "text": text}
        project = state["info"].get("project")
        if isinstance(data, dict) and "progress" in data and isinstance(project, dict):
            _merge_known(project, {k: data.get(k) for k in
                                   ("progress", "curr_layer", "total_layers", "remain_time", "filename")})
    elif kind == "multiColorBox" and isinstance(data, dict):
        boxes = data.get("multi_color_box")
        for box in boxes if isinstance(boxes, list) else []:
            if not isinstance(box, dict) or "id" not in box:
                continue
            old = state["boxes"].setdefault(_int(box["id"]), {"slots": {}})
            if box.get("loaded_slot") is not None:
                old["loaded_slot"] = _int(box["loaded_slot"])
            slots = box.get("slots")
            for slot in slots if isinstance(slots, list) else []:
                if isinstance(slot, dict) and "index" in slot:
                    old["slots"][_int(slot["index"])] = slot
    elif kind == "extfilbox" and isinstance(data, dict):
        _merge_known(state["ext"], data)


def _color(rgb) -> str | None:
    if isinstance(rgb, list) and len(rgb) >= 3 and all(isinstance(c, int) and 0 <= c <= 255 for c in rgb[:3]):
        return "#{:02X}{:02X}{:02X}".format(*rgb[:3])
    return None


def _ams(state: dict) -> list[dict]:
    """ACE Pro slots, first unit first (a Kobra X's built-in box, id -1, last); the slot
    feeding the head is active. Without an ACE, the external spool if it reported one."""
    slots = []
    for box_id in sorted(state["boxes"], key=lambda i: (i < 0, i)):
        box = state["boxes"][box_id]
        loaded = box.get("loaded_slot", -1)
        for index in sorted(box["slots"]):
            s = box["slots"][index]
            kind, color = str(s.get("type") or ""), _color(s.get("color"))
            if not kind and not color:
                continue  # empty slot
            slots.append({"color": color or "#808080", "type": kind,
                          "active": index == loaded if loaded >= 0 else s.get("status") == 5})
    if not slots and (state["ext"].get("type") or _color(state["ext"].get("color"))):
        ext = state["ext"]
        slots.append({"color": _color(ext.get("color")) or "#808080", "type": str(ext.get("type") or ""),
                      "active": bool(ext.get("loaded"))})
    return slots[:MAX_AMS]


def _file_name(path) -> str:
    name = str(path or "").replace("\\", "/").rsplit("/", 1)[-1]
    return re.sub(r"(\.gcode)?\.3mf$|\.(gcode|gco|g)$", "", name, flags=re.I)


def build_message(name: str, model: str, state: dict) -> dict:
    """The board's `printer` entry from the merged reports."""
    info = state["info"]
    project = info.get("project") if isinstance(info.get("project"), dict) else None
    last = info.get("last_project") if isinstance(info.get("last_project"), dict) else {}
    reason = state.get("reason")
    phase = str((project or {}).get("state") or "")
    error = ""
    if info.get("state") != "free" and project:
        if phase in _PAUSED or (project.get("pause") in (1, 2) and phase not in _STOPPED):
            status = "PAUSE"
        elif phase in _RUNNING:
            status = "RUNNING"
        elif phase == "finished":
            status = "FINISH"
        elif phase in _STOPPED:
            status = "IDLE"
        else:
            status = "PREPARE"  # preheating, leveling, calibrating and words not seen yet
    elif str(last.get("state") or "") == "finished":
        status, project = "FINISH", last
    else:
        status, project = "IDLE", None
    if reason and status == "PAUSE" and not reason["stopped"]:
        error = reason["text"]
    elif reason and reason["stopped"] and status == "IDLE":
        status, error = "FAILED", reason["text"]
    active = status in ("RUNNING", "PAUSE", "PREPARE")
    shown = active or status == "FINISH"
    project = project or {}
    temp = info.get("temp") if isinstance(info.get("temp"), dict) else {}
    return make_message(
        name, model, state=status,
        pct=100 if status == "FINISH" else _int(project.get("progress")) if active else 0,
        remaining_min=_int(project.get("remain_time")) if active else 0,
        layer=_int(project.get("curr_layer")) if shown else 0,
        layers=_int(project.get("total_layers")) if shown else 0,
        file=_file_name(project.get("filename")) if shown else "",
        nozzle=_num(temp.get("curr_nozzle_temp")), nozzle_target=_num(temp.get("target_nozzle_temp")),
        bed=_num(temp.get("curr_hotbed_temp")), bed_target=_num(temp.get("target_hotbed_temp")),
        chamber=_num(temp.get("curr_chamber_temp")),
        speed=SPEEDS.get(_int(info.get("print_speed_mode")), ""), error=error, ams=_ams(state))


# ---- the MQTT session ----------------------------------------------------------------
class Session:
    """One login on the printer's broker: merged state plus the paho client."""

    def __init__(self, host: str, info: dict, creds: dict):
        self.model_id, self.device_id = str(info["modelId"]), creds["deviceId"]
        self.info = info
        self.state = new_state()
        self.lock = threading.Lock()
        self.connected = False
        self.refused = None
        self.got_info = threading.Event()
        self.answered = threading.Event()
        self.last_report = 0.0
        self.next_info = self.next_box = 0.0
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                             client_id=f"deskhub-{random.getrandbits(32):08x}")
        client.username_pw_set(creds["username"], creds["password"])
        if USE_TLS:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE  # self-signed
            client.tls_set_context(ctx)
        client.on_connect, client.on_disconnect, client.on_message = \
            self._on_connect, self._on_disconnect, self._on_message
        self.client = client
        client.connect_async(host, creds["broker_port"], keepalive=30)
        client.loop_start()

    def _topic(self, side: str) -> str:
        return f"{PREFIX}/{side}/{self.model_id}/{self.device_id}"

    def _on_connect(self, client, userdata, flags, rc, props=None):
        if rc.is_failure:
            self.refused = rc
            self.answered.set()
            return
        self.connected = True
        self.answered.set()
        client.subscribe(self._topic("printer/public") + "/#")
        self.next_info = self.next_box = 0.0
        self.query()

    def _on_disconnect(self, client, userdata, flags, rc=None, props=None):
        self.connected = False

    def _on_message(self, client, userdata, msg):
        try:
            report = json.loads(msg.payload)
        except ValueError:
            return
        if not isinstance(report, dict):
            return
        parts = msg.topic.split("/")
        kind = str(report.get("type") or (parts[-2] if parts[-1] == "report" else parts[-1]))
        with self.lock:
            try:
                apply_report(self.state, kind, report)
            except Exception as e:  # raised here, it would end paho's network thread
                log.debug("anycubic: dropped a %s report (%s)", kind[:20], type(e).__name__)
                return
            self.last_report = time.monotonic()
        if kind == "info" and isinstance(report.get("data"), dict):
            self.got_info.set()

    def _send(self, kind: str, action: str):
        payload = {"type": kind, "action": action, "timestamp": int(time.time() * 1000),
                   "msgid": f"{random.getrandbits(128):032x}", "data": None}
        self.client.publish(f"{self._topic('web/printer')}/{kind}", json.dumps(payload))

    def query(self):
        """Ask for `info` every INFO_S and the ACE / external spool every BOX_S."""
        now = time.monotonic()
        if now >= self.next_info:
            self._send("info", "query")
            self.next_info = now + INFO_S
        if now >= self.next_box:
            self._send("multiColorBox", "getInfo")
            self._send("extfilbox", "query")
            self.next_box = now + BOX_S

    def wait_ready(self, timeout: float):
        """Wait for the login and the first `info` report; raises if either fails."""
        if not self.answered.wait(timeout):
            raise ConnectionError("the printer's MQTT broker did not answer")
        if self.refused is not None:
            raise AnycubicError(f"The printer's broker refused the LAN Mode login ({self.refused}).")
        if not self.got_info.wait(timeout):
            raise ConnectionError("connected, but the printer sent no status")

    def message(self, name: str, model: str) -> dict:
        with self.lock:
            return build_message(name, model, self.state)

    def close(self):
        self.client.loop_stop()
        try:
            self.client.disconnect()
        except Exception:
            pass


def connect(host: str, port: int = DEFAULT_PORT, timeout: float = OPEN_WAIT_S) -> Session:
    info, creds = handshake(host, port, timeout=timeout)
    session = Session(host, info, creds)
    try:
        session.wait_ready(timeout)
    except Exception:
        session.close()
        raise
    return session


class AnycubicWorker(PollingWorker):
    type = "anycubic"
    default_port = DEFAULT_PORT
    default_model = "Anycubic"
    REOPEN_ON_ERROR = True  # a new login after every drop: it changes when the printer restarts
    HANDSHAKE_BACKOFF_S = HANDSHAKE_BACKOFF_S

    def __init__(self, entry: dict, secret_getter):
        super().__init__(entry, secret_getter)
        self._next_open = 0.0

    def open(self):
        # A drop reconnects at once (the printer may have restarted with a new login), but
        # after a failed attempt the next handshake waits HANDSHAKE_BACKOFF_S.
        delay = self._next_open - time.monotonic()
        if delay > 0 and self.stop_event.wait(delay):
            raise ConnectionError("stopping")
        try:
            return connect(self.host, self.port)
        except Exception:
            self._next_open = time.monotonic() + self.HANDSHAKE_BACKOFF_S
            raise

    def close(self, session):
        session.close()

    def hint(self, e):
        return str(e) if isinstance(e, AnycubicError) else None

    def poll(self, session) -> dict:
        if not session.connected:
            raise ConnectionError("lost the connection to the printer's broker")
        if time.monotonic() - session.last_report > INFO_S * 3 + 5:
            raise ConnectionError("the printer stopped sending status")
        session.query()
        return session.message(self.label, self.model)


def _short_model(name) -> str:
    return re.sub(r"^anycubic\s+", "", str(name or ""), flags=re.I).strip()


def probe(entry: dict, secret: str | None = None, timeout: float = 5) -> ProbeResult:
    """Run the handshake and wait for a status report. LAN Mode needs no credential."""
    host, port = str(entry["host"]), int(entry.get("port") or DEFAULT_PORT)
    try:
        session = connect(host, port, timeout=timeout)
    except AnycubicError as e:
        return ProbeResult(False, str(e))
    except (ConnectionError, OSError) as e:
        return ProbeResult(False, f"No LAN Mode answer from {host_port(host, port)} ({_safe_error(e)}). Check the "
                                  "IP address, that LAN Mode is on (Settings > Network > LAN Mode), and that "
                                  "the PC and printer are on the same network.")
    try:
        with session.lock:
            data = session.state["info"]
            model = _short_model(session.info.get("modelName") or data.get("model"))
            name = str(data.get("printerName") or session.info.get("deviceName") or model or "")
        return ProbeResult(True, "Connected and the printer is reporting status.", name, model)
    finally:
        session.close()


SPEC = BackendSpec(
    type="anycubic",
    menu="Anycubic (LAN Mode): Kobra 3, Kobra S1, Kobra 4, Kobra X, with ACE Pro",
    intro="""Anycubic printer (LAN Mode)
  For the Kobra 3 / 3 V2 / 3 Max, Kobra S1 / S1 Max, Kobra 4 and Kobra X on stock firmware,
  with or without ACE Pro. Turn on LAN Mode on the printer: Settings > Network > LAN Mode.
  Note: LAN Mode takes the printer out of the Anycubic cloud and app until you pair it again.
  No password is needed: the companion asks the printer for its login each time it connects.
  The printer answers on port 18910. The Kobra 2 series has no LAN Mode and is not supported.""",
    default_port=DEFAULT_PORT,
    default_model="Anycubic",
    probe=probe,
    brand="Anycubic LAN",
)
WORKER = AnycubicWorker
