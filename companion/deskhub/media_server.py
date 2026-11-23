"""Plex and Jellyfin: what plays anywhere on the media server, for the Media page.

The companion asks each server on the LAN. Plex: `GET /status/sessions` with an
`X-Plex-Token`. Jellyfin: `GET /Sessions` with an API key made in its dashboard
(Administration > API Keys). Tokens and keys live in the Windows credential store
(service "deskhub-media", one entry per server URL), never in config.toml.

Jellyfin sessions can be paused, resumed, skipped and seeked from the board. Plex
can only reach players that allow remote control, which many don't, so Plex is
display-only.

    [[media_servers]]
    type = "jellyfin"              # or "plex"
    url = "http://192.168.1.20:8096"
"""
import io
import json
import logging
import socket
import time
import uuid
import zlib
from dataclasses import dataclass, field

from PIL import Image

from .media import ART_SIZE, art_frame
from .text import ascii_fold

log = logging.getLogger(__name__)

KEYRING_SERVICE = "deskhub-media"
PRODUCT = "Desk Hub"
POLL_ACTIVE_S = 3
POLL_IDLE_S = 15
BACKOFF_S = 60
ART_RETRY_S = 30
TICKS = 10_000_000   # Jellyfin times are in 100 ns ticks
PLEX_PORT = 32400
JELLYFIN_PORT = 8096


# ---- credential store -------------------------------------------------------

def _key_user(url: str) -> str:
    return url.rstrip("/").lower()


def get_secret(url: str) -> str:
    try:
        import keyring

        return keyring.get_password(KEYRING_SERVICE, _key_user(url)) or ""
    except Exception as e:  # no keyring backend
        log.debug("media server secret: %s", e)
        return ""


def set_secret(url: str, secret: str | None):
    import keyring

    if secret:
        keyring.set_password(KEYRING_SERVICE, _key_user(url), secret)
    else:
        try:
            keyring.delete_password(KEYRING_SERVICE, _key_user(url))
        except keyring.errors.PasswordDeleteError:
            pass


def client_id() -> str:
    """A stable id for this PC, which Plex and Jellyfin want with every request."""
    try:
        import keyring

        cid = keyring.get_password(KEYRING_SERVICE, "client-id")
        if not cid:
            cid = uuid.uuid4().hex
            keyring.set_password(KEYRING_SERVICE, "client-id", cid)
        return cid
    except Exception:
        return "deskhub-" + uuid.UUID(int=uuid.getnode()).hex[-12:]


# ---- one session, whichever server it came from -----------------------------

@dataclass
class Session:
    server: str            # "Plex" or "Jellyfin" (or the name from config)
    id: str                # the server's session id
    kind: str              # "episode", "movie", "track", "video"
    title: str
    subtitle: str          # show and episode, artist, or year
    user: str
    device: str
    playing: bool
    pos: float             # seconds, as of `seen`
    dur: float
    art_key: str           # what to fetch for the cover ("" = none)
    can_control: bool
    can_seek: bool
    transcoding: bool = False
    seen: float = field(default_factory=time.monotonic)

    def position(self, now: float | None = None) -> float:
        pos = self.pos
        if self.playing:
            pos += (now if now is not None else time.monotonic()) - self.seen
        return min(pos, self.dur) if self.dur > 0 else pos


def _episode_line(show: str, season, episode) -> str:
    if season is not None and episode is not None:
        return f"{show} - S{season} E{episode}" if show else f"S{season} E{episode}"
    return show or ""


# ---- Plex -------------------------------------------------------------------

def _plex_get(url: str, token: str, path: str, params: dict | None = None, raw: bool = False):
    import httpx

    r = httpx.get(url.rstrip("/") + path, params=params, timeout=8,
                  headers={"X-Plex-Token": token, "Accept": "application/json",
                           "X-Plex-Product": PRODUCT, "X-Plex-Client-Identifier": client_id()})
    r.raise_for_status()
    return r.content if raw else r.json()


