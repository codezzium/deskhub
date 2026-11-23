"""Snapmaker J1 / J1s and Artisan through SACP, Snapmaker's binary protocol, on TCP port 8888 of
the touchscreen (the way Snapmaker Luban connects over Wi-Fi).

Packets: 0xAA 0x55, UInt16 length (bytes 7 to the end), version 1, receiver id, CRC-8 of bytes
0-5, sender id, attribute (0 request, 1 ACK), UInt16 sequence, command set, command id, payload,
then a UInt16 TCP/IP-style checksum of bytes 7 to the end of the payload. Numbers are little
endian; strings are a UInt16 length then the bytes; "floats" are Int32 thousandths. Peers: 0 the
PC, 1 the controller, 2 the touchscreen.

Connecting: the PC sends 0x01 0x05 to the touchscreen with three strings (host name, client
name, token). The token is the client's own: the screen keeps the tokens it has let in, and with
*Remote Connection* set to "Only the first time" a known token is let straight in. Otherwise the
screen shows "Connection Verification" with Refuse / Connect, and the ACK only comes once someone
taps. Result 0 is in, 200 refused, 201 "Allow Remote Connection" is off. Until then the screen
passes nothing to the controller, so no status can be read without approval. While the prompt is
up the screen writes stray "a" bytes to the socket to check it's still there. Only one client at
a time: while Luban (or anything else) is connected, a new socket is accepted and ignored. A
client that closes while its prompt is up doesn't take the prompt down, and the screen keeps
ignoring new sockets until someone taps, so a pending connect is never closed on a timer here.

After that, packets addressed to the controller pass through the screen. Status comes from
subscriptions (0x01 0x00 with set, id and a UInt16 interval in ms; 0x01 0x01 cancels): the
controller then pushes that command every interval. 0x01 0x06 to the screen ends the session; the
screen sends 0x01 0x06 itself when someone taps Disconnect there.

Sources the protocol details were checked against:
  - Snapmaker's SACP docs and SDK (github.com/Snapmaker/Snapmaker-SACP, docs/*.md, and the
    @snapmaker/snapmaker-sacp-sdk 0.1.1 package): framing, CRC-8, checksum, subscriptions and the
    payload layouts (ExtruderInfo, GetHotBed, MachineInfo, ModuleInfo, GetWorkSpeed)
  - Snapmaker Luban (github.com/Snapmaker/Luban, src/server/services/machine): SacpTcpChannel.ts
    (connect), sacp/SacpClient.ts (command ids), channels/SacpChannel.ts (which subscriptions it
    uses for the J1 and Artisan, the status codes in app/constants WORKFLOW_STATUS_MAP)
  - the J1 touchscreen app (github.com/Snapmaker/Snapmaker-J1-Screen-App,
    platform/base/.../service/remote/S30RemoteService.java): the approval prompt, tokens, result
    codes; the Artisan's screen app does the same (Snapmaker-Artisan-Screen-App)
  - the J1 controller firmware (github.com/Snapmaker/SnapmakerController-IDEX, snapmaker/event and
    snapmaker/module): what each subscription sends, the status and exception codes
  - sm2uploader (github.com/macdylan/sm2uploader, sacp.go): a third-party client making the same
    0x01 0x05 connect

Not confirmed anywhere (no printer to test against):
  - the J1s reporting machine type 4 like the J1 (Luban treats them as one machine);
  - the Artisan's payloads: its controller (github.com/Snapmaker/Artisan-Controller) registers the
    same heartbeat, extruder and bed subscriptions, and Luban reads both machines with the same
    code, but the layouts were only checked in the J1's firmware;
  - 0xac 0x1a (file name, line count and estimated time, which Luban asks the screen for): neither
    public screen app answers it, so the file name falls back to the controller's 0xac 0x00, and
    without a line count there is no progress;
  - which extruder is in use on the Artisan (the J1 says so in 0x10 0xa4; elsewhere it's the
    heated one);
  - filament type and colour: the J1's controller doesn't report them over SACP, so no slots show;
  - whether Disconnect on the touchscreen sends 0x01 0x06 or just closes the socket: both are taken
    as the user ending the session, and the worker stays away for 15 minutes.
"""
import dataclasses
import socket
import struct
import time
import uuid

from .calendar_feed import _safe_error
from .printer_poll import BackendSpec, PollingWorker, ProbeResult, host_port, make_message

