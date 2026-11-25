"""CPU, RAM, GPU (NVIDIA via NVML), network and disk usage."""
import logging
import os
import time

import psutil

from .hw_sensors import HwSensors
from .text import ascii_fold

log = logging.getLogger(__name__)


def _cpu_name() -> str:
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
            name = winreg.QueryValueEx(k, "ProcessorNameString")[0]
    except OSError:
        import platform

        name = platform.processor()
    for junk in ("(R)", "(TM)", "CPU", "Processor"):
        name = name.replace(junk, "")
    name = name.split("@")[0]
    return ascii_fold(name, 40)


class Stats:
    def __init__(self):
        psutil.cpu_percent(None)
        self._net = psutil.net_io_counters()
        self._io = psutil.disk_io_counters()
        self._t = time.monotonic()
        self._cpu_name = _cpu_name()
        self._drive = os.environ.get("SystemDrive", "C:") + "\\"
        self._sensors = HwSensors()
        self._gpus = []
        try:
            import pynvml

            pynvml.nvmlInit()
            self._nvml = pynvml
            for i in range(min(pynvml.nvmlDeviceGetCount(), 2)):
                handle = pynvml.nvmlDeviceGetHandleByIndex(i)
                name = pynvml.nvmlDeviceGetName(handle)
                if isinstance(name, bytes):
                    name = name.decode()
                self._gpus.append((handle, ascii_fold(name.replace("NVIDIA ", ""), 40)))
        except Exception as e:  # no NVIDIA GPU or driver
            log.info("GPU stats unavailable: %s", e)

    def sample(self) -> dict:
        now = time.monotonic()
        net = psutil.net_io_counters()
        dt = max(now - self._t, 0.001)
        down = (net.bytes_recv - self._net.bytes_recv) / dt / 1024
        up = (net.bytes_sent - self._net.bytes_sent) / dt / 1024
        io = psutil.disk_io_counters()
        disk_read = disk_write = 0.0
        if io is not None and self._io is not None:
            disk_read = max(io.read_bytes - self._io.read_bytes, 0) / dt / 1024
            disk_write = max(io.write_bytes - self._io.write_bytes, 0) / dt / 1024
        self._net, self._io, self._t = net, io, now

        vm = psutil.virtual_memory()
        msg = {
            "type": "stats",
            "now": int(time.time()),
            "cpu": round(psutil.cpu_percent(None), 1),
            "ram_used": round(vm.used / 2**30, 2),
            "ram_total": round(vm.total / 2**30, 1),
            "net_down": round(down, 1),
            "net_up": round(up, 1),
            "disk": round(psutil.disk_usage(self._drive).percent, 1),
            "disk_read": round(disk_read, 1),
            "disk_write": round(disk_write, 1),
            "cpu_name": self._cpu_name,
            "gpu_name": self._gpus[0][1] if self._gpus else "",
        }
        if self._sensors.cpu_temp is not None:
            msg["cpu_temp"] = round(self._sensors.cpu_temp, 1)
        if self._sensors.disk_temp is not None:
            msg["disk_temp"] = round(self._sensors.disk_temp, 1)
        for i, (handle, name) in enumerate(self._gpus):
            n = self._nvml
            key = "" if i == 0 else str(i + 1)
            if i:
                msg[f"gpu{key}_name"] = name
            try:
                mem = n.nvmlDeviceGetMemoryInfo(handle)
                msg[f"gpu{key}"] = n.nvmlDeviceGetUtilizationRates(handle).gpu
                msg[f"gpu{key}_temp"] = n.nvmlDeviceGetTemperature(handle, n.NVML_TEMPERATURE_GPU)
                msg[f"vram{key}_used"] = round(mem.used / 2**30, 2)
                msg[f"vram{key}_total"] = round(mem.total / 2**30, 1)
            except Exception as e:
                log.debug("nvml read failed: %s", e)
        return msg
