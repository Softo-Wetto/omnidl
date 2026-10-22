"""Run scdl with OmniDL's link-job hooks (see ytdlp_tool): a per-job file manifest, and a skip
for recordings the output folder already has.

scdl downloads through yt-dlp. With `--omnidl-manifest FILE`, its yt-dlp writes one JSON line
per file it saves (source, title, path) — wherever scdl put it (playlists go in a subfolder) —
and with `--omnidl-known FILE` it skips tracks OmniDL already saved, listing them in the
manifest instead. That is how a re-run playlist, or a hosted one, still offers its files to
Save, and how two tracks with the same name stay two files.

scdl's own `--yt-dlp-args` can't be used for this: it merges yt-dlp's command-line defaults
over scdl's settings, which drops scdl's post-processing (its AAC->M4A remux, FLAC conversion).

Deliberately imports nothing from OmniDL but ytdlp_tool, so it runs as a plain script.
"""
from __future__ import annotations

import sys

try:
    from .ytdlp_tool import KNOWN_FLAG, MANIFEST_FLAG, load_known, take_flag, with_hooks
except ImportError:                       # run as a plain script: ytdlp_tool sits beside it
    from ytdlp_tool import KNOWN_FLAG, MANIFEST_FLAG, load_known, take_flag, with_hooks


def split_manifest(argv: list[str]) -> tuple[list[str], str | None]:
    """scdl's arguments, and the manifest path if one was given."""
    return take_flag(argv, MANIFEST_FLAG)


def main(argv: list[str] | None = None) -> int:
    args, manifest = split_manifest(sys.argv[1:] if argv is None else argv)
    args, known_path = take_flag(args, KNOWN_FLAG)
    import scdl.scdl as scdl_main

    if manifest or known_path:
        known = load_known(known_path)
        plain = scdl_main.YoutubeDL

        class LinkYoutubeDL(plain):
            def __init__(self, params=None, *rest, **kwargs):
                super().__init__(with_hooks(params, manifest, known), *rest, **kwargs)

        scdl_main.YoutubeDL = LinkYoutubeDL
    sys.argv = ["scdl", *args]
    scdl_main._main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