DEFAULT_PORT = 8888
DEFAULT_MODEL = "J1"
CLIENT_NAME = "DeskHub"    # shown on the touchscreen's prompt
APPROVAL_WAIT_S = 60       # setup waits this long for Connect on the touchscreen
# While its prompt is up the touchscreen writes a byte every second, so a pending connect stays
# open as long as those come: closing it would leave the prompt up and the screen ignoring every
# new socket until someone taps. Silence this long means the socket is being ignored (another
# client is connected, or an old prompt is still up).
SILENT_S = 6.0
IGNORED_BACKOFF_S = (60, 300)   # then wait this long before trying again, doubling up to the cap
SCREEN_ENDED_S = 15 * 60        # after Disconnect on the touchscreen, stay away this long
REQUEST_S = 3.0            # wait for an ACK this long
STALE_STATUS_S = 10.0      # Luban drops the connection after 10 s without a heartbeat
EXCEPTIONS_S = 5.0         # ask for the exception list this often
EXCEPTION_MISSES = 3       # forget the exceptions after this many unanswered requests

PC, CONTROLLER, SCREEN = 0, 1, 2
REQUEST, ACK = 0, 1
HEADER = 13                # bytes before the payload

# Subscriptions: (command set, id, interval ms). 0x10 0xa4 (which extruder is in use) is the J1's
# own; a printer that doesn't know one answers with a non-zero result, which is fine.
SUBSCRIPTIONS = [(0x01, 0xa0, 1000),   # heartbeat: the machine status
                 (0x10, 0xa0, 1000),   # extruder temperatures
                 (0x14, 0xa0, 1000),   # bed temperature
                 (0xac, 0xa0, 1000),   # G-code line being printed
                 (0xac, 0xa5, 1000),   # seconds the job has been running
                 (0xac, 0xa4, 2000),   # print speed percentage
                 (0x10, 0xa4, 2000)]   # FDM head info, with which extruder is active (J1)

# Machine status from the heartbeat (the J1 firmware's system_status_e; Luban's
# WORKFLOW_STATUS_MAP for 0-10). 31-34 are calibration and PID tuning, outside a job.
_STATES = {0: "IDLE", 1: "PREPARE", 2: "RUNNING", 3: "PAUSE", 4: "PAUSE", 5: "IDLE", 6: "IDLE",
           7: "RUNNING", 8: "FINISH", 9: "PREPARE", 10: "RUNNING", 11: "PREPARE",
           31: "PREPARE", 32: "PREPARE", 33: "PREPARE", 34: "PREPARE"}
_JOB_CODES = {1, 2, 3, 4, 7, 8, 9, 10, 11}   # the job's file and progress belong on the board
_STOPPED = {5, 6}

MACHINES = {3: "Artisan", 4: "J1"}           # MachineInfo type (0-2 are the 2.0, 5 the Ray)
LASER_IDS = {2, 14, 19, 20, 23}
CNC_IDS = {1, 15}

# J1 exception codes (exception_type_e in the J1 firmware), shortened for the board.
J1_EXCEPTIONS = {1: "X1 motor driver fault", 2: "Heated bed not found", 3: "Heated bed self-check failed",
                 4: "Left nozzle missing", 5: "Right nozzle missing", 6: "Both nozzles missing",
                 7: "Left nozzle temperature error", 8: "Right nozzle temperature error",
                 9: "Nozzle temperature error", 10: "Bed temperature error", 11: "Motion error",
                 12: "Left nozzle heating timed out", 13: "Right nozzle heating timed out",
                 14: "Bed heating timed out", 15: "Nozzles heating timed out",
                 16: "X2 motor driver fault", 17: "Y motor driver fault", 18: "Z motor driver fault",
                 19: "E0 motor driver fault", 20: "E1 motor driver fault", 21: "Motor driver fault",
                 22: "Left nozzle overheating", 23: "Right nozzle overheating"}
J1_OWNER = 2049


# ---- packets -----------------------------------------------------------------------------
def crc8(data: bytes) -> int:
    """CRC-8, polynomial 0x07, initial 0, no reflection: the header check over bytes 0-5."""
    crc = 0
    for byte in data:
        for bit in range(7, -1, -1):
            top = crc & 0x80
            crc = (crc << 1) & 0xff
            if bool(top) != bool(byte >> bit & 1):
                crc ^= 0x07
    return crc


