"""Run scdl, adding OmniDL's per-job file manifest.

scdl downloads through yt-dlp. With `--omnidl-manifest FILE`, its yt-dlp also writes one line
per file the run ends up with to FILE — including tracks it found already there (which a
before/after look at the folder never saw), wherever scdl put them (playlists go in a
subfolder). That is how a re-run playlist, or a hosted one, can still offer its files to Save.

scdl's own `--yt-dlp-args` can't be used for this: it merges yt-dlp's command-line defaults
over scdl's settings, which drops scdl's post-processing (its AAC->M4A remux, FLAC conversion).

Deliberately imports nothing from OmniDL, so it runs as a plain script.
"""
from __future__ import annotations

import sys

MANIFEST_FLAG = "--omnidl-manifest"


def split_manifest(argv: list[str]) -> tuple[list[str], str | None]:
    """scdl's arguments, and the manifest path if one was given."""
    argv = list(argv)
    if MANIFEST_FLAG in argv:
        at = argv.index(MANIFEST_FLAG)
        manifest = argv[at + 1] if at + 1 < len(argv) else None
        del argv[at:at + 2]
        return argv, manifest
    return argv, None


def with_manifest(params: dict | None, manifest: str) -> dict:
    """yt-dlp params plus "print each final file path to `manifest`"."""
    params = dict(params or {})
    print_to_file = {when: list(entries) for when, entries in (params.get("print_to_file") or {}).items()}
    print_to_file.setdefault("after_move", []).append(("filepath", manifest))
    params["print_to_file"] = print_to_file
    return params


def main(argv: list[str] | None = None) -> int:
    args, manifest = split_manifest(sys.argv[1:] if argv is None else argv)
    import scdl.scdl as scdl_main

    if manifest:
        plain = scdl_main.YoutubeDL

        class ManifestYoutubeDL(plain):
            def __init__(self, params=None, *rest, **kwargs):
                super().__init__(with_manifest(params, manifest), *rest, **kwargs)

        scdl_main.YoutubeDL = ManifestYoutubeDL
    sys.argv = ["scdl", *args]
    scdl_main._main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
