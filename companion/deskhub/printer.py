"""Bambu Lab printer status over MQTT (LAN or cloud), for the board's printer page.

Each printer gets a worker thread running a paho client. Printers send partial updates,
so reports are merged into a cached state; `PrinterService.message()` turns that state
into the `printer` message for the board.
"""
import copy
import json
import logging
import re
import ssl
import threading
import time

import paho.mqtt.client as mqtt

from .bambu_cloud import SERVICE, AuthError, BambuError, get_uid, make_client, mqtt_host
from .calendar_feed import _safe_error
from .text import ascii_fold

log = logging.getLogger(__name__)

MAX_PRINTERS = 4
MAX_AMS = 8
STALE_S = 60
PUSHALL_S = 300
PUSHALL = '{"pushing":{"sequence_id":"0","command":"pushall"}}'
SPEEDS = {1: "Silent", 2: "Standard", 3: "Sport", 4: "Ludicrous"}
# Serial number prefixes (first 3 characters) per model.
MODELS = {"00M": "X1C", "03W": "X1E", "01S": "P1P", "01P": "P1S",
          "030": "A1 mini", "039": "A1", "094": "H2D"}
_AUTH_CODES = {4, 5, 134, 135}
_COLOR = re.compile(r"[0-9a-fA-F]{6}([0-9a-fA-F]{2})?")


def lan_key(serial: str) -> str:
    return f"lan:{serial}"


def model_of(entry: dict) -> str:
    return ascii_fold(entry.get("model") or MODELS.get(str(entry.get("serial", ""))[:3], ""), 20)


def _merge(dst: dict, src: dict):
    """Merge a partial report into the cached one. Lists of {'id': ...} items (AMS units,
    trays, extruders) are merged item by item; anything else is replaced."""
    for k, v in src.items():
        old = dst.get(k)
        if isinstance(v, dict) and isinstance(old, dict):
            _merge(old, v)
        elif (isinstance(v, list) and isinstance(old, list)
              and all(isinstance(x, dict) and "id" in x for x in v + old)):
            by_id = {str(x["id"]): x for x in old}
            for x in v:
                if str(x["id"]) in by_id:
                    _merge(by_id[str(x["id"])], x)
                else:
                    old.append(copy.deepcopy(x))
        else:
            dst[k] = copy.deepcopy(v)


def _num(v) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
        return None
    try:
        f = round(float(v), 1)
    except (ValueError, OverflowError):
        return None
    return int(f) if f.is_integer() else f


def _int(v) -> int:
    f = _num(v)
    return int(f) if f is not None else 0


def _packed(v) -> tuple[float | None, float | None]:
    """H2D packs temperatures into one integer: current in the low 16 bits, target in the high."""
    if isinstance(v, int) and not isinstance(v, bool):
        return _num(v & 0xFFFF), _num((v >> 16) & 0xFFFF)
    return _num(v), None


def offline_message(name: str, model: str) -> dict:
    """The board's entry for a printer we can't reach (same shape for every brand)."""
    return {"name": ascii_fold(name, 24), "model": model, "online": False, "state": "OFFLINE",
            "pct": 0, "remaining_min": 0, "layer": 0, "layers": 0, "file": "", "nozzle": None,
            "nozzle_target": None, "bed": None, "bed_target": None, "chamber": None, "speed": "",
            "error": "", "ams": []}


