"""Focused review reproductions. Temporary media only; outbound network is blocked."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(REPO), str(REPO / "tests")]
from support import make_audio
from app import engines, jobs
from app.jobs import Job, JobManager
from app.spotify_resolver import Track
from app.settings import DEFAULTS


def reap_unowned():
    with tempfile.TemporaryDirectory(dir=HERE, prefix="cleanup-") as td:
        root = Path(td).resolve()
        album = root / "My Artist" / "My Album"
        assert album.resolve().is_relative_to(HERE)
        album.mkdir(parents=True)
        song = album / "keep.mp3"
        song.write_bytes(b"user library fixture; not a hosted job")
        old = time.time() - jobs.FILE_TTL_SECONDS - 600
        os.utime(album, (old, old))
        with patch.object(jobs, "DOWNLOAD_ROOT", root), patch.object(jobs.settings_mod, "LOCAL_MODE", False):
            JobManager()._reap_orphans(time.time())
        return {"untrackedLibraryAlbumDeleted": not album.exists(), "userFileDeleted": not song.exists()}


async def restart_collision():
    with tempfile.TemporaryDirectory(dir=HERE, prefix="collision-") as td:
        root = Path(td)
        original = Track("A/B", "Song", 3)
        requested = Track("A_B", "Song", 3)
        file = make_audio(root / (original.filename + ".opus"), 3)
        JobManager._tag(file, original)
        manager = JobManager()  # a new process has no in-memory written_by identities
        settings = {**DEFAULTS, "output_dir": str(root), "audio_format": "opus", "skip_existing": True}
        job = Job("new request", "youtube", None, "", settings=settings)
        job.status = "running"
        with patch("app.jobs.candidate_search.search_all") as search:
            result = await manager._fetch_track(job, requested, settings, False, 1, 1)
        from mutagen import File
        tags = File(file, easy=True)
        return {"differentIdentities": jobs._identity(original) != jobs._identity(requested),
                "sameFilename": original.filename == requested.filename,
                "outcome": result.status, "requestedArtist": requested.artist,
                "deliveredArtist": tags.get("artist"), "searchCalls": search.call_count,
                "fileOffered": bool(job.list_files())}


async def direct_url_collision():
    with tempfile.TemporaryDirectory(dir=HERE, prefix="direct-") as td:
        root = Path(td)
        out = root / "output"
        out.mkdir()
        # Two different local recordings sharing a title, substituting for two video URLs.
        sources = [make_audio(root / "first" / "Same title.wav", 2),
                   make_audio(root / "second" / "Same title.wav", 4)]
        manager = JobManager()
        manager._persist = lambda: None
        settings = {**DEFAULTS, "output_dir": str(out), "audio_format": "mp3", "skip_existing": True}
        results = []
        for i, source in enumerate(sources):
            with patch.object(jobs.settings_mod, "LOCAL_MODE", True):
                job = await manager.submit(f"https://www.youtube.com/watch?v=fixture{i}", settings, "review")
            # Feed explicit distinct media IDs, matching titles, and local media URLs to the
            # real downloader. Output/conversion/manifest options still come from the app.
            info = root / f"source-{i}.json"
            info.write_text(json.dumps({"id": f"fixture-{i}", "title": "Same title",
                                       "url": source.as_uri(), "webpage_url": source.as_uri(),
                                       "ext": "wav", "extractor": "generic", "extractor_key": "Generic"}),
                            encoding="utf-8")
            job.argv.pop()
            job.argv.extend(["--enable-file-urls", "--load-info-json", str(info)])
            await manager._run_job(job)
            files = job.list_files()
            from mutagen import File
            decoded = subprocess.check_output(["ffmpeg", "-v", "error", "-i", str(files[0]),
                                               "-map", "0:a:0", "-f", "hash", "-hash", "sha256", "-"],
                                              text=True).strip() if files else None
            results.append({"status": job.status, "sourceId": f"fixture-{i}",
                            "sourceSeconds": 2 if i == 0 else 4,
                            "deliveredSeconds": round(File(files[0]).info.length, 2) if files else None,
                            "files": [p.name for p in files],
                            "sha256": hashlib.sha256(files[0].read_bytes()).hexdigest() if files else None,
                            "decodedAudioSha256": decoded,
                            "alreadyDownloaded": "already been downloaded" in job.output})
        return {"jobs": results,
                "sameDecodedAudio": results[0]["decodedAudioSha256"] == results[1]["decodedAudioSha256"]}


async def main():
    results = {"commit": subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=REPO, text=True).strip(),
               "unsafeOrphanCleanup": reap_unowned(),
               "collisionAfterRestart": await restart_collision(),
               "directUrlCollision": await direct_url_collision()}
    (HERE / "backend-results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(json.dumps(results, indent=2))


asyncio.run(main())