def checksum(data: bytes) -> int:
    """The trailing check: a TCP/IP checksum, summing big-endian byte pairs (written little
    endian in the packet, as the SDK and firmware both do)."""
    total = 0
    for i in range(0, len(data) - 1, 2):
        total += data[i] << 8 | data[i + 1]
    if len(data) % 2:
        total += data[-1]
    while total >> 16:
        total = (total & 0xffff) + (total >> 16)
    return ~total & 0xffff


@dataclasses.dataclass
class Packet:
    receiver: int
    sender: int
    attribute: int
    sequence: int
    cmd_set: int
    cmd_id: int
    payload: bytes = b""


def encode(p: Packet) -> bytes:
    head = struct.pack("<BBHBB", 0xAA, 0x55, len(p.payload) + 8, 1, p.receiver)
    body = struct.pack("<BBHBB", p.sender, p.attribute, p.sequence & 0xffff, p.cmd_set, p.cmd_id) + p.payload
    return head + bytes([crc8(head)]) + body + struct.pack("<H", checksum(body))


class Framer:
    """Cuts a byte stream into packets. Skips anything before a valid header (the touchscreen's
    stray bytes, garbage), drops frames whose checksum is wrong and keeps a partial frame for
    the next feed."""

    # The J1's controller builds packets of at most 1024 bytes. A longer length is taken for a
    # false header, so one bad length can't stall the stream for long.
    MAX_LEN = 1024

    def __init__(self):
        self.buf = bytearray()

    def feed(self, data: bytes) -> list[Packet]:
        self.buf += data
        out = []
        while True:
            start = self.buf.find(b"\xaa\x55")
            if start < 0:
                del self.buf[:max(len(self.buf) - 1, 0)]  # a trailing 0xAA may start the next one
                return out
            del self.buf[:start]
            if len(self.buf) < 7:
                return out
            if crc8(self.buf[:6]) != self.buf[6] or self.buf[4] != 1:
                del self.buf[:1]   # not a header after all
                continue
            length = self.buf[2] | self.buf[3] << 8
            if not 8 <= length <= self.MAX_LEN:
                del self.buf[:1]
                continue
            if len(self.buf) < 7 + length:
                return out
            frame = bytes(self.buf[:7 + length])
            body = frame[7:-2]
            if checksum(body) != (frame[-2] | frame[-1] << 8):
                del self.buf[:1]   # corrupted: look for the next header inside it
                continue
            del self.buf[:7 + length]
            out.append(Packet(frame[5], frame[7], frame[8], frame[9] | frame[10] << 8, frame[11],
                              frame[12], frame[HEADER:-2]))


def decode(data: bytes) -> list[Packet]:
    """Every whole, valid packet in `data`."""
    return Framer().feed(data)


def sacp_string(text: str) -> bytes:
    raw = text.encode("utf-8")
    return struct.pack("<H", len(raw)) + raw


def read_string(data: bytes, offset: int) -> tuple[str, int]:
    """(text, offset after it). Raises struct.error or ValueError on a short buffer."""
    (n,) = struct.unpack_from("<H", data, offset)
    end = offset + 2 + n
    if end > len(data):
        raise ValueError("string runs past the payload")
    return bytes(data[offset + 2:end]).decode("utf-8", "replace"), end


def connect_payload(host_name: str, token: str) -> bytes:
    """0x01 0x05: the PC's host name, the client name the touchscreen shows, the token."""
    return sacp_string(host_name) + sacp_string(CLIENT_NAME) + sacp_string(token)


def subscribe_payload(cmd_set: int, cmd_id: int, interval_ms: int) -> bytes:
    return struct.pack("<BBH", cmd_set, cmd_id, interval_ms)


# ---- payloads ---------------------------------------------------------------------------
def _milli(data: bytes, offset: int) -> float:
    return struct.unpack_from("<i", data, offset)[0] / 1000


def machine_type(payload: bytes) -> int | None:
    """MachineInfo (0x01 0x21 ACK): result, then the machine type first."""
    return payload[1] if len(payload) >= 2 and payload[0] == 0 else None


