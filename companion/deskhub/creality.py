"""Creality printers on stock firmware ("Creality OS", Klipper underneath), for the board's
printer page: K1 / K1C / K1 Max / K1 SE, K2 / K2 Plus / K2 Pro, Hi and Ender-3 V3 / V3 KE.

The printer serves a websocket at `ws://<ip>:9999/` with no login; Creality Print's device
manager uses the same one. On connect it sends its whole state as one JSON object, then only the
keys that changed (temperatures about once a second), so the worker merges every message into
one running state. `{"method":"get","params":{"ReqPrinterPara":1}}` asks for the whole state
again, and `{"method":"get","params":{"boxsInfo":1}}` for the CFS boxes, which the printer never
sends unasked. It also sends `{"ModeCode":"heart_beat"}`, which must be answered with the text
`ok`, and sometimes a bare `ok` of its own. Values often come as strings ("31.030000").

Sources the protocol details were checked against:
  - Creality Print 7.3.0's device manager (resources/web/deviceMgr in
    github.com/CrealityOfficial/CrealityPrint): the state codes and how it reads the fields
  - OrcaSlicer's Creality host (src/slic3r/Utils/CrealityPrint.cpp, CrealityPrintAgent.cpp in
    github.com/OrcaSlicer/OrcaSlicer): connect URL, heartbeat, the CFS reply
  - Home Assistant integration github.com/3dg1luk43/ha_creality_ws: ws_client.py, utils.py and
    its captures of a real K1C (tools/tests/fixtures/*.jsonl)
  - PrintGuard (github.com/oliverbravery/PrintGuard, integrations/creality.py) and TigerTag's
    protocol notes (github.com/TigerTag-Project/TigerTag-Studio-Manager,
    docs/creality-websocket-protocol.md)

Not confirmed anywhere: whether the Sonic Pad serves this websocket (nothing found says it
does; on Klipper with Moonraker it can be added as a Klipper printer instead).
"""
import inspect
import json
import math
import re
import time

from websockets.exceptions import InvalidStatus, WebSocketException
from websockets.sync.client import connect

from .calendar_feed import _safe_error
from .printer import MAX_AMS
from .printer import _num as _printer_num
from .printer_poll import BackendSpec, PollingWorker, ProbeResult, host_port, make_message
from .text import ascii_fold

DEFAULT_PORT = 9999
DEFAULT_MODEL = "Creality"
RESPONSE_S = 3.0           # after connecting, wait this long for the first state
ASK_S = 5.0                # ask for the whole state this often (ha_creality_ws does the same)
BOXES_S = 60.0             # and for the CFS boxes this often
STALE_STATUS_S = 30.0      # reconnect when nothing, heartbeats included, has come in for this long
MAX_MESSAGE = 16 * 2**20   # the file list and print history arrive as one large message

ASK_STATE = json.dumps({"method": "get", "params": {"ReqPrinterPara": 1}})
ASK_BOXES = json.dumps({"method": "get", "params": {"boxsInfo": 1}})

# `state`, as Creality Print's device manager names them: 0 idle, 1 printing, 2 complete,
# 3 failed, 4 aborted, 5 paused, 6 pausing, 7 stopping, 8 restoring (resuming). 9 is a job
# starting, seen for under a second on a real K1C (ha_creality_ws). The file name, progress and
# layers keep the last job's values after it ends, so only these states show them.
_STATES = {0: "IDLE", 1: "RUNNING", 2: "FINISH", 3: "FAILED", 4: "IDLE", 5: "PAUSE", 6: "PAUSE",
           7: "IDLE", 8: "RUNNING", 9: "PREPARE"}
_JOB_CODES = {1, 5, 6, 8, 9}
# Keys that are large and unused here (print history, file list).
_SKIP = {"historyList", "retGcodeFileInfo", "retGcodeFileInfo2"}

# Model codes the K2s, Ender-3 V3s and the Hi report as `model` (from ha_creality_ws and
# OrcaSlicer). K1s report a plain name ("K1C", "CR-K1 Max").
MODELS = {"F001": "Ender-3 V3", "F002": "Ender-3 V3 Plus", "F005": "Ender-3 V3 KE",
          "F008": "K2 Plus", "F012": "K2 Pro", "F018": "Hi", "F021": "K2"}

