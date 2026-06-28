"""Write authoritative Spotify metadata (title/artist/album/track#/cover) onto a file.

yt-dlp embeds whatever tags the YouTube source had; this overrides the important fields
with Spotify's values and embeds the real Spotify album cover. Best-effort — any failure
is swallowed so it never breaks a download.
"""
from __future__ import annotations

import urllib.request
from pathlib import Path

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"


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
        else:  # opus / ogg / wav — text tags via the easy interface
            _tag_easy(path, track)
    except Exception:
        pass  # never let tagging break a successful download


def _tag_mp3(path, track, cover):
    from mutagen.id3 import ID3, TIT2, TPE1, TALB, TRCK, APIC, ID3NoHeaderError
    try:
        tags = ID3(str(path))
    except ID3NoHeaderError:
        tags = ID3()
    if track.title:
        tags.setall("TIT2", [TIT2(encoding=3, text=track.title)])
    if track.artist:
        tags.setall("TPE1", [TPE1(encoding=3, text=track.artist)])
    if track.album:
        tags.setall("TALB", [TALB(encoding=3, text=track.album)])
    if track.track_number:
        tags.setall("TRCK", [TRCK(encoding=3, text=str(track.track_number))])
    if cover:
        tags.delall("APIC")
        tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover))
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
    if cover:
        audio["covr"] = [MP4Cover(cover, imageformat=MP4Cover.FORMAT_JPEG)]
    audio.save()


def _tag_flac(path, track, cover):
    from mutagen.flac import FLAC, Picture
    from mutagen.id3 import PictureType
    audio = FLAC(str(path))
    if track.title:
        audio["title"] = track.title
    if track.artist:
        audio["artist"] = track.artist
    if track.album:
        audio["album"] = track.album
    if track.track_number:
        audio["tracknumber"] = str(track.track_number)
    if cover:
        pic = Picture()
        pic.type = int(PictureType.COVER_FRONT)
        pic.mime = "image/jpeg"
        pic.data = cover
        audio.clear_pictures()
        audio.add_picture(pic)
    audio.save()


def _tag_easy(path, track):
    from mutagen import File
    audio = File(str(path), easy=True)
    if audio is None:
        return
    if track.title:
        audio["title"] = track.title
    if track.artist:
        audio["artist"] = track.artist
    if track.album:
        audio["album"] = track.album
    if track.track_number:
        audio["tracknumber"] = str(track.track_number)
    audio.save()