def head_kind(payload: bytes) -> str:
    """'laser', 'cnc' or 'fdm' from the module list (0x01 0x20 ACK). A machine with a printing
    head, or a list that can't be read, counts as 'fdm'."""
    ids = []
    try:
        if payload[0] != 0:
            return "fdm"
        offset = 2
        for _ in range(payload[1]):
            ids.append(struct.unpack_from("<H", payload, offset + 1)[0])
            offset = read_string(payload, offset + 10)[1]
    except (IndexError, struct.error, ValueError):
        pass
    if any(i in LASER_IDS for i in ids) and not any(i in (0, 13) for i in ids):
        return "laser"
    if any(i in CNC_IDS for i in ids) and not any(i in (0, 13) for i in ids):
        return "cnc"
    return "fdm"


def update(st: dict, p: Packet):
    """Fold one pushed status packet into the running state. Odd or short payloads are ignored."""
    d = p.payload
    if not d or d[0] != 0:
        return  # result byte: 0 is success
    key = (p.cmd_set, p.cmd_id)
    try:
        if key == (0x01, 0xa0):
            st["status"] = d[1]
        elif key == (0x10, 0xa0):
            # ExtruderInfo: key, count, then 17 bytes each. The J1 sends one packet per extruder
            # with count 1 and index 0, so the extruder is the module key's low bits (Luban reads
            # it from the key too); a dual head sending both carries the real index.
            count = d[2]
            temps = st.setdefault("nozzles", {})
            for i in range(count):
                e = d[3 + 17 * i:3 + 17 * (i + 1)]
                if len(e) < 17:
                    break
                index = d[1] & 0x07 if count == 1 else e[0]
                temps[index] = (_milli(e, 9), _milli(e, 13))
        elif key == (0x10, 0xa4):
            # FDM info (J1): key, head status, head active, extruder count, ExtruderInfo entry...
            if len(d) >= 5:
                st.setdefault("active", {})[d[1] & 0x07] = bool(d[3])
        elif key == (0x14, 0xa0):
            # GetHotBed: key, zone count, then index, Int32 thousandths, Int16 target per zone.
            if d[2] and len(d) >= 10:
                st["bed"] = (_milli(d, 4), struct.unpack_from("<h", d, 8)[0])
        elif key == (0xac, 0xa0):
            st["line"] = struct.unpack_from("<I", d, 1)[0]
        elif key == (0xac, 0xa5):
            st["elapsed_s"] = struct.unpack_from("<I", d, 1)[0]
        elif key == (0xac, 0xa4):
            st["speed"] = struct.unpack_from("<h", d, 2)[0]  # after an extruder-count byte
    except (IndexError, struct.error):
        pass


def file_info(p: Packet | None) -> dict:
    """{name, lines, estimate_s} from the screen's 0xac 0x1a ACK or {name} from the controller's
    0xac 0x00 ACK (MD5 then name), or {} when there's nothing usable."""
    if p is None or not p.payload or p.payload[0] != 0:
        return {}
    d = p.payload
    try:
        if p.cmd_id == 0x1a:
            name, off = read_string(d, 1)
            lines, estimate = struct.unpack_from("<II", d, off)
            return {"name": name, "lines": lines, "estimate_s": estimate} if name or lines else {}
        _, off = read_string(d, 1)
        name = read_string(d, off)[0].rstrip("\x00")
        return {"name": name} if name else {}
    except (IndexError, struct.error, ValueError):
        return {}


def exceptions(payload: bytes) -> list[tuple[int, int, int]]:
    """(level, owner, code) for each active exception in a 0x04 0x02 ACK."""
    out = []
    try:
        if payload[0] != 0:
            return []
        for i in range(payload[1]):
            out.append(struct.unpack_from("<BHB", payload, 2 + 4 * i))
    except (IndexError, struct.error):
        pass
    return out


def _file_name(path) -> str:
    name = str(path or "").replace("\\", "/").rsplit("/", 1)[-1]
    for ext in (".gcode", ".gco", ".g", ".nc", ".cnc"):
        if name.lower().endswith(ext):
            return name[:-len(ext)]
    return name


def _error_text(owner: int, code: int) -> str:
    if owner == J1_OWNER and code in J1_EXCEPTIONS:
        return J1_EXCEPTIONS[code]
    return f"Error {code}"