# websockets 15 reads proxy settings from the environment; the printer is on the LAN.
_CONNECT_OPTS = {"proxy": None} if "proxy" in inspect.signature(connect).parameters else {}


def _num(v) -> float | None:
    """printer._num (which takes numeric strings), but NaN and Infinity are unknown."""
    f = _printer_num(v)
    return f if f is not None and math.isfinite(f) else None


def _int(v) -> int:
    f = _num(v)
    return int(f) if f is not None else 0


def model_name(raw) -> str:
    """A readable model from the printer's `model`: "F012" -> "K2 Pro", "CR-K1 Max" -> "K1 Max"."""
    text = str(raw or "").strip()
    return MODELS.get(text.upper(), re.sub(r"^CR-(?=K)", "", text, flags=re.I))


def _file_name(path) -> str:
    name = str(path or "").replace("\\", "/").rsplit("/", 1)[-1]
    return re.sub(r"(\.gcode)?\.3mf$|\.(gcode|gco|g)$", "", name, flags=re.I)


def merge(state: dict, msg: dict):
    """Fold one message into the running state. Messages carry only the keys that changed;
    nested values (err, boxsInfo) come whole, so they replace the old ones."""
    for k, v in msg.items():
        if k not in _SKIP:
            state[k] = v


def board_state(st: dict) -> str:
    """One of the board's STATES from the merged state."""
    code = _int(st.get("state"))
    self_test = _num(st.get("withSelfTest"))
    # The start-of-print self-test (levelling, vibration) counts up from 1 to 100 when done.
    # It runs while `state` is already 1, and can also be started from the screen while idle.
    if self_test is not None and 1 <= self_test <= 99 and code in (0, 1, 9):
        return "PREPARE"
    return _STATES.get(code, "IDLE")


def _color(raw) -> str | None:
    """The printer writes colours as "#0RRGGBB" (a pad digit, then the colour); the last six
    hex digits are the colour. A multi-colour spool lists several; the first is used."""
    text = str(raw or "").split(",")[0].split(";")[0].strip().lstrip("#")
    if len(text) >= 6 and re.fullmatch(r"[0-9a-fA-F]+", text):
        return "#" + text[-6:].upper()
    return None


