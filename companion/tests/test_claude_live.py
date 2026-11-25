"""Live Claude limits: an expired sign-in gets Claude Code to renew it, at most
every 10 minutes, and a renewed token is used straight away. No network."""
import json
import pathlib
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import httpx
from deskhub import claude_live

problems = []


def check(ok, what):
    print(("ok  " if ok else "BAD ") + what)
    if not ok:
        problems.append(what)


class Resp:
    status_code = 200

    def json(self):
        return {"five_hour": {"utilization": 42.0, "resets_at": "2099-01-01T00:00:00+00:00"},
                "seven_day": {"utilization": 17.0, "resets_at": "2099-01-02T00:00:00+00:00"}}


sent = []
httpx.get = lambda url, **kw: sent.append(kw["headers"]["Authorization"]) or Resp()

with tempfile.TemporaryDirectory() as d:
    creds = pathlib.Path(d) / ".credentials.json"

    def write(token, expires_in):
        creds.write_text(json.dumps({"claudeAiOauth": {
            "accessToken": token, "expiresAt": int((time.time() + expires_in) * 1000)}}), encoding="utf-8")

    # Expired, and the renew works: the new token is used at once.
    write("old", -3600)
    calls = []

    def renew():
        calls.append(1)
        write("new", 8 * 3600)
        return True

    live = claude_live.LiveLimits(creds, renew=renew)
    got = live.get(active=True)
    check(calls == [1], "an expired sign-in asks Claude Code to renew it")
    check(sent == ["Bearer new"], f"the renewed token is used: {sent}")
    check(got.get("five_hour", {}).get("pct") == 42.0 and got.get("seven_day", {}).get("pct") == 17.0,
          f"limits read: {got}")

    # Expired, and the renew doesn't help: no request, and no second try for 10 minutes.
    write("old", -3600)
    sent.clear()
    calls.clear()
    live = claude_live.LiveLimits(creds, renew=lambda: calls.append(1) or True)
    live.get(active=True)
    live._next = 0   # poll again right away
    live.get(active=True)
    check(calls == [1], f"renew tried once per 10 minutes, not every poll: {len(calls)}")
    check(sent == [], "an expired token is never sent")

    # A token that's still good needs no renew.
    write("good", 3600)
    calls.clear()
    claude_live.LiveLimits(creds, renew=lambda: calls.append(1) or True).get(active=True)
    check(calls == [], "a valid sign-in isn't renewed")

check(claude_live.renew_signin(None) is False, "no claude CLI: nothing to run")

weekly = claude_live.weekly_limits([
    {"kind": "weekly_all", "group": "weekly", "percent": 14, "resets_at": "2099-01-01T00:00:00+00:00",
     "scope": None},
    {"kind": "weekly_scoped", "group": "weekly", "percent": 0, "resets_at": "2099-01-01T00:00:00+00:00",
     "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None}},
    {"kind": "weekly_all", "group": "weekly", "percent": 50, "resets_at": "2000-01-01T00:00:00+00:00"},
    {"kind": "session", "group": "five_hour", "percent": 9, "resets_at": "2099-01-01T00:00:00+00:00"},
], time.time())
check([(w["name"], w["pct"]) for w in weekly] == [("HAFTALIK", 14.0), ("FABLE", 0.0)],
      f"weekly limits from the limits list: {weekly}")
check(claude_live.weekly_limits(None, time.time()) == [], "no limits list: nothing")

print("\nPROBLEMS:" if problems else "\nOK", problems if problems else "")