def build_message(name: str, model: str, st: dict) -> dict:
    """The board's `printer` entry from the merged state."""
    code = st.get("status")
    state = _STATES.get(code, "IDLE") if isinstance(code, int) else "IDLE"
    errors = st.get("exceptions") or []
    if code in _STOPPED and errors:
        state = "FAILED"  # stopped with an exception still up
    job = code in _JOB_CODES
    active = job and state in ("RUNNING", "PAUSE", "PREPARE")
    line, lines = st.get("line") or 0, st.get("lines") or 0
    if state == "RUNNING" and code == 2 and not line:
        state = "PREPARE"  # heating before the first line

    pct = 0.0
    if state == "FINISH":
        pct = 100
    elif active and lines:
        pct = min(line / lines, 1) * 100
    remaining_s = 0.0
    elapsed = st.get("elapsed_s") or 0
    if active:
        if pct > 1 and elapsed:
            remaining_s = elapsed * (100 / pct - 1)
        elif st.get("estimate_s"):
            remaining_s = max(st["estimate_s"] - elapsed, 0)
    speed = st.get("speed")

    error = ""
    if errors and state in ("PAUSE", "FAILED"):
        _, owner, value = max(errors)  # the highest level first
        error = _error_text(owner, value)
    elif state == "FAILED":
        error = "Print stopped by an error"

    temps = {}
    if st.get("head", "fdm") == "fdm":
        nozzles = st.get("nozzles") or {}
        flags = st.get("active") or {}
        pick = next((i for i in sorted(nozzles) if flags.get(i)), None)
        if pick is None:
            pick = next((i for i in sorted(nozzles) if (nozzles[i][1] or 0) > 0), min(nozzles, default=None))
        cur, tgt = nozzles.get(pick, (None, None))
        bed = st.get("bed") or (None, None)
        temps = {"nozzle": cur, "nozzle_target": tgt, "bed": bed[0], "bed_target": bed[1]}
    # Laser and CNC jobs: the state and progress, no temperatures.

    return make_message(name, model, state=state, pct=pct, remaining_min=remaining_s / 60,
                        file=_file_name(st.get("file")) if job else "",
                        speed=f"{speed}%" if active and isinstance(speed, int) and speed > 0 else "",
                        error=error, **temps)


# ---- the connection ---------------------------------------------------------------------
class NotApproved(Exception):
    """The touchscreen is asking whether to let this PC in."""


class Refused(Exception):
    """Someone tapped Refuse on the touchscreen."""


class RemoteOff(Exception):
    """The printer's "Allow Remote Connection" setting is off."""


class NotAnswering(Exception):
    """The touchscreen took the socket but says nothing: another client is connected, or an old
    prompt is still up."""


class ScreenEnded(ConnectionError):
    """The touchscreen ended a session it had let in (Disconnect tapped there)."""


class Link:
    """One TCP connection to the touchscreen."""

    def __init__(self, host: str, port: int, timeout: float = 5):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.framer = Framer()
        self.inbox: list[Packet] = []
        self.seq = 0
        self.opened = time.monotonic()
        self.heard = self.opened        # last whole packet
        self.raw_at = self.opened       # last byte of any kind (the prompt's stray bytes count)
        self.authed = False             # the connect was let in
        self.ended = False              # the touchscreen sent 0x01 0x06

    def close(self):
        self.sock.close()

    def send(self, receiver: int, cmd_set: int, cmd_id: int, payload: bytes = b"") -> int:
        self.seq = self.seq % 0xfffe + 1
        self.sock.settimeout(REQUEST_S)  # not the short read timeout left from _read
        self.sock.sendall(encode(Packet(receiver, PC, REQUEST, self.seq, cmd_set, cmd_id, payload)))
        return self.seq

    def _read(self, timeout: float):
        self.sock.settimeout(max(timeout, 0.001))
        try:
            data = self.sock.recv(65536)
        except TimeoutError:
            return
        if not data:
            if self.authed:
                raise ScreenEnded("the touchscreen closed the session")
            raise ConnectionError("the printer closed the connection")
        self.raw_at = time.monotonic()
        for p in self.framer.feed(data):
            self.heard = time.monotonic()
            if p.attribute == REQUEST and (p.cmd_set, p.cmd_id) == (0x01, 0x06):
                # Disconnect tapped on the touchscreen: answer, as Luban does, then stop.
                self.sock.settimeout(REQUEST_S)
                self.sock.sendall(encode(Packet(p.sender, PC, ACK, p.sequence, 0x01, 0x06, b"\x00")))
                self.ended = True
            self.inbox.append(p)

    def next(self, timeout: float) -> Packet | None:
        """The next packet, or None when `timeout` runs out."""
        end = time.monotonic() + timeout
        while not self.inbox:
            if self.ended:
                raise ScreenEnded("disconnected on the touchscreen")
            left = end - time.monotonic()
            if left <= 0:
                return None
            self._read(left)
        return self.inbox.pop(0)

    def wait_ack(self, seq: int, cmd_set: int, cmd_id: int, timeout: float, keep: list | None = None):
        """The ACK to request `seq`, or None. Other packets go to `keep` (if given)."""
        end = time.monotonic() + timeout
        while (left := end - time.monotonic()) > 0:
            p = self.next(left)
            if p is None:
                break
            if p.attribute == ACK and p.sequence == seq and (p.cmd_set, p.cmd_id) == (cmd_set, cmd_id):
                return p
            if keep is not None:
                keep.append(p)
        return None

    def request(self, receiver, cmd_set, cmd_id, payload=b"", timeout=REQUEST_S, keep=None):
        return self.wait_ack(self.send(receiver, cmd_set, cmd_id, payload), cmd_set, cmd_id, timeout, keep)