def _ams(st: dict) -> list[dict]:
    """CFS slots, first unit first, then the external spool holder. Read the way OrcaSlicer
    reads boxsInfo: a CFS box is `type` 0 with `state` 1, the holder is `type` 1; a slot with
    `state` 0, or with no type and no colour, is empty. The slot feeding the head is `selected`.
    Unconfirmed: whether newer K2 Plus firmware, which OrcaSlicer says marks the active slot
    with `state` 1 (2 for loaded but not in use), also sets `selected`."""
    info = st.get("boxsInfo")
    boxes = info.get("materialBoxs") if isinstance(info, dict) else None
    if not isinstance(boxes, list):
        return []
    cfs_off = st.get("cfsConnect") is not None and _int(st.get("cfsConnect")) == 0
    units, holder = [], []
    for box in boxes:
        if not isinstance(box, dict):
            continue
        kind = _int(box.get("type"))
        if kind == 0 and (cfs_off or _int(box.get("state")) != 1):
            continue
        if kind not in (0, 1):
            continue
        mats = box.get("materials")
        slots = []
        for m in mats if isinstance(mats, list) else []:
            if not isinstance(m, dict) or _int(m.get("state")) == 0:
                continue
            material, color = str(m.get("type") or "").strip(), _color(m.get("color"))
            if not material and not color:
                continue
            slots.append((_int(m.get("id")), {"color": color or "#808080", "type": material,
                                              "active": _int(m.get("selected")) == 1}))
        slots = [s for _, s in sorted(slots, key=lambda x: x[0])]
        if kind == 0:
            units.append((_int(box.get("id")), slots))
        else:
            holder += slots
    out = [s for _, slots in sorted(units, key=lambda x: x[0]) for s in slots] + holder
    if len(out) > MAX_AMS:
        # 3-4 CFS units have more slots than the board shows: keep the 8 around the one in use.
        active = next((i for i, s in enumerate(out) if s["active"]), 0)
        start = min(active // MAX_AMS * MAX_AMS, len(out) - MAX_AMS)
        out = out[start:start + MAX_AMS]
    return out


def build_message(name: str, model: str, st: dict) -> dict:
    """The board's `printer` entry from the merged state."""
    state = board_state(st)
    code = _int(st.get("state"))
    active = state in ("RUNNING", "PAUSE", "PREPARE") and code in _JOB_CODES
    job = active or state in ("FINISH", "FAILED")

    layer, layers = _int(st.get("layer")), _int(st.get("TotalLayer"))
    progress = _num(st.get("printProgress"))
    if progress is None and layers:
        progress = layer / layers * 100
    pct = 100 if state == "FINISH" else (progress or 0) if active else 0
    remaining_s = max(_int(st.get("printLeftTime")), 0) if active else 0  # seconds
    speed = _num(st.get("curFeedratePct"))

    # err stays set after some faults clear (ha_creality_ws, PrintGuard), so it only shows
    # when the job paused or failed. Creality Print shows its own text for each code; the
    # printer's `value` is usually empty, so the code is shown then.
    error = ""
    err = st.get("err")
    errcode = _int(err.get("errcode")) if isinstance(err, dict) else _int(err)
    if state in ("PAUSE", "FAILED") and errcode:
        value = err.get("value") if isinstance(err, dict) else ""
        error = value if isinstance(value, str) and value.isascii() and value.strip() else f"Error {errcode}"
    elif state == "FAILED":
        error = "Print stopped by an error"

    # boxTemp is the chamber sensor. Printers without one report maxBoxTemp 0 (TigerTag's
    # notes, from an Ender-3 V4); their boxTemp is left out.
    chamber = st.get("boxTemp")
    if st.get("maxBoxTemp") is not None and _num(st.get("maxBoxTemp")) == 0:
        chamber = None
    return make_message(
        name, model, state=state, pct=pct, remaining_min=remaining_s / 60,
        layer=layer if job else 0, layers=layers if job else 0,
        file=_file_name(st.get("printFileName")) if job else "",
        nozzle=_num(st.get("nozzleTemp")), nozzle_target=_num(st.get("targetNozzleTemp")),
        bed=_num(st.get("bedTemp0")), bed_target=_num(st.get("targetBedTemp0")),
        chamber=_num(chamber),
        speed=f"{round(speed)}%" if speed and active else "", error=error, ams=_ams(st))


class Link:
    """One websocket to the printer. Like OrcaSlicer, no subprotocol is asked for (the printer's
    own web page asks for "wsslicer"; ha_creality_ws does too, and either works)."""

    def __init__(self, host: str, port: int, timeout: float = 5):
        # No library pings: the printer's own heart_beat and the silence timeout cover liveness,
        # and ha_creality_ws turns them off too in case the printer doesn't answer them.
        self.ws = connect(f"ws://{host_port(host, port)}/", open_timeout=timeout, close_timeout=1,
                          compression=None, max_size=MAX_MESSAGE, ping_interval=None,
                          **_CONNECT_OPTS)
        self.heard = time.monotonic()

    def close(self):
        self.ws.close()

    def send(self, text: str):
        self.ws.send(text)  # a str goes as a text frame, which K1 firmware needs (OrcaSlicer)

    def recv(self, timeout: float) -> dict | None:
        """The next JSON object, or None when `timeout` runs out. Answers the heartbeat."""
        end = time.monotonic() + timeout
        while (left := end - time.monotonic()) > 0:
            try:
                raw = self.ws.recv(timeout=left)
            except TimeoutError:
                return None
            self.heard = time.monotonic()
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", "replace")
            try:
                msg = json.loads(raw)
            except ValueError:
                continue  # the printer's own "ok", and anything else that isn't JSON
            if not isinstance(msg, dict):
                continue
            if msg.get("ModeCode") == "heart_beat":
                self.ws.send("ok")  # plain text, not JSON
                continue
            return msg
        return None


class CrealityWorker(PollingWorker):
    type = "creality"
    default_port = DEFAULT_PORT
    default_model = DEFAULT_MODEL
    REOPEN_ON_ERROR = True

    def __init__(self, entry: dict, secret_getter):
        super().__init__(entry, secret_getter)
        self._state: dict = {}
        self._asked = self._asked_boxes = 0.0

    def open(self):
        link = Link(self.host, self.port)
        try:
            # The printer sends its whole state on connect; ask too, in case a firmware doesn't.
            link.send(ASK_STATE)
            link.send(ASK_BOXES)
            now = time.monotonic()
            self._asked = self._asked_boxes = now
            end = now + RESPONSE_S
            while (left := end - time.monotonic()) > 0 and "state" not in self._state:
                msg = link.recv(left)
                if msg is None:
                    break
                merge(self._state, msg)
            if "state" not in self._state:
                raise TimeoutError("the printer sent no status")
        except Exception:
            link.close()
            raise
        return link

    def poll(self, link) -> dict:
        now = time.monotonic()
        if now - self._asked >= ASK_S:
            link.send(ASK_STATE)
            self._asked = now
        # No CFS (cfsConnect 0) means no boxes to ask about after the first time.
        if now - self._asked_boxes >= BOXES_S and _int(self._state.get("cfsConnect", 1)):
            link.send(ASK_BOXES)
            self._asked_boxes = now
        # Take everything queued since the last poll (bounded, should the printer never pause).
        end = time.monotonic() + 1.0
        while time.monotonic() < end and (msg := link.recv(0.05)) is not None:
            merge(self._state, msg)
        if time.monotonic() - link.heard > STALE_STATUS_S:
            raise TimeoutError("nothing from the printer")
        return build_message(self.label, self.model, self._state)

    def reset(self):
        # A new connection gets the whole state again.
        self._state = {}


def probe(entry: dict, secret: str | None = None, timeout: float = 5) -> ProbeResult:
    """Check the websocket answers with printer status; suggests the printer's host name as its
    name and its model."""
    host, port = str(entry["host"]), int(entry.get("port") or DEFAULT_PORT)
    try:
        link = Link(host, port, timeout=timeout)
    except InvalidStatus as e:
        return ProbeResult(False, f"{host_port(host, port)} answered with HTTP {e.response.status_code}. "
                                  "Is this a Creality printer on stock firmware?")
    except (OSError, WebSocketException) as e:
        return ProbeResult(False, f"No websocket at {host_port(host, port)} ({_safe_error(e, '')}). Check the "
                                  "IP address and that the printer is on and on this network.")
    st: dict = {}
    try:
        link.send(ASK_STATE)
        end = time.monotonic() + timeout
        while (left := end - time.monotonic()) > 0 and not ("model" in st and "hostname" in st):
            msg = link.recv(left)
            if msg is None:
                break
            merge(st, msg)
    except (OSError, WebSocketException):
        pass
    finally:
        link.close()
    if not any(k in st for k in ("model", "hostname", "state", "nozzleTemp")):
        return ProbeResult(False, f"{host_port(host, port)} answered but sent no printer status. Is this a "
                                  "Creality printer on stock firmware?")
    model = ascii_fold(model_name(st.get("model")), 15)
    name = str(st.get("hostname") or "")
    return ProbeResult(True, f"Connected to the {model or DEFAULT_MODEL}.", name, model)


SPEC = BackendSpec(
    type="creality",
    menu="Creality (stock firmware): K1, K1C, K1 Max, K1 SE, K2, K2 Plus, K2 Pro, Hi, Ender-3 V3 / V3 KE",
    intro="""Creality printer (stock firmware)
  For the K1 / K1C / K1 Max / K1 SE, K2 / K2 Plus / K2 Pro, Hi and Ender-3 V3 / V3 KE.
  Talks to the printer over its local network connection (port 9999), the same way Creality
  Print does. No password or access code is needed; the printer and this PC just have to be
  on the same network. CFS colours show on the board.""",
    default_port=DEFAULT_PORT,
    default_model=DEFAULT_MODEL,
    probe=probe,
    secret_prompt=None,
    ask_port=False,
    brand="Creality",
)
WORKER = CrealityWorker