class PlexServer:
    kind = "plex"

    def __init__(self, url: str, token: str, name: str = "Plex", get=_plex_get):
        self.url, self.token, self.name, self._get = url.rstrip("/"), token, name, get

    def sessions(self) -> list[Session]:
        data = self._get(self.url, self.token, "/status/sessions")
        out = []
        for m in (data.get("MediaContainer") or {}).get("Metadata") or []:
            kind = m.get("type", "")
            player = m.get("Player") or {}
            if kind == "episode":
                subtitle = _episode_line(m.get("grandparentTitle", ""), m.get("parentIndex"), m.get("index"))
                art = m.get("grandparentThumb") or m.get("parentThumb") or m.get("thumb", "")
            elif kind == "track":
                subtitle = m.get("grandparentTitle") or m.get("originalTitle") or ""
                album = m.get("parentTitle", "")
                subtitle = f"{subtitle} - {album}" if subtitle and album else subtitle or album
                art = m.get("parentThumb") or m.get("thumb", "")
            else:
                subtitle = str(m.get("year", "")) if m.get("year") else ""
                art = m.get("thumb", "")
            out.append(Session(
                server=self.name,
                id=str((m.get("Session") or {}).get("id") or m.get("sessionKey") or m.get("ratingKey", "")),
                kind={"episode": "episode", "movie": "movie", "track": "track"}.get(kind, "video"),
                title=m.get("title", ""),
                subtitle=subtitle,
                user=(m.get("User") or {}).get("title", ""),
                device=player.get("title") or player.get("product") or "",
                playing=player.get("state") == "playing",
                pos=(m.get("viewOffset") or 0) / 1000,
                dur=(m.get("duration") or 0) / 1000,
                art_key=art or "",
                can_control=False,
                can_seek=False,
                transcoding=(m.get("TranscodeSession") or {}).get("videoDecision") == "transcode",
            ))
        return out

    def cover(self, s: Session) -> bytes:
        return self._get(self.url, self.token, "/photo/:/transcode",
                         {"url": s.art_key, "width": ART_SIZE, "height": ART_SIZE, "minSize": 1}, raw=True)

    def command(self, s: Session, cmd: str, value: int = 0):
        pass  # display-only, see the module docstring


# Plex sign-in through plex.tv: the user approves a PIN in the browser.
PLEX_TV = "https://plex.tv/api/v2"


def plex_pin(post=None) -> dict:
    """Start a sign-in. Returns {"id", "code", "url"}; open url, then poll plex_pin_token()."""
    import httpx
    from urllib.parse import urlencode

    post = post or httpx.post
    cid = client_id()
    r = post(f"{PLEX_TV}/pins", params={"strong": "true"}, timeout=15,
             headers={"Accept": "application/json", "X-Plex-Product": PRODUCT, "X-Plex-Client-Identifier": cid})
    r.raise_for_status()
    pin = r.json()
    url = "https://app.plex.tv/auth#?" + urlencode({"clientID": cid, "code": pin["code"],
                                                     "context[device][product]": PRODUCT})
    return {"id": pin["id"], "code": pin["code"], "url": url}


def plex_pin_token(pin_id, get=None) -> str:
    """The account token once the PIN is approved, else ""."""
    import httpx

    get = get or httpx.get
    r = get(f"{PLEX_TV}/pins/{pin_id}", timeout=15,
            headers={"Accept": "application/json", "X-Plex-Product": PRODUCT, "X-Plex-Client-Identifier": client_id()})
    r.raise_for_status()
    return r.json().get("authToken") or ""


# ---- Jellyfin ---------------------------------------------------------------

def _jf_auth(key: str) -> str:
    return (f'MediaBrowser Client="{PRODUCT}", Device="Desk Hub", DeviceId="{client_id()}", '
            f'Version="1.0", Token="{key}"')


def _jf_request(method: str, url: str, key: str, path: str, params: dict | None = None, raw: bool = False):
    import httpx

    r = httpx.request(method, url.rstrip("/") + path, params=params, timeout=8,
                      headers={"Authorization": _jf_auth(key), "Accept": "application/json"})
    r.raise_for_status()
    if raw:
        return r.content
    return r.json() if r.content else None


