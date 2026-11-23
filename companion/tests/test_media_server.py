"""Plex and Jellyfin with fake servers: parsing sessions, picking one to show, the board
message, Jellyfin controls, poster covers, discovery replies, back-off, and the setup
step (fake credential store). Never contacts a real server.

    uv run python tests/test_media_server.py
"""
import builtins
import getpass
import io
import pathlib
import struct
import sys
import tempfile

from PIL import Image

from deskhub import media_server as ms, setup_cli

problems = []


def check(cond, msg):
    if not cond:
        problems.append(msg)


def png(w, h, color=(200, 30, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, "PNG")
    return buf.getvalue()


# ---- Plex ---------------------------------------------------------------------

PLEX_SESSIONS = {"MediaContainer": {"size": 2, "Metadata": [
    {"type": "episode", "title": "The Pilot", "grandparentTitle": "Some Show", "parentIndex": 2, "index": 5,
     "viewOffset": 600_000, "duration": 1_800_000, "grandparentThumb": "/library/metadata/10/thumb/1",
     "thumb": "/library/metadata/12/thumb/1", "Player": {"title": "Living Room TV", "state": "paused"},
     "User": {"title": "alex"}, "Session": {"id": "abc"}},
    {"type": "movie", "title": "A Movie", "year": 1999, "viewOffset": 60_000, "duration": 7_200_000,
     "thumb": "/library/metadata/20/thumb/2", "Player": {"title": "Phone", "state": "playing"},
     "User": {"title": "sam"}, "Session": {"id": "def"}, "TranscodeSession": {"videoDecision": "transcode"}},
]}}
plex_calls = []


def plex_get(url, token, path, params=None, raw=False, tls_name=""):
    plex_calls.append((path, params, token, tls_name))
    if token == "bad":
        raise RuntimeError("401 Unauthorized")
    if path == "/status/sessions":
        return PLEX_SESSIONS
    if path == "/photo/:/transcode":
        return png(300, 300)   # square, so the 2:3 poster frame gets bands
    if path == "/":
        return {"MediaContainer": {"version": "1.41.2.9200-c6bbc1b53"}}
    if path == "/library/sections":
        return {"MediaContainer": {"Directory": [{"key": "1", "type": "movie"}, {"key": "2", "type": "show"},
                                                 {"key": "3", "type": "photo"}]}}
    if path.startswith("/library/sections/"):
        return {"MediaContainer": {"totalSize": {1: 312, 2: 45, 4: 2104}[params["type"]]}}
    raise AssertionError(path)


plex = ms.PlexServer("http://plex.test:32400/", "tok", get=plex_get)
sessions = plex.sessions()
check(len(sessions) == 2, f"plex sessions: {sessions}")
ep, movie = sessions
check(ep.title == "The Pilot" and ep.subtitle == "Some Show - S2 E5", f"plex episode: {ep}")
check(ep.user == "alex" and ep.device == "Living Room TV" and not ep.playing, f"plex player: {ep}")
check(ep.pos == 600 and ep.dur == 1800 and ep.id == "abc", f"plex times: {ep}")
check(ep.art_key == "/library/metadata/10/thumb/1", f"plex episode uses the show poster: {ep.art_key}")
check(not ep.can_control and not ep.can_seek, "plex is display-only")
check(movie.subtitle == "1999" and movie.playing and movie.transcoding, f"plex movie: {movie}")
check(plex.url == "http://plex.test:32400", "trailing slash dropped")

# ---- Jellyfin -----------------------------------------------------------------

JF_SESSIONS = [
    {"Id": "idle", "UserName": "alex", "DeviceName": "Laptop"},   # nothing playing
    {"Id": "s1", "UserName": "alex", "DeviceName": "Shield", "SupportsRemoteControl": True,
     "PlayState": {"PositionTicks": 300 * ms.TICKS, "IsPaused": False, "CanSeek": True},
     "NowPlayingItem": {"Type": "Episode", "Name": "Ep", "SeriesName": "Show", "ParentIndexNumber": 1,
                        "IndexNumber": 3, "RunTimeTicks": 2400 * ms.TICKS, "Id": "item1", "SeriesId": "series1",
                        "SeriesPrimaryImageTag": "t", "ImageTags": {"Primary": "x"}}},
    {"Id": "s2", "UserName": "sam", "DeviceName": "Kitchen", "SupportsRemoteControl": True,
     "PlayState": {"PositionTicks": 10 * ms.TICKS, "IsPaused": True},
     "NowPlayingItem": {"Type": "Audio", "Name": "Song", "AlbumArtist": "Band", "Album": "Record",
                        "RunTimeTicks": 200 * ms.TICKS, "Id": "track1", "AlbumId": "album1",
                        "AlbumPrimaryImageTag": "a"},
     "TranscodingInfo": {"IsVideoDirect": True}},
    {"Id": "s3", "UserName": "kid", "DeviceName": "Tablet", "SupportsRemoteControl": False,
     "PlayState": {"PositionTicks": 0, "IsPaused": False},
     "NowPlayingItem": {"Type": "Movie", "Name": "Film", "ProductionYear": 2020, "RunTimeTicks": 0, "Id": "m1"}},
]
jf_calls = []


def jf_request(method, url, key, path, params=None, raw=False):
    jf_calls.append((method, path, params))
    if method == "GET" and path == "/Sessions":
        return JF_SESSIONS
    if method == "GET" and path.endswith("/Images/Primary"):
        return png(400, 400, (0, 0, 255))
    if method == "POST":
        return None
    if path == "/System/Info":
        return {"Version": "10.10.3", "ServerName": "jelly"}
    if path == "/Items/Counts":
        return {"MovieCount": 1, "SeriesCount": 0, "EpisodeCount": 0, "SongCount": 5000}
    raise AssertionError(path)


jf = ms.JellyfinServer("http://jf.test:8096", "key", "Home Jellyfin", request=jf_request)
js = jf.sessions()
check([s.id for s in js] == ["s1", "s2", "s3"], f"jellyfin skips idle sessions: {[s.id for s in js]}")
s1, s2, s3 = js
check(s1.subtitle == "Show - S1 E3" and s1.art_key == "series1" and s1.pos == 300 and s1.dur == 2400,
      f"jellyfin episode: {s1}")
check(s1.can_control and s1.can_seek and s1.playing, f"jellyfin controls: {s1}")
check(s2.subtitle == "Band - Record" and s2.art_key == "album1" and not s2.playing and not s2.can_seek,
      f"jellyfin audio: {s2}")
check(not s2.transcoding, "direct stream is not transcoding")
check(s3.subtitle == "2020" and s3.art_key == "" and not s3.can_control, f"jellyfin movie, no image: {s3}")

jf.command(s1, "toggle")
jf.command(s1, "seek", 90)
jf.command(s1, "next")
jf.command(s3, "toggle")   # no remote control: nothing sent
posts = [c for c in jf_calls if c[0] == "POST"]
check(posts == [("POST", "/Sessions/s1/Playing/PlayPause", None),
                ("POST", "/Sessions/s1/Playing/Seek", {"seekPositionTicks": 90 * ms.TICKS}),
                ("POST", "/Sessions/s1/Playing/NextTrack", None)], f"jellyfin commands: {posts}")

# ---- picking a session, the board message --------------------------------------

both = ms.MediaServers([plex, jf])
both.poll(now=100.0)
check(len(both.sessions) == 5, f"all sessions: {len(both.sessions)}")
check(both.current[1].id == "def", f"first playing session shown: {both.current[1].id}")
check(both.interval() == ms.POLL_ACTIVE_S, "polls fast while something plays")
msg = both.message(now=104.0)
check(msg["active"] and msg["playing"] and msg["title"] == "A Movie", f"message: {msg}")
check(msg["pos"] == 64 and msg["dur"] == 7200, f"position extrapolated while playing: {msg}")
check(msg["source"] == "Plex - transcoding", f"source: {msg['source']}")
check(msg["album"] == "Phone - sam (+4 more)", f"who and how many more: {msg['album']}")
check(msg["type"] == "media_server" and msg["name"] == "Plex" and msg["online"], f"server section: {msg}")
check(msg["streams"] == 5 and msg["transcoding"] == 1, f"stream counts: {msg}")
check(msg["version"] == "Plex Media Server 1.41.2", f"version: {msg['version']}")
check(msg["library"] == "312 movies, 45 shows, 2,104 episodes", f"library: {msg['library']}")
check(msg["controls"] is False and msg["can_seek"] is False, "plex: no controls on the board")

# Sticky: a later poll keeps showing the same session while it plays.
PLEX_SESSIONS["MediaContainer"]["Metadata"][0]["Player"]["state"] = "playing"
both.poll(now=110.0)
check(both.current[1].id == "def", "keeps the shown session while it plays")
# It pauses while another plays: switch.
PLEX_SESSIONS["MediaContainer"]["Metadata"][1]["Player"]["state"] = "paused"
both.poll(now=120.0)
check(both.current[1].id == "abc", f"moves to a playing session: {both.current[1].id}")

# Covers: a tall poster fits inside the square, on black.
frame = both.art()
check(frame is not None and frame[:4] == b"ARS1", "server cover has its own frame type")
art_id, w, h = struct.unpack("<IHH", frame[4:12])
check((w, h) == (ms.POSTER_W, ms.POSTER_H) and len(frame) == 12 + w * h * 2, "2:3 poster size")
px = frame[12:]
corner = px[0] | px[1] << 8
middle = (h // 2 * w + w // 2) * 2
check(corner == 0 and (px[middle] | px[middle + 1] << 8) != 0, "square cover letterboxed, not cropped")
check(both.art() is None, "no new frame for the same cover")
check(both.message()["art_id"] == art_id and both.current_art() == frame, "message names the cover")

# A cover that fails to load is tried again after a while, not dropped for the whole show.
class FlakyCover(ms.JellyfinServer):
    fails = 1

    def cover(self, s):
        if self.fails:
            self.fails -= 1
            raise RuntimeError("timeout")
        return png(100, 100)


fc = ms.MediaServers([FlakyCover("http://jf.test:8096", "key", request=jf_request)])
fc.poll(now=0.0)
check(fc.art() is None and fc.art() is None, "failed cover not retried at once")
fc._art_failed = (fc._art_failed[0], fc._art_failed[1] - ms.ART_RETRY_S - 1)
check(fc.art() is not None and fc.message()["art_id"] != 0, "failed cover retried later")

# Only some users.
only = ms.MediaServers([jf], users=["SAM"])
only.poll(now=0.0)
check([s.id for _, s in only.sessions] == ["s2"], "server_users filter, any case")
m = only.message(now=50.0)
check(m["pos"] == 10 and not m["playing"] and m["controls"] is True, f"paused jellyfin message: {m}")
only.command("toggle")
check(only.current[1].playing, "toggle shows at once")
check(jf_calls[-1] == ("POST", "/Sessions/s2/Playing/PlayPause", None), "command goes to the shown session")
only.command("seek", 30)   # this one can't seek
check(jf_calls[-1][1] != "/Sessions/s2/Playing/Seek", "no seek when the session can't")

# Nothing anywhere.
empty = ms.MediaServers([])
empty.poll()
check(empty.message()["configured"] is False and empty.interval() == ms.POLL_IDLE_S, "nothing set up")

# Idle server: its name, version and library, "nothing playing".
quiet = ms.MediaServers([jf], users=["nobody"])
quiet.poll(now=0.0)
q = quiet.message()
check(q["active"] is False and q["configured"] and q["online"] and q["name"] == "Home Jellyfin", f"idle: {q}")
check(q["version"] == "Jellyfin 10.10.3" and q["library"] == "1 movie, 5,000 songs", f"idle info: {q}")
n_info = len([c for c in jf_calls if c[1] == "/System/Info"])
quiet.poll(now=60.0)
check(len([c for c in jf_calls if c[1] == "/System/Info"]) == n_info, "library sizes not asked every poll")
quiet.poll(now=ms.INFO_S + 1)
check(len([c for c in jf_calls if c[1] == "/System/Info"]) == n_info + 1, "refreshed after a while")
# Unreachable: offline, still named.
down = ms.MediaServers([ms.PlexServer("http://plex.test:32400", "bad", "beans", get=plex_get)])
down.poll(now=0.0)
d = down.message()
check(d["online"] is False and d["name"] == "beans" and not d["active"], f"offline server: {d}")

# A server that fails waits out the back-off, keeping its last sessions.
bad = ms.PlexServer("http://plex.test:32400", "bad", get=plex_get)
flaky = ms.MediaServers([bad, jf])
flaky.poll(now=0.0)
n = len([c for c in plex_calls if c[2] == "bad"])
flaky.poll(now=10.0)
check(len([c for c in plex_calls if c[2] == "bad"]) == n, "failed server not asked again during back-off")
flaky.poll(now=ms.BACKOFF_S + 1)
check(len([c for c in plex_calls if c[2] == "bad"]) == n + 1, "asked again after back-off")
check(len(flaky.sessions) == 3, "the other server still shows")

# ---- reaching a Plex server --------------------------------------------------


class Resp:
    def __init__(self, data, status=200):
        self.data, self.status_code = data, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self.data


RESOURCES = [
    {"clientIdentifier": "other", "connections": []},
    {"clientIdentifier": "m1", "accessToken": "srv-tok", "connections": [
        {"protocol": "https", "address": "203.0.113.9", "port": 32400,
         "uri": "https://203-0-113-9.hash.plex.direct:32400", "local": False},
        {"protocol": "https", "address": "192.168.0.142", "port": 32400,
         "uri": "https://192-168-0-142.hash.plex.direct:32400", "local": True}]},
]
tv = lambda url, **kw: Resp(RESOURCES)
conn = ms.plex_connection("http://192.168.0.142:32400", "acct", get=tv, identity=lambda u: ("m1", "https"))
check(conn == {"url": "https://192.168.0.142:32400", "tls_name": "192-168-0-142.hash.plex.direct",
               "token": "srv-tok"}, f"secure local connection from plex.tv: {conn}")
down = lambda url, **kw: Resp([], 503)
conn = ms.plex_connection("http://192.168.0.142:32400", "acct", get=down, identity=lambda u: ("m1", "http"))
check(conn == {"url": "http://192.168.0.142:32400", "tls_name": "", "token": "acct"}, f"plain HTTP fallback: {conn}")
try:
    ms.plex_connection("http://192.168.0.142:32400", "acct", get=down, identity=lambda u: ("m1", "https"))
    check(False, "secure-only server without plex.tv should fail")
except RuntimeError as e:
    check("secure" in str(e), f"says why: {e}")

seen = []


def ident_get(url, **kw):
    seen.append((url, kw.get("verify"), kw.get("headers", {}).get("X-Plex-Token")))
    if url.startswith("http://"):
        raise ConnectionResetError("reset")
    return Resp({"MediaContainer": {"machineIdentifier": "m1"}})


check(ms.plex_identity("http://192.168.0.142:32400", get=ident_get) == ("m1", "https"), "identity over HTTPS")
check(seen[-1] == ("https://192.168.0.142:32400/identity", False, None), f"no token on the unchecked probe: {seen}")

# ---- discovery replies ----------------------------------------------------------

gdm = (b"HTTP/1.0 200 OK\r\nContent-Type: plex/media-server\r\nResource-Identifier: 123\r\n"
       b"Name: basement\r\nPort: 32400\r\nVersion: 1.40\r\n\r\n")
check(ms.parse_discovery(gdm, "192.168.1.5") == {"type": "plex", "name": "basement",
                                                 "url": "http://192.168.1.5:32400"}, "plex GDM reply")
jfr = b'{"Address":"http://192.168.1.6:8096/","Id":"x","Name":"jelly"}'
check(ms.parse_discovery(jfr, "192.168.1.6") == {"type": "jellyfin", "name": "jelly",
                                                 "url": "http://192.168.1.6:8096"}, "jellyfin reply")
check(ms.parse_discovery(b"M-SEARCH * HTTP/1.1\r\n\r\n", "1.2.3.4") is None, "own probe ignored")
check(ms.parse_discovery(b"{bad json", "1.2.3.4") is None, "bad json ignored")

# ---- setup step and config ---------------------------------------------------

store = {}


class FakeKeyring:
    class errors:
        class PasswordDeleteError(Exception):
            pass

    @staticmethod
    def get_password(service, user):
        return store.get((service, user))

    @staticmethod
    def set_password(service, user, value):
        store[(service, user)] = value

    @staticmethod
    def delete_password(service, user):
        if (service, user) not in store:
            raise FakeKeyring.errors.PasswordDeleteError()
        del store[(service, user)]


sys.modules["keyring"] = FakeKeyring
answers = []
builtins.input = lambda prompt="": answers.pop(0)
getpass.getpass = lambda prompt="": answers.pop(0)
ms.discover = lambda timeout=2.0: [{"type": "jellyfin", "name": "jelly", "url": "http://192.168.1.6:8096"}]
ms._jf_request = jf_request
orig_init = ms.JellyfinServer.__init__
ms.JellyfinServer.__init__ = lambda self, url, key, name="Jellyfin", request=None: orig_init(
    self, url, key, name, request=jf_request)

with tempfile.TemporaryDirectory() as tmp:
    cfg = pathlib.Path(tmp) / "config.toml"
    cfg.write_text("[hub]\nhost = \"deskhub.local\"\n", encoding="utf-8")
    # add the discovered Jellyfin with a key, then done
    answers[:] = ["a", "1", "the-key", ""]
    setup_cli.step_media_servers(cfg, ask_first=False)
    entries = setup_cli.media_server_entries(cfg)
    check(entries == [{"type": "jellyfin", "name": "jelly", "url": "http://192.168.1.6:8096"}], f"saved: {entries}")
    check(ms.get_secret("http://192.168.1.6:8096/") == "the-key", "key in the credential store")
    check("the-key" not in cfg.read_text(encoding="utf-8"), "key not in config.toml")
    check(cfg.read_text(encoding="utf-8").startswith("[hub]"), "rest of config kept")
    built = ms.build_servers(entries)
    check(len(built) == 1 and built[0].key == "the-key" and built[0].name == "jelly", "companion builds it")
    check(ms.build_servers([{"type": "plex", "url": "http://x:32400"}]) == [], "skipped without a token")
    check(ms.build_servers([{"type": "emby", "url": "http://x"}]) == [], "unknown type skipped")

    # a typed Plex address with a pasted token that the server refuses: not saved
    orig_plex = ms.PlexServer.__init__
    ms.PlexServer.__init__ = lambda self, url, token, name="Plex", tls_name="", get=None: orig_plex(
        self, url, token, name, tls_name=tls_name, get=plex_get)
    # plex.tv knows the server's secure address and hands out the server's own token.
    ms.plex_connection = lambda url, token: (
        {"url": "http://10.0.0.9:32400", "tls_name": "", "token": token} if token == "bad" else
        {"url": "https://10.0.0.9:32400", "tls_name": "10-0-0-9.abc.plex.direct", "token": "server-token"})
    answers[:] = ["a", "10.0.0.9", "plex", "n", "bad", ""]
    ms.discover = lambda timeout=2.0: []
    setup_cli.step_media_servers(cfg, ask_first=False)
    check(len(setup_cli.media_server_entries(cfg)) == 1, "refused token not saved")
    # same with a good token: saved with the default port
    answers[:] = ["a", "10.0.0.9", "plex", "n", "good", ""]
    setup_cli.step_media_servers(cfg, ask_first=False)
    saved = setup_cli.media_server_entries(cfg)
    check([e["url"] for e in saved] == ["http://192.168.1.6:8096", "https://10.0.0.9:32400"], f"plex added: {saved}")
    check(saved[1].get("tls_name") == "10-0-0-9.abc.plex.direct", "plex.direct name saved")
    check(ms.get_secret("https://10.0.0.9:32400") == "server-token", "server's token saved")
    built = ms.build_servers(saved)
    check(built[1].tls_name == "10-0-0-9.abc.plex.direct", "companion checks the certificate by name")
    built[1].sessions()
    check(plex_calls[-1][3] == "10-0-0-9.abc.plex.direct", "requests carry the name")

    # remove the first
    answers[:] = ["r", "1", ""]
    setup_cli.step_media_servers(cfg, ask_first=False)
    check([e["url"] for e in setup_cli.media_server_entries(cfg)] == ["https://10.0.0.9:32400"], "removed")
    check(ms.get_secret("http://192.168.1.6:8096") == "", "its key removed too")

    # Plex browser sign-in: PIN approved on the second check.
    tokens = iter(["", "plex-token"])
    ms.plex_pin = lambda post=None: {"id": 7, "code": "ABCD", "url": "https://app.plex.tv/auth#?code=ABCD"}
    ms.plex_pin_token = lambda pin_id, get=None: next(tokens)
    opened = []
    check(setup_cli.plex_sign_in(open_url=opened.append, sleep=lambda s: None) == "plex-token", "plex PIN sign-in")
    check(opened == ["https://app.plex.tv/auth#?code=ABCD"], "opens the browser")

check(setup_cli.topic_name("plex") == "media" and setup_cli.topic_name("jellyfin") == "media", "setup topic")

if problems:
    print("\n".join(problems))
    sys.exit(1)
print("OK media servers")
