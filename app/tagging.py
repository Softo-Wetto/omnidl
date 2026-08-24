"""Write authoritative Spotify metadata (title/artist/album/track#/cover) onto a file.

yt-dlp embeds whatever tags the YouTube source had; this overrides the important fields
with Spotify's values and embeds the real Spotify album cover. Best-effort — any failure
is swallowed so it never breaks a download.
"""
from __future__ import annotations

import urllib.request
from pathlib import Path

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"


# yt-dlp's --embed-metadata copies the *video's* description, synopsis and source URL
# into the audio file. They're meaningless for a music track and make tag editors look
# a mess, so they're removed once the authoritative Spotify fields are written.
_JUNK_TAGS = ("description", "synopsis", "purl", "comment", "language")


def _artists(track) -> str:
    """Every credited artist, matching how spotdl names and tags its files."""
    return getattr(track, "all_artists", "") or getattr(track, "artist", "")


def _fetch_cover(url: str) -> bytes | None:
    if not url:
        return None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        return urllib.request.urlopen(req, timeout=15).read()
    except Exception:
        return None


def apply_tags(path: Path, track) -> None:
    """Set title/artist/album/track number and embed cover art on the file at `path`."""
    try:
        ext = path.suffix.lower()
        cover = _fetch_cover(track.cover_url)
        if ext == ".mp3":
            _tag_mp3(path, track, cover)
        elif ext in (".m4a", ".mp4", ".aac"):
            _tag_mp4(path, track, cover)
        elif ext == ".flac":
            _tag_flac(path, track, cover)
        elif ext in (".opus", ".ogg"):
            _tag_ogg(path, track, cover)
        else:  # wav and anything else — text tags via the easy interface
            _tag_easy(path, track)
    except Exception:
        pass  # never let tagging break a successful download


def _tag_mp3(path, track, cover):
    from mutagen.id3 import (ID3, TIT2, TPE1, TPE2, TALB, TRCK, TDRC, APIC,
                             ID3NoHeaderError)
    try:
        tags = ID3(str(path))
    except ID3NoHeaderError:
        tags = ID3()
    if track.title:
        tags.setall("TIT2", [TIT2(encoding=3, text=track.title)])
    if _artists(track):
        tags.setall("TPE1", [TPE1(encoding=3, text=_artists(track))])
    if track.artist:
        tags.setall("TPE2", [TPE2(encoding=3, text=track.artist)])   # album artist = lead
    if track.album:
        tags.setall("TALB", [TALB(encoding=3, text=track.album)])
    if track.track_number:
        tags.setall("TRCK", [TRCK(encoding=3, text=str(track.track_number))])
    if getattr(track, "date", ""):
        tags.setall("TDRC", [TDRC(encoding=3, text=track.date)])
    if cover:
        tags.delall("APIC")
        tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover))
    tags.delall("COMM")
    for frame in list(tags.getall("TXXX")):
        if (frame.desc or "").strip().lower() in _JUNK_TAGS:
            tags.delall(f"TXXX:{frame.desc}")
    tags.save(str(path))


def _tag_mp4(path, track, cover):
    from mutagen.mp4 import MP4, MP4Cover
    audio = MP4(str(path))
    if track.title:
        audio["\xa9nam"] = [track.title]
    if track.artist:
        audio["\xa9ART"] = [track.artist]
    if track.album:
        audio["\xa9alb"] = [track.album]
    if track.track_number:
        audio["trkn"] = [(track.track_number, 0)]
    if getattr(track, "date", ""):
        audio["©day"] = [track.date]
    if cover:
        audio["covr"] = [MP4Cover(cover, imageformat=MP4Cover.FORMAT_JPEG)]
    for key in ("desc", "ldes", "©cmt", "purl"):
        audio.pop(key, None)
    audio.save()


def _tag_flac(path, track, cover):
    from mutagen.flac import FLAC, Picture
    from mutagen.id3 import PictureType
    audio = FLAC(str(path))
    if track.title:
        audio["title"] = track.title
    if _artists(track):
        audio["artist"] = _artists(track)
    if track.artist:
        audio["albumartist"] = track.artist
    if track.album:
        audio["album"] = track.album
    if track.track_number:
        audio["tracknumber"] = str(track.track_number)
    if getattr(track, "date", ""):
        audio["date"] = track.date
    for key in _JUNK_TAGS:
        audio.pop(key, None)
    if cover:
        pic = Picture()
        pic.type = int(PictureType.COVER_FRONT)
        pic.mime = "image/jpeg"
        pic.data = cover
        audio.clear_pictures()
        audio.add_picture(pic)
    audio.save()


def _tag_ogg(path, track, cover):
    """Tag Opus/Vorbis, including the album cover.

    This path used to fall through to the text-only tagger, so every .opus kept the 16:9
    YouTube video thumbnail that --embed-thumbnail had baked in, instead of the square
    Spotify album art. Cover art in Ogg is a base64 FLAC picture block in a comment field.
    """
    import base64
    from mutagen import File
    from mutagen.flac import Picture
    from mutagen.id3 import PictureType

    audio = File(str(path))
    if audio is None:
        return
    if track.title:
        audio["title"] = track.title
    if _artists(track):
        audio["artist"] = _artists(track)
    if track.artist:
        audio["albumartist"] = track.artist
    if track.album:
        audio["album"] = track.album
    if track.track_number:
        audio["tracknumber"] = str(track.track_number)
    if getattr(track, "date", ""):
        audio["date"] = track.date
    for key in _JUNK_TAGS:
        audio.pop(key, None)
    if cover:
        pic = Picture()
        pic.type = int(PictureType.COVER_FRONT)
        pic.mime = "image/jpeg"
        pic.data = cover
        audio["metadata_block_picture"] = [base64.b64encode(pic.write()).decode("ascii")]
    audio.save()


def _tag_easy(path, track):
    from mutagen import File
    audio = File(str(path), easy=True)
    if audio is None:
        return
    if track.title:
        audio["title"] = track.title
    if _artists(track):
        audio["artist"] = _artists(track)
    if track.album:
        audio["album"] = track.album
    if track.track_number:
        audio["tracknumber"] = str(track.track_number)
    audio.save()