class PrinterState:
    def __init__(self):
        self.data: dict = {}
        self.last_data = 0.0
        self.connected = False
        self.lock = threading.Lock()

    def merge(self, report: dict, now: float | None = None):
        with self.lock:
            _merge(self.data, report)
            self.last_data = time.monotonic() if now is None else now

    @staticmethod
    def _temps(d: dict) -> dict:
        """Nozzle, bed and chamber temperatures: classic fields first, H2D layout as fallback."""
        dev = d.get("device") if isinstance(d.get("device"), dict) else {}
        out = {"nozzle": _num(d.get("nozzle_temper")), "nozzle_target": _num(d.get("nozzle_target_temper")),
               "bed": _num(d.get("bed_temper")), "bed_target": _num(d.get("bed_target_temper")),
               "chamber": _num(d.get("chamber_temper"))}
        ext = dev.get("extruder") if isinstance(dev.get("extruder"), dict) else {}
        info = [x for x in ext.get("info") or [] if isinstance(x, dict)]
        if info:
            # Dual-nozzle printers (H2D, X2D...) also fill the classic fields, but with
            # nozzle 0 even while nozzle 1 is printing. The active nozzle's id is in
            # bits 4-7 of extruder.state, so prefer that one.
            cur = (_int(ext.get("state")) >> 4) & 0xF
            pick = next((x for x in info if _int(x.get("id")) == cur), info[0])
            now_t, target = _packed(pick.get("temp"))
            if now_t is not None:
                out["nozzle"] = now_t
            if target is not None:
                out["nozzle_target"] = target
        for key, section in (("bed", "bed"), ("chamber", "ctc")):
            sec = dev.get(section)
            sinfo = sec.get("info") if isinstance(sec, dict) else None
            if isinstance(sinfo, dict) and out[key] is None:
                out[key], target = _packed(sinfo.get("temp"))
                if key == "bed" and out["bed_target"] is None:
                    out["bed_target"] = target
        return out

    @staticmethod
    def _ams(d: dict) -> list[dict]:
        ams = d.get("ams") if isinstance(d.get("ams"), dict) else {}
        now = str(ams.get("tray_now", ""))
        slots = []
        for i, unit in enumerate(ams.get("ams") or []):
            if not isinstance(unit, dict):
                continue
            uid = _int(unit.get("id", i))
            for t in unit.get("tray") or []:
                if not isinstance(t, dict):
                    continue
                color = str(t.get("tray_color") or "")
                if not _COLOR.fullmatch(color) and not t.get("tray_type"):
                    continue  # empty slot
                slots.append({"color": "#" + color[:6].upper() if _COLOR.fullmatch(color) else "#808080",
                              "type": ascii_fold(t.get("tray_type"), 12),
                              "active": now == str(uid * 4 + _int(t.get("id")))})
        return slots[:MAX_AMS]

    def message(self, name: str, model: str, now: float | None = None) -> dict:
        now = time.monotonic() if now is None else now
        with self.lock:
            d = copy.deepcopy(self.data)
            fresh = self.connected and self.last_data and now - self.last_data <= STALE_S
        if not fresh:
            return offline_message(name, model)
        base = {"name": ascii_fold(name, 24), "model": model}
        file = str(d.get("subtask_name") or d.get("gcode_file") or "").replace("\\", "/").rsplit("/", 1)[-1]
        file = re.sub(r"(\.gcode)?\.3mf$|\.gcode$", "", file, flags=re.I)
        code = _int(d.get("print_error"))
        hexcode = f"{code:08X}"
        return {**base, "online": True,
                "state": ascii_fold(str(d.get("gcode_state") or "IDLE").upper(), 12),
                "pct": _int(d.get("mc_percent")), "remaining_min": _int(d.get("mc_remaining_time")),
                "layer": _int(d.get("layer_num")), "layers": _int(d.get("total_layer_num")),
                "file": ascii_fold(file, 40), **self._temps(d),
                "speed": SPEEDS.get(_int(d.get("spd_lvl")), ""),
                "error": f"Error {hexcode[:4]}-{hexcode[4:]}" if code else "",
                "ams": self._ams(d)}


class Worker(threading.Thread):
    """One printer's MQTT connection. Reconnects on its own; stops for good on a rejected login."""

    def __init__(self, entry: dict, secret_getter, client_factory=mqtt.Client,
                 host: str | None = None, port: int = 8883, tls: bool = True):
        super().__init__(daemon=True, name=f"printer-{entry.get('name', '')}")
        self.entry = entry
        self.label = str(entry.get("name") or entry.get("serial") or "Printer")
        self.serial = str(entry["serial"])
        self.cloud = entry.get("type") == "cloud"
        self.get_secret = secret_getter
        self.factory = client_factory
        self.host, self.port, self.tls = host, port, tls
        self.state = PrinterState()
        self.stop_event = threading.Event()
        self.next_push = 0.0
        self.client = None

    def _auth_failed(self):
        if self.cloud:
            log.error("printer %s: Bambu cloud login was rejected or has expired. Run `deskhub-printers` "
                      "(or `deskhub-setup`) to sign in again.", self.label)
        else:
            log.error("printer %s: the printer rejected the access code. Check it on the printer's "
                      "screen and run `deskhub-printers` to update it.", self.label)
        self.stop_event.set()

    def _credentials(self) -> tuple[str, str, str]:
        """-> (host, user, password)."""
        if self.cloud:
            account, region = str(self.entry.get("account", "")), str(self.entry.get("region", "us"))
            token = self.get_secret(SERVICE, account)
            if not token:
                raise AuthError("no stored token")
            with make_client() as http:
                uid = get_uid(http, region, token)
            return self.host or mqtt_host(region), f"u_{uid}", token
        code = self.get_secret(SERVICE, lan_key(self.serial))
        if not code:
            raise AuthError("no stored access code")
        return self.host or str(self.entry.get("host", "")), "bblp", code

    def _publish_pushall(self):
        self.client.publish(f"device/{self.serial}/request", PUSHALL)
        self.next_push = time.monotonic() + PUSHALL_S

    # paho 2.x callbacks
    def _on_connect(self, client, userdata, flags, rc, props=None):
        if rc.is_failure:
            if rc.value in _AUTH_CODES:
                self._auth_failed()
            else:
                log.info("printer %s: MQTT connect refused (%s)", self.label, rc)
            return
        self.state.connected = True
        log.info("printer %s: connected", self.label)
        client.subscribe(f"device/{self.serial}/report")
        self._publish_pushall()

    def _on_disconnect(self, client, userdata, flags, rc=None, props=None):
        if self.state.connected:
            log.info("printer %s: disconnected", self.label)
        self.state.connected = False

    def _on_message(self, client, userdata, msg):
        try:
            report = json.loads(msg.payload).get("print")
        except (ValueError, AttributeError):
            return
        if isinstance(report, dict):
            self.state.merge(report)

    def run(self):
        while True:
            if self.stop_event.is_set():
                return
            try:
                host, user, password = self._credentials()
                break
            except AuthError:
                self._auth_failed()
                return
            except (BambuError, OSError) as e:
                log.warning("printer %s: %s; retrying", self.label, _safe_error(e))
                self.stop_event.wait(30)
        client = self.factory(mqtt.CallbackAPIVersion.VERSION2,
                              client_id=f"deskhub-{self.serial[-6:]}-{id(self) % 9999}")
        self.client = client
        client.username_pw_set(user, password)
        if self.tls:
            if self.cloud:
                ctx = ssl.create_default_context()
            else:  # the printer's certificate is self-signed
                ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            client.tls_set_context(ctx)
        client.on_connect, client.on_disconnect, client.on_message = \
            self._on_connect, self._on_disconnect, self._on_message
        client.reconnect_delay_set(2, 30)
        client.connect_async(host, self.port, keepalive=30)
        client.loop_start()
        try:
            while not self.stop_event.wait(1):
                if self.state.connected and time.monotonic() >= self.next_push:
                    self._publish_pushall()
        finally:
            client.loop_stop()
            try:
                client.disconnect()
            except Exception:
                pass

    def stop(self):
        self.stop_event.set()

    def message(self) -> dict:
        return self.state.message(self.label, model_of(self.entry))


