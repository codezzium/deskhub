import logging
import re
import threading
import time

import httpx

log = logging.getLogger(__name__)

URL = "http://127.0.0.1:8085/data.json"
POLL_S = 2
RETRY_S = 30
NUMBER = re.compile(r"-?\d+(?:[.,]\d+)?")
CPU_PREFIXES = ("/amdcpu/", "/intelcpu/")
CPU_PREFERRED = ("Core (Tctl/Tdie)", "CPU Package", "Core (Tctl)", "Core (Tdie)")
DISK_PREFIXES = ("/nvme/", "/hdd/", "/ssd/", "/ata/", "/storage/")
DISK_SKIP = ("warning", "critical", "limit", "resolution")


def parse_temp(text: object) -> float | None:
    m = NUMBER.search(str(text))
    return float(m.group().replace(",", ".")) if m else None


def temperature_sensors(node: dict) -> list[dict]:
    found = []
    stack = [node]
    while stack:
        n = stack.pop()
        if n.get("Type") == "Temperature" and n.get("SensorId"):
            found.append(n)
        stack.extend(reversed(n.get("Children") or []))
    return found


def pick_temps(tree: dict) -> tuple[float | None, float | None]:
    sensors = temperature_sensors(tree)
    cpu = [s for s in sensors if s["SensorId"].startswith(CPU_PREFIXES)]
    cpu.sort(key=lambda s: CPU_PREFERRED.index(s.get("Text")) if s.get("Text") in CPU_PREFERRED
             else len(CPU_PREFERRED))
    disk = [s for s in sensors if s["SensorId"].startswith(DISK_PREFIXES)
            and not any(w in str(s.get("Text", "")).lower() for w in DISK_SKIP)]
    cpu_temp = parse_temp(cpu[0].get("Value")) if cpu else None
    disk_temp = parse_temp(disk[0].get("Value")) if disk else None
    return cpu_temp, disk_temp


class HwSensors:
    def __init__(self, url: str = URL):
        self.url = url
        self.cpu_temp: float | None = None
        self.disk_temp: float | None = None
        self._online = None
        threading.Thread(target=self._run, name="hw-sensors", daemon=True).start()

    def _run(self):
        with httpx.Client(timeout=1.5) as client:
            while True:
                try:
                    tree = client.get(self.url).raise_for_status().json()
                    self.cpu_temp, self.disk_temp = pick_temps(tree)
                    if self._online is not True:
                        log.info("LibreHardwareMonitor found: cpu %s C, disk %s C", self.cpu_temp, self.disk_temp)
                    self._online = True
                    time.sleep(POLL_S)
                except (httpx.HTTPError, ValueError) as e:
                    self.cpu_temp = self.disk_temp = None
                    if self._online is not False:
                        log.info("LibreHardwareMonitor not reachable at %s (%s); CPU and disk temperatures off",
                                 self.url, e)
                    self._online = False
                    time.sleep(RETRY_S)
