from pathlib import Path

import pytest

from app.naming import MediaInfo, build_path, is_supported, parse_caption, resolve

M, T = Path("/data/movies"), Path("/data/tvshows")


def path_for(filename, caption=None, tmdb_id=None):
    info = resolve(filename, caption)
    assert info is not None, f"could not identify {filename!r} / {caption!r}"
    if tmdb_id:
        info = MediaInfo(**{**info.__dict__, "tmdb_id": tmdb_id})
    ext = Path(filename).suffix.lower() if filename else ".mp4"
    return build_path(info, ext, M, T)


@pytest.mark.parametrize(
    "filename, expected",
    [
        ("Inception.2010.1080p.BluRay.x264-SPARKS.mkv", "/data/movies/Inception (2010)/Inception (2010).mkv"),
        ("The.Dark.Knight.2008.2160p.UHD.BluRay.x265.mkv", "/data/movies/The Dark Knight (2008)/The Dark Knight (2008).mkv"),
        # TMDB later canonicalises "Dune Part 2" → "Dune: Part Two"
        ("Dune Part Two (2024) [1080p] [WEBRip] [5.1] [YTS.MX].mp4", "/data/movies/Dune Part 2 (2024)/Dune Part 2 (2024).mp4"),
        ("Harry.Potter.and.the.Deathly.Hallows.Part.1.2010.mkv",
         "/data/movies/Harry Potter and the Deathly Hallows Part 1 (2010)/Harry Potter and the Deathly Hallows Part 1 (2010).mkv"),
        ("Kill Bill Vol 2 2004.mkv", "/data/movies/Kill Bill Vol 2 (2004)/Kill Bill Vol 2 (2004).mkv"),
        ("[TamilBlasters] Jailer (2023) Tamil HQ HDRip - 720p - x264.mkv", "/data/movies/Jailer (2023)/Jailer (2023).mkv"),
        ("Interstellar 2014 IMAX 1080p WEB-DL DDP5.1 H.264.mkv", "/data/movies/Interstellar (2014)/Interstellar (2014).mkv"),
        ("Spider-Man.Across.the.Spider-Verse.2023.1080p.WEB-DL.mkv",
         "/data/movies/Spider-Man Across the Spider-Verse (2023)/Spider-Man Across the Spider-Verse (2023).mkv"),
    ],
)
def test_movies(filename, expected):
    assert str(path_for(filename)) == expected


@pytest.mark.parametrize(
    "filename, expected",
    [
        ("Breaking.Bad.S01E02.720p.BluRay.x264.mkv",
         "/data/tvshows/Breaking Bad/Season 01/Breaking Bad S01E02.mkv"),
        ("The.Office.US.S03E10.1080p.WEB-DL.mkv",
         "/data/tvshows/The Office/Season 03/The Office S03E10.mkv"),
        ("Dark.S02E01-E02.1080p.NF.WEB-DL.mkv",
         "/data/tvshows/Dark/Season 02/Dark S02E01-E02.mkv"),
        ("Severance 1x05 The Grim Barbarity.mkv",
         "/data/tvshows/Severance/Season 01/Severance S01E05.mkv"),
        ("[SubsPlease] Frieren - 12 (1080p) [ABCD1234].mkv",
         "/data/tvshows/Frieren/Season 01/Frieren S01E12.mkv"),
    ],
)
def test_episodes(filename, expected):
    assert str(path_for(filename)) == expected


def test_tmdb_id_goes_in_folder_only():
    p = path_for("Inception.2010.1080p.mkv", tmdb_id=27205)
    assert str(p) == "/data/movies/Inception (2010) [tmdbid-27205]/Inception (2010).mkv"


def test_episode_with_year_and_tmdb():
    info = MediaInfo("episode", "Doctor Who", year=2005, season=1, episodes=(3,), tmdb_id=57243)
    assert str(build_path(info, ".mkv", M, T)) == (
        "/data/tvshows/Doctor Who (2005) [tmdbid-57243]/Season 01/Doctor Who S01E03.mkv"
    )


def test_subtitle_keeps_language():
    assert str(path_for("Inception.2010.1080p.en.srt")) == "/data/movies/Inception (2010)/Inception (2010).eng.srt"


@pytest.mark.parametrize(
    "caption, expected",
    [
        ("#movie Inception 2010", MediaInfo("movie", "Inception", year=2010)),
        ("#movie Inception (2010)", MediaInfo("movie", "Inception", year=2010)),
        ("#MOVIE Amélie", MediaInfo("movie", "Amélie")),
        ("#tv Breaking Bad S01E02", MediaInfo("episode", "Breaking Bad", season=1, episodes=(2,))),
        ("#tv Dark S2E1-E2", MediaInfo("episode", "Dark", season=2, episodes=(1, 2))),
        ("great movie!\n#movie Heat 1995", MediaInfo("movie", "Heat", year=1995)),
    ],
)
def test_caption_tags(caption, expected):
    assert parse_caption(caption) == expected


def test_caption_overrides_filename():
    p = path_for("random_upload_final_v2.mkv", caption="#movie Heat 1995")
    assert str(p) == "/data/movies/Heat (1995)/Heat (1995).mkv"


def test_compressed_video_uses_caption_text():
    # Telegram "video" messages carry no filename
    p = path_for(None, caption="Oppenheimer 2023 1080p")
    assert str(p) == "/data/movies/Oppenheimer (2023)/Oppenheimer (2023).mp4"


def test_unidentifiable_returns_none():
    assert resolve(None, None) is None
    assert resolve(None, "") is None


@pytest.mark.parametrize("filename, caption", [("random123.mp4", None), ("Inception.1080p.mkv", None), (None, "lol")])
def test_yearless_movie_guess_is_unidentified(filename, caption):
    assert resolve(filename, caption) is None


def test_yearless_movie_tag_still_works():
    p = path_for("Amelie.en.srt", caption="#movie Amélie")
    assert str(p) == "/data/movies/Amélie/Amélie.eng.srt"


@pytest.mark.parametrize(
    "filename",
    ["IMG_1126.MP4", "VID_20240512_183000.mp4", "PXL_20240101_123456789.mp4", "DSC_0042.MOV",
     "MVI_1234.MOV", "GOPR0042.MP4", "WhatsApp Video 2024-05-01 at 10.22.33.mp4"],
)
def test_device_filenames_are_unidentified(filename):
    assert resolve(filename, None) is None


def test_device_filename_with_caption_tag():
    p = path_for("IMG_1126.MP4", caption="#tv Dark S01E02")
    assert str(p) == "/data/tvshows/Dark/Season 01/Dark S01E02.mp4"


def test_unsafe_characters_stripped():
    info = MediaInfo("movie", 'Mission: Impossible / "Fallout"', year=2018)
    assert str(build_path(info, ".mkv", M, T)) == (
        "/data/movies/Mission Impossible Fallout (2018)/Mission Impossible Fallout (2018).mkv"
    )


@pytest.mark.parametrize(
    "name, mime, ok",
    [
        ("a.mkv", None, True),
        ("a.SRT", None, True),
        (None, "video/mp4", True),
        ("a.zip", "application/zip", False),
        ("a.jpg", "image/jpeg", False),
    ],
)
def test_is_supported(name, mime, ok):
    assert is_supported(name, mime) is ok
