"""Now playing, from Windows' media sessions (Spotify, browsers/YouTube, etc.)."""
import datetime as dt
import io
import logging
import struct
import zlib

from PIL import Image

from .text import ascii_fold

log = logging.getLogger(__name__)

ART_SIZE = 280

try:
    from winrt.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager as SessionManager,
        GlobalSystemMediaTransportControlsSessionPlaybackStatus as PlaybackStatus,
    )
    from winrt.windows.storage.streams import Buffer, InputStreamOptions
except ImportError:  # not on Windows
    SessionManager = None

_SOURCES = {
    "spotify": "Spotify",
    "chrome": "Chrome",
    "msedge": "Edge",
    "firefox": "Firefox",
    "308046b0af4a39cb": "Firefox",
    "opera": "Opera",
    "brave": "Brave",
    "vlc": "VLC",
    "applemusic": "Apple Music",
    "zunemusic": "Media Player",
    "tidal": "TIDAL",
    "discord": "Discord",
}


def _source_name(app_id: str) -> str:
    low = (app_id or "").lower()
    for key, name in _SOURCES.items():
        if key in low:
            return name
    base = low.split("!")[-1].split("\\")[-1].removesuffix(".exe")
    return ascii_fold(base.title(), 24) or "Media"


def art_frame(art_id: int, im: Image.Image) -> bytes:
    """Pack an ART_SIZE square RGB image as the hub's ART1 frame (RGB565, little endian)."""
    px = im.tobytes()
    out = bytearray(ART_SIZE * ART_SIZE * 2)
    j = 0
    for i in range(0, len(px), 3):
        v = ((px[i] & 0xF8) << 8) | ((px[i + 1] & 0xFC) << 3) | (px[i + 2] >> 3)
        out[j] = v & 0xFF
        out[j + 1] = v >> 8
        j += 2
    return b"ART1" + struct.pack("<IHH", art_id, ART_SIZE, ART_SIZE) + bytes(out)


def to_rgb565_frame(art_id: int, raw: bytes) -> bytes:
    """Center-crop to a square, resize, and pack as the hub's ART1 frame."""
    im = Image.open(io.BytesIO(raw)).convert("RGB")
    w, h = im.size
    side = min(w, h)
    im = im.crop(((w - side) // 2, (h - side) // 2, (w + side) // 2, (h + side) // 2))
    return art_frame(art_id, im.resize((ART_SIZE, ART_SIZE), Image.LANCZOS))


class Media:
    def __init__(self):
        self._mgr = None
        self._thumb_key = None
        self._art_id = 0
        self._art_frame = None

    async def start(self):
        if SessionManager is None:
            log.warning("media sessions need Windows")
            return
        self._mgr = await SessionManager.request_async()

    def _session(self):
        return self._mgr.get_current_session() if self._mgr else None

    async def snapshot(self) -> tuple[dict, bytes | None]:
        """Returns the media message, plus a new ART1 frame when the cover changed."""
        s = self._session()
        if s is None:
            return {"type": "media", "active": False}, None
        try:
            props = await s.try_get_media_properties_async()
        except OSError as e:
            log.debug("media properties failed: %s", e)
            return {"type": "media", "active": False}, None
        info = s.get_playback_info()
        tl = s.get_timeline_properties()
        playing = info.playback_status == PlaybackStatus.PLAYING

        dur = tl.end_time.total_seconds() if tl.end_time else 0
        pos = tl.position.total_seconds() if tl.position else 0
        # Players only update the timeline occasionally; extrapolate while playing.
        if playing and tl.last_updated_time:
            age = (dt.datetime.now(dt.timezone.utc) - tl.last_updated_time).total_seconds()
            if 0 < age < 24 * 3600:
                pos += age
        if dur > 0:
            pos = min(pos, dur)

        new_frame = None
        key = (props.title, props.artist, props.album_title)
        if key != self._thumb_key:
            self._thumb_key = key
            self._art_id, self._art_frame = 0, None
            if props.thumbnail:
                try:
                    raw = await self._read_thumbnail(props.thumbnail)
                    art_id = (zlib.crc32(raw) & 0x7FFFFFFF) or 1
                    self._art_frame = to_rgb565_frame(art_id, raw)
                    self._art_id = art_id
                    new_frame = self._art_frame
                except Exception as e:
                    log.info("could not read cover art: %s", e)

        controls = info.controls
        msg = {
            "type": "media",
            "active": bool(props.title),
            "playing": playing,
            "title": ascii_fold(props.title, 120),
            "artist": ascii_fold(props.artist or props.album_artist, 90),
            "album": ascii_fold(props.album_title, 90),
            "source": _source_name(s.source_app_user_model_id),
            "pos": int(pos),
            "dur": int(dur),
            "can_seek": bool(controls and controls.is_playback_position_enabled and dur > 0),
            "art_id": self._art_id,
        }
        return msg, new_frame

    def current_art(self) -> bytes | None:
        return self._art_frame

    @staticmethod
    async def _read_thumbnail(ref) -> bytes:
        stream = await ref.open_read_async()
        size = stream.size
        buf = Buffer(size)
        await stream.read_async(buf, size, InputStreamOptions.READ_AHEAD)
        return bytes(memoryview(buf)[: buf.length])

    async def command(self, cmd: str, value: int = 0):
        s = self._session()
        if s is None:
            return
        if cmd == "toggle":
            await s.try_toggle_play_pause_async()
        elif cmd == "next":
            await s.try_skip_next_async()
        elif cmd == "prev":
            await s.try_skip_previous_async()
        elif cmd == "seek":
            # Position is in 100 ns ticks.
            await s.try_change_playback_position_async(int(value) * 10_000_000)
