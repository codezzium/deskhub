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

from .media import art_frame
from .text import ascii_fold

log = logging.getLogger(__name__)

KEYRING_SERVICE = "deskhub-media"
PRODUCT = "Desk Hub"
POLL_ACTIVE_S = 3
POLL_IDLE_S = 15
BACKOFF_S = 60
ART_RETRY_S = 30
INFO_S = 10 * 60      # version and library sizes change slowly
POSTER_W, POSTER_H = 128, 192   # the board's server cover, 2:3 like a poster
TICKS = 10_000_000   # Jellyfin times are in 100 ns ticks
SERVER_ART_MAGIC = b"ARS1"
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

def _plex_get(url: str, token: str, path: str, params: dict | None = None, raw: bool = False,
              tls_name: str = ""):
    """A request to the server. With tls_name, connect by IP but check the certificate
    against the server's own *.plex.direct name (Plex servers set to require secure
    connections refuse plain HTTP, and their certificate never names the IP)."""
    import httpx

    with httpx.Client(timeout=8) as c:
        req = c.build_request("GET", url.rstrip("/") + path, params=params,
                              headers={"X-Plex-Token": token, "Accept": "application/json",
                                       "X-Plex-Product": PRODUCT, "X-Plex-Client-Identifier": client_id()},
                              extensions={"sni_hostname": tls_name} if tls_name else None)
        r = c.send(req)
    r.raise_for_status()
    return r.content if raw else r.json()


def plex_identity(url: str, get=None) -> tuple[str, str]:
    """(machine id, scheme) of the Plex server at url. /identity needs no token, so
    trying HTTPS without checking the certificate sends nothing secret."""
    import httpx
    from urllib.parse import urlsplit

    get = get or httpx.get
    host = urlsplit(url).netloc
    last = None
    for scheme in ("http", "https"):
        try:
            r = get(f"{scheme}://{host}/identity", timeout=5, verify=scheme == "http",
                    headers={"Accept": "application/json"})
            r.raise_for_status()
            mc = r.json().get("MediaContainer") or {}
            return mc.get("machineIdentifier", ""), scheme
        except Exception as e:
            last = e
    raise last


def plex_connection(url: str, token: str, get=None, identity=plex_identity) -> dict:
    """Where and how to reach a Plex server from this PC: {"url", "tls_name", "token"}.

    Asks plex.tv for the server's local HTTPS address (its *.plex.direct name, which the
    certificate is made for) and the server's own access token. Without that, plain
    HTTP if the server takes it.
    """
    import httpx
    from urllib.parse import urlsplit

    get = get or httpx.get
    machine, scheme = identity(url)
    host = urlsplit(url).hostname
    port = urlsplit(url).port or PLEX_PORT
    try:
        r = get(f"{PLEX_TV}/resources", params={"includeHttps": 1, "includeRelay": 0}, timeout=15,
                headers={"Accept": "application/json", "X-Plex-Token": token, "X-Plex-Product": PRODUCT,
                         "X-Plex-Client-Identifier": client_id()})
        r.raise_for_status()
        resources = r.json()
    except Exception as e:
        log.info("plex.tv resources: %s", e)
        resources = []
    for res in resources:
        if res.get("clientIdentifier") != machine:
            continue
        for c in res.get("connections") or []:
            if c.get("protocol") == "https" and c.get("address") == host:
                name = urlsplit(c.get("uri", "")).hostname or ""
                if name.endswith(".plex.direct"):
                    return {"url": f"https://{host}:{c.get('port') or port}", "tls_name": name,
                            "token": res.get("accessToken") or token}
    if scheme == "http":
        return {"url": f"http://{host}:{port}", "tls_name": "", "token": token}
    raise RuntimeError("the server only takes secure connections, and plex.tv didn't list its "
                       "secure address for this account")


