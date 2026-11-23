"""`deskhub-setup <topic>` runs one part of setup on its own. Temp files only."""
import builtins
import contextlib
import io
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import tomlkit
from deskhub import calendars_cli
from deskhub import setup_cli as s

problems = []


def check(ok, what):
    print(("ok  " if ok else "BAD ") + what)
    if not ok:
        problems.append(what)


def run(argv, answers):
    """Run deskhub-setup with these arguments and typed answers; returns (output, exit code)."""
    it = iter(answers)
    real_input, real_argv = builtins.input, sys.argv
    builtins.input = lambda prompt="": next(it)
    sys.argv = ["deskhub-setup", *argv]
    out, code = io.StringIO(), 0
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            s.main()
    except SystemExit as e:
        code = e.code or 0
    finally:
        builtins.input, sys.argv = real_input, real_argv
    return out.getvalue(), code


check(s.topic_name("Calendars") == "calendar" and s.topic_name("grid") == "home"
      and s.topic_name("ai") == "claude" and s.topic_name("radio") is None, "names and aliases")

with tempfile.TemporaryDirectory() as d:
    d = pathlib.Path(d)
    config = d / "config.toml"
    config.write_text('[hub]\ntoken = "abc"\n', encoding="utf-8")
    fw = d / "firmware"
    fw.mkdir()
    sdk = fw / "sdkconfig"
    sdk.write_text('CONFIG_HUB_CITY="Charlotte"\nCONFIG_HUB_TOKEN="abc"\nCONFIG_HUB_FAHRENHEIT=y\n'
                   '# CONFIG_HUB_24H is not set\n', encoding="utf-8")
    base = ["--config", str(config), "--firmware", str(fw)]

    out, code = run(["--help"], [])
    check(code == 0 and "calendar" in out and "weather" in out and "internet" in out, "--help lists the topics")

    out, code = run(["radio", *base], [])
    check(code == 2 and "Unknown topic: radio" in out, "unknown topic explained")

    out, _ = run(["internet", *base], ["", "6", "y"])
    net = tomlkit.parse(config.read_text(encoding="utf-8"))["internet"]
    check(net["speedtest_hours"] == 6 and net["show_ip"] is True, f"internet saved: {dict(net)}")
    check("Restart the companion" in out, "says to restart the companion")
    check("Wi-Fi" not in out and "Calendars" not in out, "only that topic runs")

    out, _ = run(["internet", *base], ["", "", ""])
    check("Restart the companion" not in out, "nothing changed, no restart note")

    out, _ = run(["grid", *base], ["y", "n", "weather", "2x1", ""])
    home = tomlkit.parse(config.read_text(encoding="utf-8"))["home"]
    check(home["grid"] == [{"widget": "weather", "x": 0, "y": 0, "w": 2, "h": 1}], "alias grid runs the Home step")

    # Weather keeps the city on Enter, flips the clock to 24-hour, then offers to flash.
    out, _ = run(["weather", *base], ["", "", "y", "n"])
    check('CONFIG_HUB_24H=y' in sdk.read_text(encoding="utf-8"), "weather topic saves units to sdkconfig")
    check("Build and flash" in out and "Later: uv run deskhub-flash" in out, "firmware topic offers to flash")

    out, _ = run(["weather", *base], ["", "", "y"])
    check("Nothing changed" in out and "Build and flash" not in out, "no change, no flash")

    # The calendar topic goes straight to the menu, with no "Add or change" question,
    # then asks how many days ahead (a bad answer first).
    opened = []
    calendars_cli.menu = lambda path: opened.append(path)
    run(["calendar", *base], ["99", "14"])
    check(opened == [config], "calendar opens the calendar menu directly")
    check(tomlkit.parse(config.read_text(encoding="utf-8"))["calendar"]["days"] == 14, "calendar days saved")

    def cfg():
        return tomlkit.parse(config.read_text(encoding="utf-8")).unwrap()

    # Firmware topics on their own: the clock and the screen timeout.
    run(["time", *base], ["n", "n"])
    check("# CONFIG_HUB_24H is not set" in sdk.read_text(encoding="utf-8"), "clock topic: back to 12-hour")
    out, _ = run(["screen", *base], ["abc", "15", "n"])
    check("CONFIG_HUB_SCREEN_OFF_MIN=15" in sdk.read_text(encoding="utf-8") and "Build and flash" in out,
          "screen timeout saved, flash offered")

    # The board's address, checked (no network in tests).
    import httpx
    real_get = httpx.get

    class R:
        status_code = 200
    httpx.get = lambda url, **kw: R()
    try:
        out, _ = run(["hub", *base], ["192.168.1.50"])
    finally:
        httpx.get = real_get
    check(cfg()["hub"] == {"token": "abc", "host": "192.168.1.50"} and "answers at 192.168.1.50" in out,
          f"hub address saved: {cfg()['hub']}")

    # Internet off.
    run(["net", *base], ["n"])
    check(cfg()["internet"]["enabled"] is False, "internet checks turned off")

    # Built-in games: leave two out (one misspelt first), then none.
    import deskhub.games_cli as games_cli
    games_cli.menu = lambda path: None
    s.step_steam_key = lambda: None
    run(["presets", *base], ["", "roblox, minecraft, Pong", "roblox, minecraft"])
    check(cfg()["game_presets"] == {"disabled": ["Roblox", "Minecraft"]}, f"presets left out: {cfg().get('game_presets')}")
    run(["games", *base], ["", "none"])
    check(cfg()["game_presets"] == {"disabled": []}, "none clears the list")
    run(["games", *base], ["n"])
    check(cfg()["game_presets"]["enabled"] is False, "built-in games off")

    # Steam on and off.
    run(["steam", *base], ["n"])
    check(cfg()["steam"] == {"enabled": False}, "Steam off")

    # Alerts: the wait before a finished session alerts.
    s.hook_events_installed = lambda: {"x"}
    s.hook_events_wanted = lambda: {"x"}
    run(["alerts", *base], ["", "45"])
    check(cfg()["claude"]["idle_alert_seconds"] == 45, "idle alert seconds saved")

    # Every topic has a step behind it.
    for name in s.TOPICS:
        check(name in s.FIRMWARE_TOPICS or name in ("calendar", "printer", "media", "games", "steam", "hub", "shortcuts",
              "pages", "home", "display", "internet", "github", "claude", "alerts", "flash", "startup"), f"{name} runs")

print("\nPROBLEMS:" if problems else "\nOK", problems if problems else "")