class JellyfinServer:
    kind = "jellyfin"

    def __init__(self, url: str, key: str, name: str = "Jellyfin", request=_jf_request):
        self.url, self.key, self.name, self._req = url.rstrip("/"), key, name, request

    def sessions(self) -> list[Session]:
        out = []
        for s in self._req("GET", self.url, self.key, "/Sessions", {"ActiveWithinSeconds": 960}) or []:
            item = s.get("NowPlayingItem")
            if not item:
                continue
            state = s.get("PlayState") or {}
            kind = item.get("Type", "")
            if kind == "Episode":
                subtitle = _episode_line(item.get("SeriesName", ""), item.get("ParentIndexNumber"),
                                         item.get("IndexNumber"))
                art = item.get("SeriesId") if item.get("SeriesPrimaryImageTag") else item.get("Id")
            elif kind == "Audio":
                artist = item.get("AlbumArtist") or ", ".join(item.get("Artists") or [])
                album = item.get("Album", "")
                subtitle = f"{artist} - {album}" if artist and album else artist or album
                art = item.get("AlbumId") if item.get("AlbumPrimaryImageTag") else item.get("Id")
            else:
                subtitle = str(item.get("ProductionYear", "")) if item.get("ProductionYear") else ""
                art = item.get("Id")
            if not (item.get("ImageTags") or {}).get("Primary") and art == item.get("Id"):
                art = ""
            remote = bool(s.get("SupportsRemoteControl"))
            out.append(Session(
                server=self.name,
                id=s.get("Id", ""),
                kind={"Episode": "episode", "Movie": "movie", "Audio": "track"}.get(kind, "video"),
                title=item.get("Name", ""),
                subtitle=subtitle,
                user=s.get("UserName", ""),
                device=s.get("DeviceName") or s.get("Client") or "",
                playing=not state.get("IsPaused", False),
                pos=(state.get("PositionTicks") or 0) / TICKS,
                dur=(item.get("RunTimeTicks") or 0) / TICKS,
                art_key=art or "",
                can_control=remote,
                can_seek=remote and bool(state.get("CanSeek")),
                transcoding=bool(s.get("TranscodingInfo")) and not (s.get("TranscodingInfo") or {}).get(
                    "IsVideoDirect", False),
            ))
        return out

    def cover(self, s: Session) -> bytes:
        return self._req("GET", self.url, self.key, f"/Items/{s.art_key}/Images/Primary",
                         {"maxWidth": ART_SIZE * 2, "maxHeight": ART_SIZE * 2, "quality": 90}, raw=True)

    def command(self, s: Session, cmd: str, value: int = 0):
        if not s.can_control:
            return
        path = f"/Sessions/{s.id}/Playing"
        if cmd == "toggle":
            self._req("POST", self.url, self.key, f"{path}/PlayPause")
        elif cmd == "next":
            self._req("POST", self.url, self.key, f"{path}/NextTrack")
        elif cmd == "prev":
            self._req("POST", self.url, self.key, f"{path}/PreviousTrack")
        elif cmd == "stop":
            self._req("POST", self.url, self.key, f"{path}/Stop")
        elif cmd == "seek" and s.can_seek:
            self._req("POST", self.url, self.key, f"{path}/Seek", {"seekPositionTicks": int(value) * TICKS})


# ---- discovery --------------------------------------------------------------

def discover(timeout: float = 2.0) -> list[dict]:
    """Plex (GDM) and Jellyfin servers that answer on the LAN: [{"type", "name", "url"}]."""
    found = {}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(0.3)
    try:
        sock.sendto(b"M-SEARCH * HTTP/1.1\r\n\r\n", ("239.0.0.250", 32414))
        sock.sendto(b"M-SEARCH * HTTP/1.1\r\n\r\n", ("255.255.255.255", 32414))
        sock.sendto(b"who is JellyfinServer?", ("255.255.255.255", 7359))
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                data, (ip, _) = sock.recvfrom(4096)
            except (socket.timeout, OSError):
                continue
            hit = parse_discovery(data, ip)
            if hit:
                found[hit["url"]] = hit
    finally:
        sock.close()
    return list(found.values())


def parse_discovery(data: bytes, ip: str) -> dict | None:
    text = data.decode("utf-8", "replace")
    if text.lstrip().startswith("{"):
        try:
            j = json.loads(text)
        except ValueError:
            return None
        if "Address" in j:
            return {"type": "jellyfin", "name": j.get("Name") or "Jellyfin", "url": j["Address"].rstrip("/")}
        return None
    if text.startswith("HTTP/1.0 200") or "Resource-Identifier:" in text:
        fields = dict(line.split(":", 1) for line in text.splitlines() if ":" in line)
        fields = {k.strip(): v.strip() for k, v in fields.items()}
        port = fields.get("Port", str(PLEX_PORT))
        return {"type": "plex", "name": fields.get("Name") or "Plex", "url": f"http://{ip}:{port}"}
    return None


# ---- all servers together, for the companion ---------------------------------