def _check_connect(ack: Packet):
    result = ack.payload[0] if ack.payload else 0
    if result == 200:
        raise Refused()
    if result == 201:
        raise RemoteOff()
    if result:
        raise ConnectionError(f"the printer turned the connection down (code {result})")


def _host_name() -> str:
    return socket.gethostname()[:32] or "PC"


class SnapmakerSacpWorker(PollingWorker):
    type = "snapmaker-sacp"
    default_port = DEFAULT_PORT
    default_model = DEFAULT_MODEL

    def __init__(self, entry: dict, secret_getter):
        super().__init__(entry, secret_getter)
        self._link: Link | None = None
        self._pending: int | None = None  # the connect request's sequence, while unanswered
        self._refused = False
        self._hold: tuple[float, Exception] | None = None  # (don't connect before, why)
        self._ignored = 0       # connects in a row the touchscreen ignored
        self._state: dict = {}
        self._exc_at = 0.0
        self._exc_misses = 0

    # The connection lives in the worker, not in the framework's `conn`: a connect waiting for
    # Connect on the touchscreen must survive the NotApproved "failure" the board reports
    # meanwhile, since a new socket would be ignored while the prompt is up.
    def open(self):
        return self

    def close(self, conn):
        self._drop(polite=True)

    def _drop(self, polite=False):
        link, self._link, self._pending = self._link, None, None
        if link is None:
            return
        try:
            if polite and not link.ended:
                for cmd_set, cmd_id, _ in SUBSCRIPTIONS:
                    link.send(CONTROLLER, 0x01, 0x01, bytes([cmd_set, cmd_id]))
                link.send(SCREEN, 0x01, 0x06)
        except OSError:
            pass
        finally:
            link.close()

    def hint(self, e):
        if isinstance(e, NotApproved):
            return "Snapmaker is asking to allow this PC; tap Connect on its touchscreen"
        if isinstance(e, Refused) or self._refused:
            return "Snapmaker refused the connection; restart the companion and tap Connect on the touchscreen"
        if isinstance(e, RemoteOff):
            return "turn on Allow Remote Connection on the Snapmaker's touchscreen"
        if isinstance(e, NotAnswering):
            return ("the Snapmaker's touchscreen isn't answering; close Luban's connection or dismiss an old "
                    "prompt on the screen (tap Refuse)")
        if isinstance(e, ScreenEnded):
            return f"disconnected on the touchscreen; reconnecting in {round(SCREEN_ENDED_S / 60)} min"
        return None

    def _hold_off(self, seconds: float, why: Exception):
        self._hold = (time.monotonic() + seconds, why)

    def poll(self, conn) -> dict:
        if not self.secret:
            raise RuntimeError("no Snapmaker token stored; run deskhub-printers to pair the printer")
        if self._refused:
            raise Refused()  # asking again would keep putting prompts on the touchscreen
        if self._hold:
            if time.monotonic() < self._hold[0]:
                raise self._hold[1]
            self._hold = None
        try:
            if self._link is None:
                self._link = Link(self.host, self.port)
                self._state = {}
                self._exc_misses = 0
                self._pending = self._link.send(SCREEN, 0x01, 0x05, connect_payload(_host_name(), self.secret))
            link = self._link
            if self._pending:
                ack = link.wait_ack(self._pending, 0x01, 0x05, 1.0)
                # Until the first byte, it isn't known yet whether a prompt is up or the socket is
                # ignored: wait for either, so the log line says the right one.
                while ack is None and link.raw_at == link.opened and time.monotonic() - link.opened <= SILENT_S:
                    ack = link.wait_ack(self._pending, 0x01, 0x05, 0.5)
                if ack is None:
                    if time.monotonic() - link.raw_at > SILENT_S:
                        # Ignored, not prompting: try again later rather than pile up sockets.
                        self._ignored += 1
                        low, high = IGNORED_BACKOFF_S
                        why = NotAnswering()
                        self._hold_off(min(low * 2 ** (self._ignored - 1), high), why)
                        raise why
                    raise NotApproved()  # the prompt is up: keep this socket open
                self._pending = None
                self._ignored = 0
                _check_connect(ack)
                link.authed = True
                self._setup(link)
            self._pump(link)
            return build_message(self.label, self.model, self._state)
        except NotApproved:
            raise
        except Refused:
            self._refused = True
            self._drop()
            raise
        except ScreenEnded as e:
            # Someone ended the session on the touchscreen; don't undo that straight away.
            self._hold_off(SCREEN_ENDED_S, e)
            self._drop()
            raise
        except Exception:
            self._drop()
            raise

    def _setup(self, link: Link):
        keep: list[Packet] = []
        info = link.request(CONTROLLER, 0x01, 0x21, keep=keep)
        kind = machine_type(info.payload) if info else None
        modules = link.request(CONTROLLER, 0x01, 0x20, keep=keep)
        self._state["head"] = head_kind(modules.payload) if modules else "fdm"
        self._state["machine"] = kind
        for cmd_set, cmd_id, interval in SUBSCRIPTIONS:
            ack = link.request(CONTROLLER, 0x01, 0x00, subscribe_payload(cmd_set, cmd_id, interval), keep=keep)
            if (cmd_set, cmd_id) == (0x01, 0xa0) and (ack is None or not ack.payload or ack.payload[0]):
                raise ConnectionError("the printer didn't accept the status subscription")
        for p in keep:
            update(self._state, p)
        self._exc_at = 0.0

    def _pump(self, link: Link):
        st = self._state
        end = time.monotonic() + 1.0
        while (left := end - time.monotonic()) > 0 and (p := link.next(min(left, 0.2))) is not None:
            if p.cmd_set == 0x04 and p.cmd_id in (0x00, 0x01):
                self._exc_at = 0.0  # an exception came or went: ask for the list now
            update(st, p)
        if time.monotonic() - link.heard > STALE_STATUS_S:
            raise TimeoutError("nothing from the printer")
        code, prev = st.get("status"), st.get("prev")
        elapsed, prev_elapsed = st.get("elapsed_s"), st.get("prev_elapsed")
        st["prev"], st["prev_elapsed"] = code, elapsed
        # A new job: the status leaves idle, stopped or completed for starting or printing (STARTING
        # can fall between two polls), or the job clock goes back. No status yet (None) isn't one:
        # the state starts empty with each connection, so nothing in it is from an old job.
        new_job = ((code in (1, 2, 9) and prev in (0, 5, 6, 8))
                   or (elapsed is not None and prev_elapsed is not None and elapsed < prev_elapsed))
        if code not in _JOB_CODES or new_job:
            for k in ("file", "lines", "estimate_s", "asked", "line", "elapsed_s", "prev_elapsed"):
                st.pop(k, None)  # the last job's details, file info and counters
        if code in _JOB_CODES and not st.get("asked"):
            st["asked"] = True
            keep: list[Packet] = []
            found = file_info(link.request(SCREEN, 0xac, 0x1a, b"\x00", timeout=2, keep=keep))
            if not found.get("name"):
                found = {**found, **file_info(link.request(CONTROLLER, 0xac, 0x00, timeout=2, keep=keep))}
            st["file"], st["lines"], st["estimate_s"] = found.get("name"), found.get("lines"), found.get("estimate_s")
            for p in keep:
                update(st, p)
        if time.monotonic() - self._exc_at >= EXCEPTIONS_S:
            self._exc_at = time.monotonic()
            keep = []
            ack = link.request(CONTROLLER, 0x04, 0x02, timeout=2, keep=keep)
            if ack is not None:
                st["exceptions"] = exceptions(ack.payload)
                self._exc_misses = 0
            else:
                self._exc_misses += 1
                if self._exc_misses >= EXCEPTION_MISSES:
                    st.pop("exceptions", None)  # can't tell any more: don't keep an old error up
            for p in keep:
                update(st, p)

    def reset(self):
        pass  # the state belongs to the link and is cleared when a new one opens


