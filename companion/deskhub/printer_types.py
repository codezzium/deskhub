"""Printer backends that are polled over the network, by config.toml `type`.

Bambu Lab ("local", "cloud") lives in printer.py and isn't listed here. Modules are imported
on first use so a broken or slow import for one brand can't stop the others.
"""
import importlib

BACKENDS = {
    "moonraker": "moonraker",
    "prusalink": "prusalink",
    "octoprint": "octoprint",
    "flashforge": "flashforge",
    "elegoo": "elegoo",
    "snapmaker": "snapmaker",
    "snapmaker-sacp": "snapmaker_sacp",
    "anycubic": "anycubic",
    "creality": "creality",
}


def module(kind: str):
    return importlib.import_module(f".{BACKENDS[kind]}", __package__)


def available() -> list[str]:
    """Backend types whose module exists (in the order above)."""
    out = []
    for kind in BACKENDS:
        try:
            module(kind)
        except ModuleNotFoundError:
            continue
        out.append(kind)
    return out
