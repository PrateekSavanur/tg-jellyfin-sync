"""Turns an uploaded audio file into a Jellyfin music library path.

Resolution order per field: the file's own tags > Telegram's audio attributes (title/performer)
> the filename (`Artist - Title.mp3`). Jellyfin reads the tags itself; the folder layout only
matters for untagged files and for browsing the disk.
"""

import logging
import re
from dataclasses import dataclass
from pathlib import Path

import mutagen

from .naming import sanitize

log = logging.getLogger(__name__)

AUDIO_EXTS = {".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".wma", ".alac", ".aiff"}

# "01 - Title", "01. Title", "1 Title" — track numbers people leave in filenames
_TRACK_PREFIX = re.compile(r"^\s*(?P<n>\d{1,3})\s*(?:[-._)]\s*|\s+)(?=\D)")


@dataclass(frozen=True)
class SongInfo:
    title: str
    artist: str | None = None
    album: str | None = None
    track: int | None = None

    @property
    def label(self) -> str:
        """Human-readable name for status messages."""
        return f"{self.artist} – {self.title}" if self.artist else self.title


def is_audio(filename: str | None, mime: str | None) -> bool:
    ext = Path(filename).suffix.lower() if filename else ""
    return ext in AUDIO_EXTS or (mime or "").startswith("audio/")


def identify(path: Path, filename: str | None, tg_title: str | None = None, tg_performer: str | None = None) -> SongInfo:
    tags = read_tags(path)
    stem = Path(filename).stem if filename else ""
    name_artist, name_title, name_track = _parse_filename(stem)

    title = tags.get("title") or tg_title or name_title or stem or "Unknown Title"
    artist = tags.get("albumartist") or tags.get("artist") or tg_performer or name_artist
    track = _number(tags.get("tracknumber")) or name_track
    return SongInfo(title=title, artist=artist, album=tags.get("album"), track=track)


def read_tags(path: Path) -> dict[str, str]:
    """First value of each common tag, via mutagen's format-independent "easy" interface."""
    try:
        audio = mutagen.File(path, easy=True)
    except Exception as e:  # mutagen raises format-specific errors for corrupt/odd files
        log.warning("Could not read tags of %s: %s", path, e)
        return {}
    if audio is None or not audio.tags:
        return {}
    out = {}
    for key in ("title", "artist", "albumartist", "album", "tracknumber"):
        values = audio.tags.get(key)
        if values and (value := str(values[0]).strip()):
            out[key] = value
    return out


def build_path(info: SongInfo, ext: str, music_dir: Path) -> Path:
    """`Artist/Album/03 - Title.mp3`; songs without an album go in `Artist/Singles/`."""
    artist = sanitize(info.artist or "Unknown Artist")
    album = sanitize(info.album) if info.album else "Singles"
    title = sanitize(info.title)
    name = f"{info.track:02d} - {title}" if info.track and info.album else title
    return music_dir / artist / album / f"{name}{ext}"


def _parse_filename(stem: str) -> tuple[str | None, str | None, int | None]:
    track = None
    if m := _TRACK_PREFIX.match(stem):
        track, stem = int(m["n"]), stem[m.end():]
    stem = stem.replace("_", " ").strip()
    if " - " in stem:
        artist, title = (part.strip() for part in stem.split(" - ", 1))
        if artist and title:
            return artist, title, track
    return None, stem or None, track


def _number(value: str | None) -> int | None:
    """Tag values like `3` or `3/12`."""
    m = re.match(r"\s*(\d+)", value or "")
    return int(m[1]) if m and int(m[1]) > 0 else None
