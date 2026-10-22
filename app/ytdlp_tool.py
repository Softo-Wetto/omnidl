"""Run yt-dlp for an OmniDL link job. Without OmniDL's flags it is exactly yt-dlp.

  --omnidl-manifest FILE  one JSON line per finished file: its source (site + media ID), page
                          address, title and path. OmniDL files each one under a name no other
                          recording holds — two different videos called "Same title" used to
                          share one file, so the second link got the first one's audio.
  --omnidl-known FILE     JSON {source: path} of recordings the folder already has. Those are
                          skipped before downloading and listed in the manifest instead, so a
                          re-run reuses its own file without downloading it again.

scdl_tool uses the same hooks for SoundCloud links. Deliberately imports nothing from OmniDL,
so it runs as a plain script (and the .exe runs it for "--run-tool yt-dlp").
"""
from __future__ import annotations

import json
import os
import sys

MANIFEST_FLAG = "--omnidl-manifest"
KNOWN_FLAG = "--omnidl-known"
# yt-dlp's own template language: a one-line JSON object of these fields.
MANIFEST_TEMPLATE = "%(.{extractor_key,id,webpage_url,original_url,title,filepath})j"


def take_flag(argv: list[str], flag: str) -> tuple[list[str], str | None]:
    """`argv` without `flag VALUE`, and VALUE (None if the flag isn't there)."""
    argv = list(argv)
    if flag not in argv:
        return argv, None
    at = argv.index(flag)
    value = argv[at + 1] if at + 1 < len(argv) else None
    del argv[at:at + 2]
    return argv, value


def source_key(info: dict) -> str:
    """Which recording this is, stable across runs: site + media ID ("youtube:dQw4w9WgXcQ").

    A plain file link ("generic") has no real ID — yt-dlp makes one from the file name, and two
    sites' "track.mp3" would share it — so its address identifies it instead.
    """
    site = str(info.get("extractor_key") or info.get("ie_key") or info.get("extractor") or "").strip().lower()
    media_id = str(info.get("id") or "").strip()
    if site in ("", "generic") or not media_id:
        return f"{site or 'generic'}:{info.get('webpage_url') or info.get('original_url') or info.get('url') or media_id}"
    return f"{site}:{media_id}"


def load_known(path: str | None) -> dict[str, str]:
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _append(manifest: str, entry: dict) -> None:
    with open(manifest, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def with_hooks(params: dict | None, manifest: str | None, known: dict[str, str]) -> dict:
    """yt-dlp params plus OmniDL's manifest line and known-recording skip."""
    params = dict(params or {})
    if manifest:
        print_to_file = {when: list(entries) for when, entries in (params.get("print_to_file") or {}).items()}
        print_to_file.setdefault("after_move", []).append((MANIFEST_TEMPLATE, manifest))
        params["print_to_file"] = print_to_file
    if known:
        original = params.get("match_filter")
        reported: set[str] = set()

        def match(info, *, incomplete=False):
            # `incomplete is True` is a bare playlist entry; the full video comes back later.
            if incomplete is not True:
                key = source_key(info)
                path = known.get(key)
                try:
                    have = bool(path) and os.path.getsize(path) > 0
                except OSError:
                    have = False
                if have:
                    if manifest and key not in reported:
                        reported.add(key)
                        _append(manifest, {"source": key, "existing": path, "title": info.get("title")})
                    return f"already downloaded: {os.path.basename(path)}"
            if original is None:
                return None
            try:
                return original(info, incomplete=incomplete)
            except TypeError:
                return None if incomplete else original(info)

        params["match_filter"] = match
    return params


def main(argv: list[str] | None = None) -> int:
    args, manifest = take_flag(sys.argv[1:] if argv is None else argv, MANIFEST_FLAG)
    args, known_path = take_flag(args, KNOWN_FLAG)
    import yt_dlp

    if manifest or known_path:
        known = load_known(known_path)
        plain = yt_dlp.YoutubeDL

        class LinkYoutubeDL(plain):
            def __init__(self, params=None, *rest, **kwargs):
                super().__init__(with_hooks(params, manifest, known), *rest, **kwargs)

        yt_dlp.YoutubeDL = LinkYoutubeDL         # what yt-dlp's command line builds
    return yt_dlp.main(args) or 0


if __name__ == "__main__":
    sys.exit(main())
