from pathlib import Path

import pytest
from mutagen.easyid3 import EasyID3

from app.music import SongInfo, build_path, identify, is_audio

MUSIC = Path("/data/music")
# MPEG-1 Layer III, 128 kbps, 44.1 kHz — enough frames for mutagen to recognise an MP3
MP3_FRAMES = (b"\xff\xfb\x90\x64" + b"\x00" * 413) * 4


def write_mp3(path: Path, **tags) -> Path:
    path.write_bytes(MP3_FRAMES)
    if tags:
        id3 = EasyID3()
        for key, value in tags.items():
            id3[key] = value
        id3.save(path)
    return path


def test_tags_win_over_filename(tmp_path):
    src = write_mp3(tmp_path / "1.part", title="Kajra Mohabbat Wala", artist="Asha Bhosle",
                    album="Kismet", tracknumber="3/8")
    info = identify(src, "random upload name.mp3", tg_title="Other", tg_performer="Someone")
    assert info == SongInfo("Kajra Mohabbat Wala", ("Asha Bhosle",), "Kismet", 3)
    assert build_path(info, ".mp3", MUSIC) == MUSIC / "Asha Bhosle" / "Kismet" / "03 - Kajra Mohabbat Wala.mp3"
    assert info.label == "Asha Bhosle – Kajra Mohabbat Wala"


def test_album_artist_groups_compilations(tmp_path):
    src = write_mp3(tmp_path / "1.part", title="Song", artist="Guest Singer", albumartist="R. D. Burman",
                    album="Sholay")
    assert identify(src, "x.mp3").artist == "R. D. Burman"


@pytest.mark.parametrize("artist", ["K. J. Yesudas/ Hemlata", "K. J. Yesudas/Hemlata", "K. J. Yesudas; Hemlata"])
def test_joined_artists_filed_under_the_first(tmp_path, artist):
    src = write_mp3(tmp_path / "1.part", title="Jab Deep Jale Aana", artist=artist, album="Chitchor", tracknumber="1")
    info = identify(src, "x.mp3")
    assert build_path(info, ".mp3", MUSIC) == MUSIC / "K. J. Yesudas" / "Chitchor" / "01 - Jab Deep Jale Aana.mp3"
    assert info.label == "K. J. Yesudas, Hemlata – Jab Deep Jale Aana"


def test_multi_value_artist_tag(tmp_path):
    src = write_mp3(tmp_path / "1.part", title="Song", artist=["Lata Mangeshkar", "Mukesh"], album="Album")
    info = identify(src, "x.mp3")
    assert info.artist == "Lata Mangeshkar"
    assert info.label == "Lata Mangeshkar, Mukesh – Song"


def test_hyphenated_artist_names_kept_whole(tmp_path):
    src = write_mp3(tmp_path / "1.part", title="Song", albumartist="Kalyanji - Anandji", album="Saraswatichandra")
    assert identify(src, "x.mp3").artist == "Kalyanji - Anandji"


def test_telegram_metadata_when_untagged(tmp_path):
    src = write_mp3(tmp_path / "1.part")
    info = identify(src, "AUD-20261024.mp3", tg_title="Mere Sapnon Ki Rani", tg_performer="Kishore Kumar")
    assert build_path(info, ".mp3", MUSIC) == MUSIC / "Kishore Kumar" / "Singles" / "Mere Sapnon Ki Rani.mp3"


@pytest.mark.parametrize(
    "filename, expected",
    [
        ("Kishore Kumar - Mere Sapnon Ki Rani.mp3", "Kishore Kumar/Singles/Mere Sapnon Ki Rani.mp3"),
        ("05 - Lata Mangeshkar - Lag Jaa Gale.mp3", "Lata Mangeshkar/Singles/Lag Jaa Gale.mp3"),
        ("Lag_Jaa_Gale.mp3", "Unknown Artist/Singles/Lag Jaa Gale.mp3"),
        ("Who: Are <You>?.mp3", "Unknown Artist/Singles/Who Are You.mp3"),  # Telegram names are untrusted
    ],
)
def test_filename_fallback(tmp_path, filename, expected):
    src = write_mp3(tmp_path / "1.part")
    info = identify(src, filename)
    assert build_path(info, ".mp3", MUSIC) == MUSIC / expected


def test_unreadable_file_still_gets_a_path(tmp_path):
    src = tmp_path / "1.part"
    src.write_bytes(b"not audio at all")
    info = identify(src, "Artist - Title.flac")
    assert build_path(info, ".flac", MUSIC) == MUSIC / "Artist" / "Singles" / "Title.flac"


def test_is_audio():
    assert is_audio("song.MP3", None)
    assert is_audio("track.flac", "application/octet-stream")
    assert is_audio(None, "audio/mpeg")
    assert not is_audio("movie.mkv", "video/x-matroska")
    assert not is_audio("notes.txt", "text/plain")