def keyring_secret(service: str, key: str) -> str | None:
    import keyring
    try:
        return keyring.get_password(service, key)
    except Exception as e:
        log.warning("credential store unavailable: %s", _safe_error(e))
        return None


class PrinterService:
    """All configured printers. Bambu entries (type "local"/"cloud") need a serial; the
    network-polled brands in printer_types.py need a host."""

    def __init__(self, entries: list[dict], secret_getter=keyring_secret, **worker_kw):
        from . import printer_types
        self.workers = []
        for e in entries:
            if len(self.workers) >= MAX_PRINTERS:
                break
            kind = e.get("type")
            if kind in printer_types.BACKENDS:
                if not e.get("host"):
                    continue
                try:
                    worker_cls = printer_types.module(kind).WORKER
                except ModuleNotFoundError:
                    log.error("printer %s: type %r isn't supported by this version", e.get("name"), kind)
                    continue
                self.workers.append(worker_cls(e, secret_getter))
            elif kind in (None, "local", "cloud") and e.get("serial"):
                self.workers.append(Worker(e, secret_getter, **worker_kw))
            else:
                log.error("printer %s: unknown type %r", e.get("name"), kind)

    def start(self):
        for w in self.workers:
            w.start()

    def stop(self, timeout: float = 2.0):
        """Stop every worker and give each a moment to close its connection cleanly (release
        printer control, end the session), since they are daemon threads."""
        for w in self.workers:
            w.stop()
        for w in self.workers:
            if w.is_alive():
                w.join(timeout)

    def message(self) -> dict:
        return {"type": "printer", "printers": [w.message() for w in self.workers]}


def probe_local(host: str, serial: str, code: str, timeout: float = 10, port: int = 8883,
                tls: bool = True) -> tuple[bool, str]:
    """Try a LAN connection for up to `timeout` seconds. -> (ok, message for the user)."""
    result = {"rc": None, "data": False}
    done = threading.Event()
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"deskhub-probe-{int(time.time()) % 10000}")
    c.username_pw_set("bblp", code)
    if tls:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        c.tls_set_context(ctx)

    def on_connect(cl, ud, flags, rc, props=None):
        result["rc"] = rc
        if rc.is_failure:
            done.set()
            return
        cl.subscribe(f"device/{serial}/report")
        cl.publish(f"device/{serial}/request", PUSHALL)

    def on_message(cl, ud, msg):
        result["data"] = True
        done.set()

    c.on_connect, c.on_message = on_connect, on_message
    try:
        c.connect_async(host, port, keepalive=30)
        c.loop_start()
        done.wait(timeout)
    finally:
        c.loop_stop()
        try:
            c.disconnect()
        except Exception:
            pass
    rc = result["rc"]
    if rc is None:
        return False, (f"No answer from {host}:{port} within {timeout:.0f}s. Check the IP address, that the "
                       "PC and printer are on the same network, and that LAN-only or Developer Mode is on.")
    if rc.is_failure:
        if rc.value in _AUTH_CODES:
            return False, "The printer rejected the access code. Check the 8 characters on its screen."
        return False, f"The printer refused the connection ({rc})."
    if not result["data"]:
        return True, ("Connected, but the printer sent no status yet. Is the serial number right? "
                      "It is saved anyway.")
    return True, "Connected and the printer is reporting status."
