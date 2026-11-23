"""Exercises deskhub-setup against temp copies: city search, POSIX time zones,
sdkconfig edits and token sync. Uses the live Open-Meteo geocoding API.

    uv run python tests/test_setup.py
"""
import builtins
import getpass
import pathlib
import shutil
import sys
import tempfile

from deskhub import setup_cli as s
import deskhub.pages as _pg
_pg.steam_installed = lambda: False
s.step_steam_key = lambda: None  # never touch the real credential store

problems = []

def check(cond, msg):
    if not cond:
        problems.append(msg)

# Time zones -> POSIX strings the ESP32 understands.
check(s.posix_tz("America/New_York") == "EST5EDT,M3.2.0,M11.1.0", "New York tz")
check(s.posix_tz("Europe/London") == "GMT0BST,M3.5.0/1,M10.5.0", "London tz")
check(s.posix_tz("Asia/Kolkata") == "IST-5:30", "Kolkata tz")

# City search, including "City, ST" narrowing.
places = s.geocode("Charlotte, NC")
check(places and places[0]["admin1"] == "North Carolina", f"Charlotte NC -> {places[:1]}")
places = s.geocode("Paris, France")
check(places and places[0]["country_code"] == "FR", "Paris FR")

# Kconfig edits keep other lines and handle quoting and booleans.
cfg = 'CONFIG_A=1\nCONFIG_HUB_CITY="New York"\n# CONFIG_HUB_24H is not set\n'
cfg = s.set_kconfig(cfg, "HUB_CITY", 'Say "hi"')
cfg = s.set_kconfig(cfg, "HUB_24H", True)
cfg = s.set_kconfig(cfg, "HUB_NEW", "x")
check(s.read_kconfig(cfg, "HUB_CITY") == 'Say "hi"', "quote round trip")
check("CONFIG_HUB_24H=y" in cfg and "CONFIG_A=1" in cfg and 'CONFIG_HUB_NEW="x"' in cfg, "kconfig edits")

# Full flow on temp copies: keep Wi-Fi, pick Charlotte, keep units, skip calendars and flash.
root = pathlib.Path(tempfile.mkdtemp())
fw = root / "firmware"; fw.mkdir()
(fw / "sdkconfig").write_text('CONFIG_HUB_WIFI_SSID="home"\nCONFIG_HUB_WIFI_PASSWORD="secret123"\n'
                              'CONFIG_HUB_TOKEN="change-me"\nCONFIG_HUB_CITY="New York"\n'
                              'CONFIG_HUB_FAHRENHEIT=y\n# CONFIG_HUB_24H is not set\n')
conf = root / "config.toml"
shutil.copy(pathlib.Path(__file__).parent.parent / "config.example.toml", conf)
# Wi-Fi, city, pick, F, 24h, calendars, printers, media servers, games, shortcuts, 11 pages (hide Weather and Games), nav bar, top bar, home (keep? no; events 2x2, a bad name, weather default, events again, stats with a bad size), PC sleep, night (bad, good, style), internet (on, speed test hours, IP), GitHub token, live limits, alerts, hooks, idle seconds, flash, start with Windows
answers = iter(["", "Charlotte, NC", "1", "", "", "n", "n", "n", "n", "n",
                "", "", "", "n", "", "", "", "", "n", "", "", "bottom", "y", "n", "events", "2x2", "bogus", "weather", "", "events", "stats", "9x9", "3x1", "", "clock", "25:00-7", "11pm-7:30am", "clock", "", "", "", "n", "y", "", "", "", "codex", "n", "y"])
builtins.input = lambda prompt="": next(answers)
# Never touch the real Claude Code settings: point the alerts step at a temp file.
s.CLAUDE_SETTINGS = root / "claude_settings.json"
installer_calls = []
s.run_installer = lambda *a: installer_calls.append(a) or True
getpass.getpass = lambda prompt="": ""
import deskhub.github as _gh
_gh.get_token = lambda: ""   # never read the real credential store
# ...and put the login shortcut in a temp "Startup" folder.
import deskhub.install_startup as startup
startup.startup_dir = lambda: root / "Startup"
sys.argv = ["deskhub-setup", "--config", str(conf), "--firmware", str(fw)]
s.main()
out = (fw / "sdkconfig").read_text()
_lnk = root / "Startup" / startup.LINK_NAME
check(_lnk.is_file() and _lnk.stat().st_size > 0, "login shortcut created")
check(b"pythonw" in _lnk.read_bytes().lower() or "pythonw".encode("utf-16-le") in _lnk.read_bytes().lower(),
      "shortcut runs pythonw")
