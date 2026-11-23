"""Snapmaker 2.0 (A150, A250, A350) through the touchscreen's HTTP API on port 8080, the one
Snapmaker Luban uses over Wi-Fi.

Pairing: `POST /api/v1/connect` without a token answers with a new token and puts a prompt on
the touchscreen. Until someone taps Yes, `GET /api/v1/status?token=...` answers 204 (empty); after
that it returns the status JSON. The token is stored, and each new session starts with
`POST /api/v1/connect` carrying it. A 401 means the printer doesn't accept the token (any more).

The J1, J1s and Artisan speak SACP on port 8888 instead and go through snapmaker_sacp.py. The Snapmaker
U1 runs Klipper and goes through the Moonraker backend.
"""
import re
import socket
import time

import httpx

from .calendar_feed import _safe_error
from .printer import _num
from .printer_poll import BackendSpec, PollingWorker, ProbeResult, host_port, make_message

DEFAULT_PORT = 8080
SACP_PORT = 8888
APPROVAL_WAIT_S = 60
TOKEN_HINT = "Snapmaker refused the token; run deskhub-printers again and approve on the touchscreen"


class NotApproved(Exception):
    """The printer answered 204: its touchscreen is asking whether to let this PC in."""


def _model(series) -> str:
    """'Snapmaker 2.0 A350' (or 'A350') -> 'Snapmaker A350'."""
    m = re.search(r"A[123]50T?", str(series or ""), re.I)
    return f"Snapmaker {m.group(0).upper()}" if m else "Snapmaker"


def _file_name(name) -> str:
    name = str(name or "").replace("\\", "/").rsplit("/", 1)[-1]
    return re.sub(r"\.(gcode|g|gco|nc|cnc)$", "", name, flags=re.I)


def _nozzle(status: dict) -> tuple:
    """(temperature, target) of the nozzle in use. A dual extruder reports nozzleTemperature1/2
    (some notes spell it nozzle1Temperature); the status doesn't say which one is active, so it's
    the heated one, else the first."""
    pairs = []
    for i in (1, 2):
        cur = status.get(f"nozzleTemperature{i}", status.get(f"nozzle{i}Temperature"))
        tgt = status.get(f"nozzleTargetTemperature{i}", status.get(f"nozzle{i}TargetTemperature"))
        if cur is not None:
            pairs.append((cur, tgt))
    if not pairs:
        return status.get("nozzleTemperature"), status.get("nozzleTargetTemperature")
    return next((p for p in pairs if (_num(p[1]) or 0) > 0), pairs[0])


def build_message(name: str, model: str, status: dict) -> dict:
    """The board's `printer` entry from a /api/v1/status reply."""
    raw = str(status.get("status") or "IDLE").upper()
    progress = status.get("progress")  # 0-1; not _num, which rounds to 0.1
    progress = max(0.0, min(float(progress), 1.0)) if isinstance(progress, (int, float)) else 0.0
    lines, total = _num(status.get("currentLine")) or 0, _num(status.get("totalLines")) or 0
    file = _file_name(status.get("fileName"))

    if raw == "RUNNING":
        state = "RUNNING" if progress > 0 or lines > 0 else "PREPARE"  # heating up
    elif raw == "PAUSED":
        state = "PAUSE"
    elif file and (progress >= 1 or (total and lines >= total)):
        state = "FINISH"  # back to IDLE with the last job's file and counters still there
    else:
        state = "IDLE"  # IDLE, STOPPED (a cancelled job) and anything unknown
    active = state in ("RUNNING", "PAUSE", "PREPARE")

    remaining = 0
    if active:
        remaining = _num(status.get("remainingTime"))
        elapsed = _num(status.get("elapsedTime")) or 0
        if remaining is None and progress > 0.01 and elapsed:
            remaining = elapsed / progress - elapsed

    error = ""
    if status.get("isEnclosureDoorOpen") is True and active:
        error = "Enclosure door is open"
    elif status.get("isFilamentOut") is True and active:
        error = "Filament ran out"

    temps = {}
    if str(status.get("toolHead") or "TOOLHEAD_3DPRINTING").startswith("TOOLHEAD_3DPRINTING"):
        nozzle, nozzle_target = _nozzle(status)
        temps = {"nozzle": nozzle, "nozzle_target": nozzle_target,
                 "bed": status.get("heatedBedTemperature"),
                 "bed_target": status.get("heatedBedTargetTemperature")}
    # Laser and CNC heads: the job's state and progress, no temperatures.

    return make_message(name, model, state=state,
                        pct=100 if state == "FINISH" else progress * 100,
                        remaining_min=(remaining or 0) / 60,
                        file=file if active or state == "FINISH" else "", error=error, **temps)


