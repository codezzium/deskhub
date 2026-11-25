import asyncio
import logging
import threading

import serial
from serial.tools import list_ports

log = logging.getLogger(__name__)

BAUD = 460800
HUB_USB_IDS = {(0x1A86, 0x55D3)}


def find_hub_port(setting: str) -> str | None:
    if not setting:
        return None
    ports = list(list_ports.comports())
    if setting.lower() != "auto":
        return setting if any(p.device.upper() == setting.upper() for p in ports) else None
    for p in ports:
        if (p.vid, p.pid) in HUB_USB_IDS:
            return p.device
    return None


class SerialLink:
    def __init__(self, port: str, loop: asyncio.AbstractEventLoop):
        self.port = port
        self.loop = loop
        self.queue: asyncio.Queue[str | None] = asyncio.Queue()
        self.lock = threading.Lock()
        self.closed = False
        self.ser = serial.Serial()
        self.ser.port = port
        self.ser.baudrate = BAUD
        self.ser.dtr = False
        self.ser.rts = False
        self.ser.timeout = 0.5
        self.ser.write_timeout = 2
        self.ser.open()
        self.thread = threading.Thread(target=self._reader, name="serial-link", daemon=True)
        self.thread.start()

    def _push(self, item: str | None):
        try:
            self.loop.call_soon_threadsafe(self.queue.put_nowait, item)
        except RuntimeError:
            pass

    def _reader(self):
        buf = b""
        try:
            while not self.closed:
                data = self.ser.read(4096)
                if not data:
                    continue
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    text = line.decode("utf-8", "replace").strip()
                    if text.startswith("{"):
                        self._push(text)
                if len(buf) > 1 << 20:
                    buf = b""
        except (serial.SerialException, OSError) as e:
            if not self.closed:
                log.info("serial link %s lost: %s", self.port, e)
        finally:
            self._push(None)

    def _write(self, data: bytes):
        with self.lock:
            self.ser.write(data)

    async def send(self, msg: str | bytes):
        if isinstance(msg, bytes) or self.closed:
            return
        try:
            await asyncio.to_thread(self._write, msg.encode("utf-8") + b"\n")
        except (serial.SerialException, OSError) as e:
            log.info("serial link %s write failed: %s", self.port, e)
            self.close()

    async def recv(self) -> str:
        item = await self.queue.get()
        if item is None:
            raise ConnectionError(f"serial link {self.port} closed")
        return item

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.ser.close()
        except (serial.SerialException, OSError):
            pass
        self._push(None)
