"""Turns a Telegram filename/caption into a Jellyfin library path.

Resolution order: caption tag (#movie / #tv) > guessit on the filename > guessit on
the caption. TMDB canonicalisation happens afterwards in tmdb.py.
"""

import re
from dataclasses import dataclass, field, replace
from pathlib import Path

from guessit import guessit

VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".wmv", ".ts", ".webm"}
SUBTITLE_EXTS = {".srt", ".ass", ".ssa", ".sub", ".vtt"}

_MOVIE_TAG = re.compile(r"^#movie\s+(?P<title>.+?)\s*\(?(?P<year>(?:19|20)\d{2})?\)?\s*$", re.I)
_TV_TAG = re.compile(
    r"^#tv\s+(?P<title>.+?)\s+S(?P<season>\d{1,2})\s*E(?P<ep>\d{1,3})(?:\s*-?\s*E?(?P<ep2>\d{1,3}))?\s*$",
    re.I,
)
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
# Phone/camera/messenger names: guessit reads `IMG_1126` as S11E26, `PXL_2024…` as a movie
_DEVICE_NAME = re.compile(
    r"^(IMG|VID|PXL|DSCN?|MVI|MOV|GOPR|GH\d{2}|DJI|SAM|WP|WhatsApp Video|Screen ?Recording|signal|video)(?=[\s_-]|\d)",
    re.I,
)


@dataclass(frozen=True)
class MediaInfo:
    kind: str  # "movie" | "episode"
    title: str
    year: int | None = None
    season: int | None = None
    episodes: tuple[int, ...] = field(default_factory=tuple)
    tmdb_id: int | None = None
    subtitle_lang: str | None = None

    @property
    def label(self) -> str:
        """Human-readable name for status messages."""
        if self.kind == "movie":
            return f"{self.title} ({self.year})" if self.year else self.title
        return f"{self.title} {_episode_code(self.season, self.episodes)}"


def parse_caption(caption: str | None) -> MediaInfo | None:
    """Explicit override: a caption line like `#movie Inception 2010` or `#tv Dark S01E02`."""
    for line in (caption or "").splitlines():
        line = line.strip()
        if m := _TV_TAG.match(line):
            eps = (int(m["ep"]),) + ((int(m["ep2"]),) if m["ep2"] else ())
            return MediaInfo("episode", m["title"].strip(), season=int(m["season"]), episodes=eps)
        if m := _MOVIE_TAG.match(line):
            return MediaInfo("movie", m["title"].strip(), year=int(m["year"]) if m["year"] else None)
    return None


def guess(name: str) -> MediaInfo | None:
    """Best-effort parse of a release-style name; None if it can't be identified."""
    if _DEVICE_NAME.match(name):
        return None
    g = guessit(name)
    title = g.get("title")
    if not title or isinstance(title, list):
        return None
    year = g.get("year") if isinstance(g.get("year"), int) else None
    lang = g.get("subtitle_language")
    lang = str(lang.alpha3) if lang is not None and not isinstance(lang, list) else None

    if g.get("type") == "episode":
        season, episode = g.get("season"), g.get("episode")
        if isinstance(season, list):
            return None  # season packs in one file aren't a thing we can place
        if episode is None:
            return None
        if season is None:
            season = 1  # anime-style absolute numbering; Jellyfin copes with Season 01
        eps = tuple(episode) if isinstance(episode, list) else (episode,)
        return MediaInfo("episode", title, year=year, season=season, episodes=eps, subtitle_lang=lang)
    # guessit splits "Dune Part Two" into title + part; they belong together for movies
    if isinstance(g.get("part"), int):
        title += f" Part {g['part']}"
    elif isinstance(g.get("volume"), int):
        title += f" Vol {g['volume']}"
    return MediaInfo("movie", title, year=year, subtitle_lang=lang)


def resolve(filename: str | None, caption: str | None) -> MediaInfo | None:
    if info := parse_caption(caption):
        if filename and (lang := _subtitle_lang(filename)):
            info = replace(info, subtitle_lang=lang)
        return info
    if filename and (info := guess(filename)) and _confident(info):
        return info
    # Compressed "video" messages have no filename; the caption is all we have
    first_line = (caption or "").strip().splitlines()[0:1]
    if first_line and (info := guess(first_line[0])) and _confident(info):
        return info
    return None


def _confident(info: MediaInfo) -> bool:
    """guessit calls any unparseable name a movie; without a year it's probably junk like `random123`."""
    return info.kind != "movie" or info.year is not None


def build_path(info: MediaInfo, ext: str, movies_dir: Path, tv_dir: Path) -> Path:
    """Jellyfin-convention path, e.g. `Inception (2010) [tmdbid-27205]/Inception (2010).mkv`."""
    title = sanitize(info.title)
    base = f"{title} ({info.year})" if info.year else title
    folder = f"{base} [tmdbid-{info.tmdb_id}]" if info.tmdb_id else base
    lang = f".{info.subtitle_lang}" if info.subtitle_lang and ext in SUBTITLE_EXTS else ""

    if info.kind == "movie":
        return movies_dir / folder / f"{base}{lang}{ext}"
    code = _episode_code(info.season, info.episodes)
    return tv_dir / folder / f"Season {info.season:02d}" / f"{title} {code}{lang}{ext}"


def sanitize(name: str) -> str:
    name = _UNSAFE.sub("", name).strip(" .")
    return re.sub(r"\s+", " ", name) or "Unknown"


def is_supported(filename: str | None, mime: str | None) -> bool:
    ext = Path(filename).suffix.lower() if filename else ""
    return ext in VIDEO_EXTS or ext in SUBTITLE_EXTS or (mime or "").startswith("video/")


def _subtitle_lang(filename: str) -> str | None:
    if Path(filename).suffix.lower() not in SUBTITLE_EXTS:
        return None
    g = guess(filename)
    return g.subtitle_lang if g else None


def _episode_code(season: int | None, episodes: tuple[int, ...]) -> str:
    code = f"S{season or 0:02d}E{episodes[0]:02d}"
    if len(episodes) > 1:
        code += f"-E{episodes[-1]:02d}"
    return code