class SnapmakerWorker(PollingWorker):
    type = "snapmaker"
    default_port = DEFAULT_PORT
    default_model = "Snapmaker"

    TIMEOUT = 5

    def __init__(self, entry: dict, secret_getter):
        super().__init__(entry, secret_getter)
        self._connected = False  # this session's POST /api/v1/connect went through
        self._waiting = False    # the touchscreen is asking to approve this token
        self._rejected = False   # the printer refused the token: stop asking until a restart

    def open(self):
        return httpx.Client(base_url=f"http://{host_port(self.host, self.port)}", timeout=self.TIMEOUT)

    def close(self, http):
        try:
            if self._connected and self.secret:
                http.post("/api/v1/disconnect", data={"token": self.secret})
        finally:
            http.close()

    def reset(self):
        # Connect again next time, except while the touchscreen prompt is up: a new connect would
        # replace it, and tapping Yes on it lets the stored token back in.
        self._connected = self._waiting

    def hint(self, e):
        if isinstance(e, NotApproved):
            return "Snapmaker is asking to approve this PC; tap Yes on its touchscreen"
        if self._rejected or getattr(getattr(e, "response", None), "status_code", None) == 401:
            return TOKEN_HINT
        return None

    def poll(self, http) -> dict:
        if not self.secret:
            # Connecting without a token would ask for a new one on the touchscreen.
            raise RuntimeError("no Snapmaker token stored; run deskhub-printers to pair the printer")
        if self._rejected:
            # Connecting again would keep putting prompts on the touchscreen.
            raise RuntimeError(TOKEN_HINT)
        fresh = not self._connected
        if fresh:
            r = http.post("/api/v1/connect", data={"token": self.secret})
            if r.status_code == 401:
                self._rejected = True
            r.raise_for_status()
            self._connected = True
        r = http.get("/api/v1/status", params={"token": self.secret})
        if r.status_code == 401:
            # An idle session the printer dropped gets one new connect; refused right after a
            # connect, the token itself is no good (or No was tapped).
            self._waiting = False
            self._rejected = fresh
        r.raise_for_status()
        status = r.json() if r.status_code != 204 and r.content.strip() else {}
        if not status:
            self._waiting = True  # the printer forgot the token (it does after a power cycle)
            raise NotApproved()
        self._waiting = False
        return build_message(self.label, self.model, status)


def _sacp_open(host: str, timeout: float) -> bool:
    try:
        socket.create_connection((host, SACP_PORT), timeout=timeout).close()
        return True
    except OSError:
        return False


def probe(entry: dict, secret: str | None = None, timeout: float = 5, wait: float = APPROVAL_WAIT_S,
          interval: float = 1) -> ProbeResult:
    """Ask the printer for a token and wait up to `wait` seconds for Yes on its touchscreen."""
    host, port = str(entry["host"]), int(entry.get("port") or DEFAULT_PORT)
    http = httpx.Client(base_url=f"http://{host_port(host, port)}", timeout=timeout)
    token = ""
    try:
        try:
            r = http.post("/api/v1/connect")
            r.raise_for_status()
            reply = r.json()
            token = str(reply.get("token") or "")
        except httpx.HTTPStatusError as e:
            return ProbeResult(False, f"The printer answered with HTTP {e.response.status_code}. "
                                      "Is this a Snapmaker 2.0 on port 8080?")
        except httpx.HTTPError as e:
            if _sacp_open(host, min(timeout, 2)):
                return ProbeResult(False, "This looks like a Snapmaker J1, J1s or Artisan. They use "
                                          "SACP: add it as \"Snapmaker J1 / J1s / Artisan\" instead.")
            return ProbeResult(False, f"No answer from {host_port(host, port)} ({_safe_error(e)}). "
                                      "Check the IP address on the touchscreen (Settings > Wi-Fi) "
                                      "and that the port is 8080.")
        except (ValueError, AttributeError):
            reply = {}
        if not token:
            return ProbeResult(False, "That answered, but not like a Snapmaker 2.0 (no token in the reply).")
        model = _model(reply.get("series"))

        print("Tap Yes on the printer's touchscreen to let this PC connect...")
        end = time.monotonic() + wait
        while True:
            try:
                r = http.get("/api/v1/status", params={"token": token})
                if r.status_code == 401:
                    token = ""
                    return ProbeResult(False, "The printer turned the connection down. Run this "
                                              "again and tap Yes on the touchscreen.", model=model)
                if r.status_code == 200 and r.content.strip() and r.json():
                    found, token = token, ""
                    return ProbeResult(True, f"Connected to the {model}.", model=model, secret=found)
            except (httpx.HTTPError, ValueError):
                pass  # the screen firmware is slow to answer while the prompt is up
            if time.monotonic() >= end:
                break
            time.sleep(interval)
        return ProbeResult(False, f"Nobody tapped Yes within {round(wait)} seconds. Run this again "
                                  "and watch the touchscreen.", model=model)
    finally:
        if token:  # never approved: clear the prompt so it doesn't stay on the screen
            try:
                http.post("/api/v1/disconnect", data={"token": token})
            except httpx.HTTPError:
                pass
        http.close()


SPEC = BackendSpec(
    type="snapmaker",
    menu="Snapmaker 2.0: A150, A250, A350",
    intro="""Snapmaker 2.0 (A150, A250, A350)
  Reads the printer through its touchscreen's Wi-Fi API (port 8080), as Snapmaker Luban does.
  Turn on Wi-Fi on the touchscreen; the IP address is under Settings > Wi-Fi. While the
  connection is tested, the touchscreen asks whether to allow it: tap Yes within a minute.
  The Snapmaker U1 runs Klipper: add it as a Klipper (Moonraker) printer instead. The J1, J1s
  and Artisan have their own entry: Snapmaker J1 / J1s / Artisan.""",
    default_port=DEFAULT_PORT,
    default_model="Snapmaker",
    probe=probe,
    secret_prompt=None,
    brand="Snapmaker",
)
WORKER = SnapmakerWorker