answers = iter(["n"])
s.step_startup(conf)
check(not _lnk.exists(), "login shortcut removed")
check(s.read_kconfig(out, "HUB_CITY") == "Charlotte", "city saved")
check(s.read_kconfig(out, "HUB_LATITUDE", ).startswith("35.2"), "lat saved")
check(s.read_kconfig(out, "HUB_TZ") == "EST5EDT,M3.2.0,M11.1.0", "tz saved")
check(s.read_kconfig(out, "HUB_WIFI_PASSWORD") == "secret123", "password kept")
token = s.read_kconfig(out, "HUB_TOKEN")
check(token != "change-me" and f'token = "{token}"' in conf.read_text(), "token synced to config.toml")
check("# Shortcut tiles" in conf.read_text(), "config comments kept")
import tomllib as _t
_home = _t.loads(conf.read_text())["home"]
check(_home == {"header": True, "grid": [
    {"widget": "events", "x": 0, "y": 0, "w": 2, "h": 2},
    {"widget": "weather", "x": 2, "y": 0, "w": 2, "h": 1},
    {"widget": "stats", "x": 2, "y": 1, "w": 3, "h": 1}]}, f"home grid saved: {_home}")
check("live_limits = true" in conf.read_text(), "live limits turned on")
check('agent = "codex"' in conf.read_text(), "AI agent saved")
check(installer_calls == [("hooks",)], f"alerts step installs the hooks: {installer_calls}")
check("alerts = true" in conf.read_text(), "alerts kept on")

# Turning alerts off with hooks installed offers to remove them; old hooks get updated.
import json as _j
s.CLAUDE_SETTINGS.write_text(_j.dumps({"hooks": {"Stop": [{"matcher": "", "hooks": [{"type": "command", "command": "py hook_bridge.py Stop"}]}]}}))
installer_calls.clear()
answers = iter(["n", ""])
s.step_alerts(conf)
check(installer_calls == [("hooks", "--remove")] and "alerts = false" in conf.read_text(), f"alerts off removes hooks: {installer_calls}")
installer_calls.clear()
answers = iter(["y", "", ""])
s.step_alerts(conf)
check(installer_calls == [("hooks",)], f"outdated hooks get updated: {installer_calls}")
import tomllib
pages = tomllib.loads(conf.read_text())["pages"]["enabled"]
check(pages == ["home", "stats", "ai", "calendar", "media", "printer", "timer", "network", "shortcuts"], f"pages saved: {pages}")
check("# Home is always shown" in conf.read_text(), "pages comment kept")
from deskhub.pages import layout_message
check(tomllib.loads(conf.read_text())["pages"].get("nav") == "bottom", "nav bar position saved")
check(layout_message(tomllib.loads(conf.read_text()))["nav"] == "bottom", "nav sent to the board")
check(tomllib.loads(conf.read_text())["display"]["pc_sleep"] == "clock", "PC sleep mode saved")
check(layout_message(tomllib.loads(conf.read_text()))["pc_sleep"] == "clock", "PC sleep mode sent to the board")
check(layout_message({})["pc_sleep"] == "off" and layout_message({"display": {"pc_sleep": "nap"}})["pc_sleep"] == "off",
      "PC sleep defaults to off")
_d = tomllib.loads(conf.read_text())["display"]
check(_d.get("night") == "11pm-7:30am" and _d.get("night_style") == "clock", f"night mode saved: {_d}")
_m = layout_message(tomllib.loads(conf.read_text()))
check((_m["night_start"], _m["night_end"], _m["night_style"]) == (23 * 60, 7 * 60 + 30, "clock"), f"night sent: {_m}")
_m = layout_message({})
check((_m["night_start"], _m["night_end"], _m["night_style"]) == (-1, -1, "off"), "no night mode by default")
from deskhub.pages import parse_hhmm
check([parse_hhmm(s) for s in ("23:00", "7", "12am", "12pm", "7:05 p.m.", "24:00", "13pm", "x")]
      == [1380, 420, 0, 720, 1145, None, None, None], "time parsing")
check(layout_message({"display": {"night": "22:00-22:00"}})["night_start"] == -1, "empty night ignored")
check(layout_message({})["nav"] == "left" and layout_message({"pages": {"nav": "top"}})["nav"] == "left", "nav defaults to left")
shutil.rmtree(root)

print("\nPROBLEMS:" if problems else "\nOK", problems if problems else "")