def to_poster_frame(art_id: int, raw: bytes) -> bytes:
    """Fit a cover (often a tall poster) inside the square, on black, as an ART1 frame."""
    im = Image.open(io.BytesIO(raw)).convert("RGB")
    im.thumbnail((ART_SIZE, ART_SIZE), Image.LANCZOS)
    canvas = Image.new("RGB", (ART_SIZE, ART_SIZE), (0, 0, 0))
    canvas.paste(im, ((ART_SIZE - im.width) // 2, (ART_SIZE - im.height) // 2))
    return art_frame(art_id, canvas)


def build_servers(entries: list[dict]) -> list:
    servers = []
    for e in entries or []:
        kind, url = str(e.get("type", "")).lower(), str(e.get("url", "")).rstrip("/")
        if kind not in ("plex", "jellyfin") or not url:
            log.warning("media server %r: needs type = \"plex\" or \"jellyfin\" and a url", e)
            continue
        secret = get_secret(url)
        if not secret:
            log.info("media server %s has no token saved; run: uv run deskhub-setup media", url)
            continue
        name = str(e.get("name") or kind.title())
        servers.append(PlexServer(url, secret, name) if kind == "plex" else JellyfinServer(url, secret, name))
    return servers


class MediaServers:
    """Polls every server and picks one session to show (playing first, sticky)."""

    def __init__(self, servers: list, users: list[str] | None = None):
        self.servers = servers
        self.users = {u.lower() for u in users or []}
        self.sessions: list[tuple[object, Session]] = []
        self.current: tuple[object, Session] | None = None
        self._failed_at: dict[int, float] = {}
        self._art_key = None
        self._art_failed = (None, 0.0)
        self._art_id = 0
        self._art_frame = None

    def poll(self, now: float | None = None):
        """One round of requests (blocking: run it in a thread)."""
        fixed = now
        now = now if now is not None else time.monotonic()
        found = []
        for i, srv in enumerate(self.servers):
            if now - self._failed_at.get(i, -BACKOFF_S) < BACKOFF_S:
                found += [(sv, s) for sv, s in self.sessions if sv is srv]
                continue
            try:
                got = srv.sessions()
                # Positions are as of the reply, not the start of the round.
                seen = fixed if fixed is not None else time.monotonic()
                for s in got:
                    s.seen = seen
                    if not self.users or s.user.lower() in self.users:
                        found.append((srv, s))
                self._failed_at.pop(i, None)
            except Exception as e:
                log.info("%s at %s: %s", srv.name, srv.url, e)
                self._failed_at[i] = now
        self.sessions = found
        self.current = self._pick()

    def _pick(self):
        if not self.sessions:
            return None
        if self.current:
            srv, cur = self.current
            for pair in self.sessions:
                if pair[0] is srv and pair[1].id == cur.id and (pair[1].playing or
                                                                 not any(s.playing for _, s in self.sessions)):
                    return pair
        playing = [p for p in self.sessions if p[1].playing]
        return (playing or self.sessions)[0]

    def interval(self) -> float:
        return POLL_ACTIVE_S if self.sessions else POLL_IDLE_S

    def art(self) -> bytes | None:
        """A new ART1 frame when the shown cover changed (blocking)."""
        if not self.current:
            return None
        srv, s = self.current
        key = (id(srv), s.art_key)
        if key == self._art_key:
            return None
        failed_key, failed_at = self._art_failed
        if key == failed_key and time.monotonic() - failed_at < ART_RETRY_S:
            return None
        self._art_key = key
        self._art_id, self._art_frame = 0, None
        if not s.art_key:
            return None
        try:
            raw = srv.cover(s)
            art_id = (zlib.crc32(raw) & 0x7FFFFFFF) or 1
            self._art_frame = to_poster_frame(art_id, raw)
            self._art_id = art_id
        except Exception as e:
            log.info("could not fetch cover from %s: %s", srv.name, e)
            # Try again later instead of going without a cover for the whole show.
            self._art_key = None
            self._art_failed = (key, time.monotonic())
        return self._art_frame

    def current_art(self) -> bytes | None:
        return self._art_frame if self.current else None

    def message(self, now: float | None = None) -> dict:
        if not self.current:
            return {"type": "media", "active": False}
        srv, s = self.current
        who = " - ".join(x for x in (s.device, s.user) if x)
        others = len(self.sessions) - 1
        if others > 0:
            who = f"{who} (+{others} more)" if who else f"+{others} more"
        return {
            "type": "media",
            "active": True,
            "playing": s.playing,
            "title": ascii_fold(s.title, 120),
            "artist": ascii_fold(s.subtitle, 90),
            "album": ascii_fold(who, 90),
            "source": ascii_fold(srv.name + (" - transcoding" if s.transcoding else ""), 31),
            "pos": int(s.position(now)),
            "dur": int(s.dur),
            "can_seek": s.can_seek and s.dur > 0,
            "controls": s.can_control,
            "art_id": self._art_id,
        }

    def command(self, cmd: str, value: int = 0):
        """Run a board button on the shown session (blocking)."""
        if not self.current:
            return
        srv, s = self.current
        try:
            srv.command(s, cmd, value)
        except Exception as e:
            log.warning("%s %s failed: %s", srv.name, cmd, e)
            return
        # Show the change at once instead of on the next poll.
        if cmd == "toggle" and s.can_control:
            s.pos, s.seen, s.playing = s.position(), time.monotonic(), not s.playing
        elif cmd == "seek" and s.can_seek:
            s.pos, s.seen = float(value), time.monotonic()