class PlexServer:
    kind = "plex"

    def __init__(self, url: str, token: str, name: str = "Plex", tls_name: str = "", get=_plex_get):
        self.url, self.token, self.name, self._get = url.rstrip("/"), token, name, get
        self.tls_name = tls_name

    def sessions(self) -> list[Session]:
        data = self._get(self.url, self.token, "/status/sessions", tls_name=self.tls_name)
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
                         {"url": s.art_key, "width": POSTER_W * 2, "height": POSTER_H * 2, "minSize": 1},
                         raw=True, tls_name=self.tls_name)

    def command(self, s: Session, cmd: str, value: int = 0):
        pass  # display-only, see the module docstring

    def info(self) -> dict:
        """Version and library sizes, for the board while nothing plays."""
        root = (self._get(self.url, self.token, "/", tls_name=self.tls_name).get("MediaContainer") or {})
        counts = {}
        sections = (self._get(self.url, self.token, "/library/sections", tls_name=self.tls_name)
                    .get("MediaContainer") or {}).get("Directory") or []
        # Plex item types: 1 movie, 2 show, 4 episode, 8 artist, 10 track.
        wanted = {"movie": [(1, "movies")], "show": [(2, "shows"), (4, "episodes")], "artist": [(10, "songs")]}
        for sec in sections:
            for kind, label in wanted.get(sec.get("type"), []):
                mc = self._get(self.url, self.token, f"/library/sections/{sec.get('key')}/all",
                               {"type": kind, "X-Plex-Container-Start": 0, "X-Plex-Container-Size": 0},
                               tls_name=self.tls_name).get("MediaContainer") or {}
                counts[label] = counts.get(label, 0) + int(mc.get("totalSize", mc.get("size", 0)) or 0)
        return {"product": "Plex Media Server", "version": ".".join(str(root.get("version", "")).split("-")[0].split(".")[:3]),
                "counts": counts}


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
                         {"maxWidth": POSTER_W * 2, "maxHeight": POSTER_H * 2, "quality": 90}, raw=True)

    def info(self) -> dict:
        """Version and library sizes, for the board while nothing plays."""
        sysinfo = self._req("GET", self.url, self.key, "/System/Info") or {}
        c = self._req("GET", self.url, self.key, "/Items/Counts") or {}
        counts = {label: int(c.get(key) or 0) for key, label in
                  (("MovieCount", "movies"), ("SeriesCount", "shows"), ("EpisodeCount", "episodes"),
                   ("SongCount", "songs")) if c.get(key)}
        return {"product": "Jellyfin", "version": str(sysinfo.get("Version", "")), "counts": counts}

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
    """Fit a cover inside a 2:3 poster on black (square album art gets bands), as an
    ARS1 frame: the server's own cover slot, so it doesn't replace the PC's."""
    im = Image.open(io.BytesIO(raw)).convert("RGB")
    im.thumbnail((POSTER_W, POSTER_H), Image.LANCZOS)
    canvas = Image.new("RGB", (POSTER_W, POSTER_H), (0, 0, 0))
    canvas.paste(im, ((POSTER_W - im.width) // 2, (POSTER_H - im.height) // 2))
    return art_frame(art_id, canvas, SERVER_ART_MAGIC)


def plural(n: int, word: str) -> str:
    return f"{n:,} {word[:-1] if n == 1 else word}"


def describe(info: dict | None) -> str:
    """'312 movies, 45 shows, 2,104 episodes' from a server's info()."""
    counts = (info or {}).get("counts") or {}
    return ", ".join(plural(counts[k], k) for k in ("movies", "shows", "episodes", "songs") if counts.get(k))


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
        servers.append(PlexServer(url, secret, name, tls_name=str(e.get("tls_name", ""))) if kind == "plex"
                       else JellyfinServer(url, secret, name))
    return servers


class MediaServers:
    """Polls every server and picks one session to show (playing first, sticky)."""

    def __init__(self, servers: list, users: list[str] | None = None):
        self.servers = servers
        self.users = {u.lower() for u in users or []}
        self.sessions: list[tuple[object, Session]] = []
        self.current: tuple[object, Session] | None = None
        self._failed_at: dict[int, float] = {}
        self.infos: dict[int, dict] = {}
        self._info_at: dict[int, float] = {}
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
                continue
            if now - self._info_at.get(i, -INFO_S) >= INFO_S:
                self._info_at[i] = now
                try:
                    self.infos[i] = srv.info()
                except Exception as e:
                    log.info("%s info: %s", srv.name, e)
        self.sessions = found
        self.current = self._pick()

    def online(self, i: int) -> bool:
        return i not in self._failed_at

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
        """A new ARS1 frame when the shown cover changed (blocking)."""
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
        """The board's server section: the shown session, or the server itself while idle."""
        if self.current:
            srv = self.current[0]
        else:
            # Idle: the first server that answers (or the first at all).
            srv = next((s for i, s in enumerate(self.servers) if self.online(i)), self.servers[0]) \
                if self.servers else None
        i = self.servers.index(srv) if srv in self.servers else -1
        info = self.infos.get(i) or {}
        product = info.get("product") or ("Plex Media Server" if getattr(srv, "kind", "") == "plex" else "Jellyfin")
        version = f"{product} {info['version']}" if info.get("version") else product
        msg = {
            "type": "media_server",
            "configured": bool(self.servers),
            "name": ascii_fold(srv.name if srv else "", 31),
            "kind": getattr(srv, "kind", ""),
            "online": self.online(i) if i >= 0 else False,
            "version": ascii_fold(version, 47),
            "library": ascii_fold(describe(info), 95),
            "streams": len(self.sessions),
            "transcoding": sum(1 for _, s in self.sessions if s.transcoding),
            "active": False,
        }
        if not self.current:
            return msg
        s = self.current[1]
        who = " - ".join(x for x in (s.device, s.user) if x)
        others = len(self.sessions) - 1
        if others > 0:
            who = f"{who} (+{others} more)" if who else f"+{others} more"
        msg.update({
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
        })
        return msg

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