def probe(entry: dict, secret: str | None = None, timeout: float = 5, wait: float = APPROVAL_WAIT_S) -> ProbeResult:
    """Connect with a new token (or the stored one) and wait up to `wait` seconds for Connect on
    the touchscreen. Suggests the model as the name."""
    host, port = str(entry["host"]), int(entry.get("port") or DEFAULT_PORT)
    token = secret or str(uuid.uuid4())
    try:
        link = Link(host, port, timeout=timeout)
    except OSError as e:
        return ProbeResult(False, f"No answer from {host_port(host, port)} ({_safe_error(e, '')}). Check the IP "
                                  "address on the touchscreen and that the printer is on this network.")
    # From here on every result carries the token, so "save it anyway" leaves the worker one to
    # connect with (and the touchscreen asks again then).
    approved = False
    try:
        seq = link.send(SCREEN, 0x01, 0x05, connect_payload(_host_name(), token))
        ack = link.wait_ack(seq, 0x01, 0x05, min(timeout, 3))
        if ack is None and time.monotonic() - link.raw_at <= SILENT_S:
            print("Tap Connect on the printer's touchscreen to let this PC in...")
        end = time.monotonic() + wait
        while ack is None:
            if time.monotonic() - link.raw_at > SILENT_S:
                return ProbeResult(False, "The touchscreen isn't answering. Close Luban's connection to the "
                                          "printer, or dismiss an old prompt on the screen (tap Refuse), then "
                                          "try again.", secret=token)
            if (left := end - time.monotonic()) <= 0:
                return ProbeResult(False, f"Nobody tapped Connect within {round(wait)} seconds. Tap Refuse on "
                                          "the prompt still on the touchscreen, then run this again.", secret=token)
            ack = link.wait_ack(seq, 0x01, 0x05, min(left, 1.0))
        _check_connect(ack)
        approved = True
        info = link.request(CONTROLLER, 0x01, 0x21)
        kind = machine_type(info.payload) if info else None
    except Refused:
        return ProbeResult(False, "The printer turned the connection down. Run this again and tap Connect; if "
                                  "you save it anyway, tap Connect when the touchscreen asks after the companion "
                                  "restarts.", secret=token)
    except RemoteOff:
        return ProbeResult(False, "The printer doesn't allow remote connections. Turn on Allow Remote "
                                  "Connection in its settings and try again.", secret=token)
    except (OSError, ConnectionError) as e:
        return ProbeResult(False, f"The connection to {host_port(host, port)} failed ({_safe_error(e, token)}). "
                                  "Is this a Snapmaker J1 or Artisan?", secret=token)
    finally:
        try:
            if approved:
                link.send(SCREEN, 0x01, 0x06)
        except OSError:
            pass
        link.close()
    model = MACHINES.get(kind, "") if kind is not None else ""
    label = f"Snapmaker {model}" if model else "the Snapmaker"
    if kind is None:
        return ProbeResult(True, "Connected, but the printer didn't say which model it is.",
                           "Snapmaker", "", secret=token)
    if kind not in MACHINES:
        return ProbeResult(True, f"Connected, but machine type {kind} isn't a J1 or Artisan; it may not "
                                 "show right.", "Snapmaker", "", secret=token)
    return ProbeResult(True, f"Connected to {label}.", label, model, secret=token)


SPEC = BackendSpec(
    type="snapmaker-sacp",
    menu="Snapmaker J1 / J1s / Artisan",
    intro="""Snapmaker J1, J1s or Artisan
  Reads the printer over its Wi-Fi connection (port 8888), the way Snapmaker Luban does. Turn on
  Allow Remote Connection on the touchscreen, and disconnect Luban: the printer takes one
  connection at a time. While the connection is tested, the touchscreen asks whether to let
  this PC in: tap Connect within a minute. With Remote Connection set to "Only the first time",
  it won't ask again. Keep Safe Mode off, or the touchscreen locks while the companion is
  connected.""",
    default_port=DEFAULT_PORT,
    default_model=DEFAULT_MODEL,
    probe=probe,
    secret_prompt=None,
    ask_port=False,
    brand="Snapmaker",
)
WORKER = SnapmakerSacpWorker
