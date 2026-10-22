"""The follow-up review of 18 October 2026 (docs/audits/2026-10-18): F01-F03, pinned.

Temporary folders and locally generated tones only. The real-downloader tests run yt-dlp and
ffmpeg on local files (file:// fixtures), never a provider; the real library is never touched.
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app import engines, jobs, ytdlp_tool
from app import settings as settings_mod
from app.jobs import Job, JobManager
from app.library import STAGING_DIR, build_library_index
from app.matching import Candidate
from app.provenance import Provenance, credits, same_song, tag_names, tag_urls
from app.spotify_resolver import Track
from support import make_audio, output_of, write_audio_output


def new_manager(store: Path) -> JobManager:
    """A manager as a fresh process would have it: nothing in memory, the record on disk."""
    manager = JobManager()
    manager.owners = Provenance(store)
    manager._persist = lambda: None
    manager._send = AsyncMock()
    return manager


def link_settings(out: Path, **extra) -> dict:
    return {**settings_mod.DEFAULTS, "output_dir": str(out), "audio_format": "mp3", **extra}


def fake_link_run(entries, *, delay: float = 0.0, site: str = "Youtube"):
    """Stand-in for yt-dlp/scdl on a staged link job, doing what ytdlp_tool does: skip sources
    the known list names (reporting the file it has), otherwise save each (relative path,
    media ID, seconds) into the job's staging folder and report it in the manifest."""
    async def run(job, argv, **kwargs):
        s = job.settings
        stage, manifest = Path(s["_stage_dir"]), Path(s["_produced_file"])
        known_file = Path(s["_known_file"])
        known = json.loads(known_file.read_text(encoding="utf-8")) if known_file.exists() else {}
        for rel, media_id, seconds in entries:
            info = {"extractor_key": site, "id": media_id, "title": Path(rel).stem,
                    "webpage_url": f"https://www.youtube.com/watch?v={media_id}"}
            key = ytdlp_tool.source_key(info)
            with open(manifest, "a", encoding="utf-8") as out:
                if key in known:
                    out.write(json.dumps({"source": key, "existing": known[key]}) + "\n")
                    continue
                await asyncio.sleep(delay)
                path = make_audio(stage / rel, seconds)
                out.write(json.dumps({**info, "filepath": str(path)}) + "\n")
        return 0
    return run


async def run_link(manager: JobManager, url: str, out: Path, entries, **extra):
    with patch("app.jobs.settings_mod.LOCAL_MODE", True):
        job = await manager.submit(url, link_settings(out, **extra), "s")
    with patch.object(manager, "_stream_subprocess", side_effect=fake_link_run(entries)):
        await manager._run_job(job)
    return job


def seconds(path: Path) -> float:
    return round(jobs._media_length(path) or 0, 1)


# ---- F01: hosted cleanup deletes only folders OmniDL marked as a hosted job's -------------

def junction(link: Path, target: Path) -> bool:
    try:
        if sys.platform == "win32":
            import _winapi
            _winapi.CreateJunction(str(target), str(link))
        else:
            os.symlink(target, link, target_is_directory=True)
        return True
    except (OSError, ImportError, AttributeError):
        return False


class HostedCleanupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.root = self.tmp / "hosted"
        self.legacy = self.tmp / "downloads"
        self.root.mkdir()
        self.legacy.mkdir()
        self.patches = [patch("app.jobs.settings_mod.LOCAL_MODE", False),
                        patch("app.jobs.DOWNLOAD_ROOT", self.root),
                        patch("app.jobs.LEGACY_DOWNLOAD_ROOT", self.legacy)]
        for p in self.patches:
            p.start()
        self.manager = new_manager(self.tmp / "prov.json")
        self.later = time.time() + settings_mod.FILE_TTL_SECONDS + 600

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self._tmp.cleanup()

    @staticmethod
    def age(*paths):
        old = time.time() - settings_mod.FILE_TTL_SECONDS - 600
        for path in paths:
            os.utime(path, (old, old))

    def hosted_job(self, session="sess", status="done"):
        job = Job("x", "youtube", ["yt-dlp"], "", session=session)
        JobManager._prepare_out_dir(job, {}, session)
        (job.out_dir / "Song.mp3").write_bytes(b"x")
        job.status, job.finished = status, time.time() - settings_mod.FILE_TTL_SECONDS - 600
        return job

    def test_hosted_storage_has_its_own_folder_by_default(self):
        if not os.environ.get("OMNIDL_DOWNLOAD_ROOT"):
            self.assertNotEqual(settings_mod.DOWNLOAD_ROOT, settings_mod.DEFAULT_OUTPUT)
        self.assertEqual(settings_mod.DEFAULT_OUTPUT, settings_mod.LEGACY_DOWNLOAD_ROOT)

    async def test_an_old_album_under_the_hosted_root_survives(self):
        """The reviewer's reproduction: an aged Artist/Album folder that was never a job."""
        album = self.root / "My Artist" / "My Album"
        album.mkdir(parents=True)
        (album / "keep.mp3").write_bytes(b"library")
        self.age(album, album.parent)
        await self.manager._expire_files(now=self.later)
        self.assertTrue((album / "keep.mp3").exists())

    async def test_names_that_only_look_like_jobs_survive(self):
        unmarked = self.root / "sess" / "0123456789ab"                  # job-shaped, no marker
        wrong = self.root / "sess" / "ba9876543210"                    # marker names another job
        not_a_job = self.root / "sess" / "Album"                       # marker, but not a job name
        for folder in (unmarked, wrong, not_a_job):
            folder.mkdir(parents=True)
            (folder / "song.mp3").write_bytes(b"x")
        (wrong / jobs._JOB_MARKER).write_text(json.dumps(
            {"omnidl": "hosted-job", "job": "0123456789ab", "session": "sess"}), encoding="utf-8")
        (not_a_job / jobs._JOB_MARKER).write_text(json.dumps(
            {"omnidl": "hosted-job", "job": "Album", "session": "sess"}), encoding="utf-8")
        self.age(unmarked, wrong, not_a_job)
        await self.manager._expire_files(now=self.later)
        for folder in (unmarked, wrong, not_a_job):
            self.assertTrue((folder / "song.mp3").exists(), folder.name)

    async def test_owned_expired_storage_goes_and_active_jobs_stay(self):
        finished = self.hosted_job()                       # in the history, expired
        orphan = self.hosted_job(session="gone")          # history no longer has it
        running = self.hosted_job(status="running")       # still downloading, however old
        for job in (finished, running):
            self.manager.jobs[job.id] = job
        self.age(finished.out_dir, orphan.out_dir, running.out_dir)
        await self.manager._expire_files(now=self.later)
        self.assertFalse(finished.out_dir.exists())
        self.assertFalse(orphan.out_dir.exists())
        self.assertFalse(orphan.out_dir.parent.exists())   # the emptied visitor folder too
        self.assertTrue((running.out_dir / "Song.mp3").exists())

    async def test_deleting_a_job_removes_only_its_own_marked_folder(self):
        job = self.hosted_job()
        self.manager.jobs[job.id] = job
        self.manager.order.append(job.id)
        self.assertTrue(await self.manager.delete(job.id, "sess"))
        self.assertFalse(job.out_dir.exists())
        # A history record pointing at a folder OmniDL never marked (an album) is refused.
        album = self.tmp / "Music" / "Album"
        album.mkdir(parents=True)
        (album / "keep.mp3").write_bytes(b"x")
        forged = Job("x", "youtube", ["yt-dlp"], "", session="sess")
        forged.out_dir = album
        self.assertFalse(JobManager._remove_files(forged))
        self.assertTrue((album / "keep.mp3").exists())

    async def test_a_local_library_survives_a_switch_to_hosted_mode(self):
        """A local install whose library is the old shared "downloads" folder, restarted with
        OMNIDL_HOST=0.0.0.0: nothing in it is cleaned, however old, however it's named."""
        album = self.legacy / "Artist" / "Album"
        job_shaped = self.legacy / "Some Folder" / "0123456789ab"
        for folder in (album, job_shaped):
            folder.mkdir(parents=True)
            (folder / "song.mp3").write_bytes(b"library")
        self.age(album, album.parent, job_shaped, job_shaped.parent)
        with patch("builtins.print") as printed:
            self.manager._adopt_hosted_folders()
        await self.manager._expire_files(now=self.later)
        self.assertTrue((album / "song.mp3").exists())
        self.assertTrue((job_shaped / "song.mp3").exists())
        self.assertIn("aren't linked to any job", " ".join(str(c) for c in printed.call_args_list))

    async def test_folders_of_jobs_in_the_history_are_carried_over_and_expire(self):
        """Hosted downloads from before the marker, in the old root: adopted on start because
        the history says OmniDL made exactly that folder for that job; then they expire."""
        job = Job("x", "youtube", ["yt-dlp"], "", session="sess")
        job.out_dir = self.legacy / "sess" / job.id
        job.out_dir.mkdir(parents=True)
        (job.out_dir / "Song.mp3").write_bytes(b"x")
        job.status, job.finished = "done", time.time()
        self.manager.jobs[job.id] = job
        self.manager._adopt_hosted_folders()
        self.assertTrue(jobs._owned_job_dir(job.out_dir, job.id))
        await self.manager._expire_files(now=self.later)
        self.assertFalse(job.out_dir.exists())

    async def test_links_and_junctions_are_never_followed(self):
        outside = self.tmp / "outside"
        victim = outside / "0123456789ab"
        victim.mkdir(parents=True)
        (victim / "precious.mp3").write_bytes(b"x")
        (victim / jobs._JOB_MARKER).write_text(json.dumps(
            {"omnidl": "hosted-job", "job": "0123456789ab", "session": "linked"}), encoding="utf-8")
        if not junction(self.root / "linked", outside):
            self.skipTest("can't create a junction/symlink here")
        self.age(victim)
        await self.manager._expire_files(now=self.later)
        self.assertTrue((victim / "precious.mp3").exists())
        # A job whose own folder is a link elsewhere isn't removed either.
        job = Job("x", "youtube", ["yt-dlp"], "", session="sess")
        (self.root / "sess").mkdir()
        job.id = "0123456789ab"
        job.out_dir = self.root / "sess" / job.id
        self.assertTrue(junction(job.out_dir, victim))
        self.assertFalse(JobManager._remove_files(job))
        self.assertTrue((victim / "precious.mp3").exists())


# ---- F02: links are filed by their source, never by title alone ---------------------------

class LinkIdentityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.out = self.tmp / "music"
        self.out.mkdir()
        self.store = self.tmp / "prov.json"

    def tearDown(self):
        self._tmp.cleanup()

    def files(self):
        return sorted(p.name for p in self.out.rglob("*") if p.is_file())

    def test_a_link_command_downloads_into_staging_through_the_wrapper(self):
        s = link_settings(self.out, _stage_dir=str(self.out / STAGING_DIR / "j1"),
                          _produced_file="C:/m.txt", _known_file="C:/k.json")
        argv = engines.build_command("youtube", "https://youtu.be/abc", s)
        self.assertTrue(argv[2].endswith("ytdlp_tool.py"))
        template = argv[argv.index("-o") + 1]
        self.assertTrue(template.startswith(str(self.out / STAGING_DIR / "j1")))
        self.assertIn("%(autonumber)s", template)
        self.assertEqual("C:/m.txt", argv[argv.index("--omnidl-manifest") + 1])
        self.assertEqual("C:/k.json", argv[argv.index("--omnidl-known") + 1])
        self.assertNotIn("--print-to-file", argv)
        shown = engines.describe_command(argv)
        self.assertTrue(shown.startswith("yt-dlp "))
        self.assertNotIn("--omnidl", shown)
        sc = engines.build_command("soundcloud", "https://soundcloud.com/a/b", s)
        self.assertEqual(str(self.out / STAGING_DIR / "j1"), sc[sc.index("--path") + 1])
        self.assertIn("--omnidl-known", sc)

    def test_the_source_is_site_and_id_and_a_plain_file_link_is_its_address(self):
        self.assertEqual("youtube:abc", ytdlp_tool.source_key({"extractor_key": "Youtube", "id": "abc"}))
        a = ytdlp_tool.source_key({"extractor_key": "Generic", "id": "track", "webpage_url": "https://a.test/track.mp3"})
        b = ytdlp_tool.source_key({"extractor_key": "Generic", "id": "track", "webpage_url": "https://b.test/track.mp3"})
        self.assertNotEqual(a, b)

    def test_the_wrapper_skips_only_recordings_it_is_told_about(self):
        have = make_audio(self.out / "Have.mp3", 2)
        manifest = self.tmp / "m.jsonl"
        params = ytdlp_tool.with_hooks({}, str(manifest), {"youtube:old": str(have)})
        match = params["match_filter"]
        self.assertIsNone(match({"extractor_key": "Youtube", "id": "old"}, incomplete=True))  # bare entry
        self.assertIn("already downloaded", match({"extractor_key": "Youtube", "id": "old"}, incomplete={"url"}))
        self.assertIn("already downloaded", match({"extractor_key": "Youtube", "id": "old"}, incomplete=False))
        self.assertIsNone(match({"extractor_key": "Youtube", "id": "new"}, incomplete=False))
        lines = [json.loads(l) for l in manifest.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([{"source": "youtube:old", "existing": str(have), "title": None}], lines)

    async def test_two_links_with_one_title_in_one_playlist_are_two_files(self):
        manager = new_manager(self.store)
        job = await run_link(manager, "https://www.youtube.com/playlist?list=x", self.out,
                             [("00001/Same title.mp3", "aaa", 2), ("00002/Same title.mp3", "bbb", 4)])
        self.assertEqual(["Same title (2).mp3", "Same title.mp3"], self.files())
        self.assertEqual([2.0, 4.0], [seconds(p) for p in job.list_files()])
        self.assertFalse((self.out / STAGING_DIR).exists())

    async def test_a_rerun_reuses_its_own_file_even_after_a_restart(self):
        first = new_manager(self.store)
        await run_link(first, "https://youtu.be/aaa", self.out, [("00001/Same title.mp3", "aaa", 2)])
        await run_link(first, "https://youtu.be/bbb", self.out, [("00001/Same title.mp3", "bbb", 4)])
        first.owners.flush(force=True)
        restarted = new_manager(self.store)
        for media_id, length in (("bbb", 4.0), ("aaa", 2.0)):
            job = await run_link(restarted, f"https://youtu.be/{media_id}", self.out,
                                 [("00001/Same title.mp3", media_id, length)])
            self.assertEqual([length], [seconds(p) for p in job.list_files()], media_id)
        self.assertEqual(["Same title (2).mp3", "Same title.mp3"], self.files())   # nothing new

    async def test_overlapping_link_jobs_never_share_a_name(self):
        manager = new_manager(self.store)
        with patch("app.jobs.settings_mod.LOCAL_MODE", True):
            same = [await manager.submit("https://youtu.be/aaa", link_settings(self.out), "s") for _ in range(2)]
            other = await manager.submit("https://youtu.be/bbb", link_settings(self.out), "s")
        runs = {same[0].id: [("00001/Same title.mp3", "aaa", 2)], same[1].id: [("00001/Same title.mp3", "aaa", 2)],
                other.id: [("00001/Same title.mp3", "bbb", 4)]}

        async def fake(job, argv, **kwargs):
            return await fake_link_run(runs[job.id], delay=0.02)(job, argv, **kwargs)

        with patch.object(manager, "_stream_subprocess", side_effect=fake):
            await asyncio.gather(*(manager._run_job(j) for j in (*same, other)))
        self.assertEqual(["Same title (2).mp3", "Same title.mp3"], self.files())
        self.assertEqual(same[0].list_files(), same[1].list_files())          # one recording, one file
        self.assertEqual([4.0], [seconds(p) for p in other.list_files()])
        self.assertEqual([2.0], [seconds(p) for p in same[0].list_files()])

    async def test_a_link_and_a_matched_song_never_take_each_others_file(self):
        manager = new_manager(self.store)
        settings = {**link_settings(self.out), "skip_existing": True, "concurrency": 1}
        song = Job("Nova - Midnight Run", "youtube", None, "", settings=dict(settings))
        song.status = "running"
        with patch("app.jobs.settings_mod.LOCAL_MODE", True):
            link = await manager.submit("https://youtu.be/vid", link_settings(self.out), "s")

        async def fake(job, argv, **kwargs):
            if job is link:
                return await fake_link_run([("00001/Nova - Midnight Run.mp3", "vid", 4)], delay=0.01)(job, argv)
            await asyncio.sleep(0.01)
            write_audio_output(argv, 3)
            return 0

        verified = Candidate("youtube_music", "https://yt.test/song", "Midnight Run", "Nova", 3, True)
        with patch("app.jobs.candidate_search.search_all", return_value=[verified]), \
                patch.object(manager, "_stream_subprocess", side_effect=fake):
            outcome, _ = await asyncio.gather(
                manager._fetch_track(song, Track("Nova", "Midnight Run", 3), song.settings, True, 1, 1),
                manager._run_job(link))
        self.assertEqual("downloaded", outcome.status)
        self.assertEqual(["Nova - Midnight Run (2).mp3", "Nova - Midnight Run.mp3"], self.files())
        song.status = "done"
        self.assertEqual([3.0], [seconds(p) for p in song.list_files()])
        self.assertEqual([4.0], [seconds(p) for p in link.list_files()])
        self.assertNotEqual(song.list_files(), link.list_files())

    async def test_a_link_saved_before_records_were_kept_is_recognised_by_its_tags(self):
        from mutagen.id3 import ID3, TXXX
        legacy = make_audio(self.out / "Same title.mp3", 2)
        tags = ID3()
        tags.add(TXXX(encoding=3, desc="purl", text=["https://www.youtube.com/watch?v=aaa"]))
        tags.save(str(legacy))
        self.assertIn("https://www.youtube.com/watch?v=aaa", tag_urls(legacy))
        manager = new_manager(self.store)
        job = await run_link(manager, "https://youtu.be/aaa", self.out, [("00001/Same title.mp3", "aaa", 4)])
        self.assertEqual(["Same title.mp3"], self.files())                    # kept, not "(2)"
        self.assertEqual([legacy], job.list_files())
        self.assertIn("already downloaded", job.output)
        other = await run_link(manager, "https://youtu.be/bbb", self.out, [("00001/Same title.mp3", "bbb", 4)])
        self.assertEqual([4.0], [seconds(p) for p in other.list_files()])     # a different video

    async def test_asking_for_a_fresh_copy_replaces_only_the_same_recording(self):
        manager = new_manager(self.store)
        await run_link(manager, "https://youtu.be/aaa", self.out, [("00001/Same title.mp3", "aaa", 2)])
        job = await run_link(manager, "https://youtu.be/aaa", self.out, [("00001/Same title.mp3", "aaa", 4)],
                             skip_existing=False)
        self.assertEqual(["Same title.mp3"], self.files())
        self.assertEqual([4.0], [seconds(p) for p in job.list_files()])

    async def test_soundcloud_playlists_keep_their_folder(self):
        manager = new_manager(self.store)
        job = await run_link(manager, "https://soundcloud.com/a/sets/b", self.out,
                             [("Mix/Uploader - Track.mp3", "1", 2), ("Uploader - Single.mp3", "2", 3)])
        self.assertEqual([self.out / "Mix" / "Uploader - Track.mp3", self.out / "Uploader - Single.mp3"],
                         job.list_files())

    async def test_a_hosted_link_job_keeps_its_files_in_its_own_folder(self):
        root = self.tmp / "hosted"
        manager = new_manager(self.store)
        with patch("app.jobs.settings_mod.LOCAL_MODE", False), patch("app.jobs.DOWNLOAD_ROOT", root):
            job = await manager.submit("https://youtu.be/aaa", link_settings(self.out), "visitor")
            self.assertTrue(job.settings["_stage_dir"].startswith(str(job.out_dir)))
            with patch.object(manager, "_stream_subprocess",
                              side_effect=fake_link_run([("00001/Same title.mp3", "aaa", 2)])):
                await manager._run_job(job)
            self.assertEqual([job.out_dir / "Same title.mp3"], job.list_files())     # not the marker
            self.assertEqual(sorted(["Same title.mp3", jobs._JOB_MARKER]),       # staging cleaned up
                             sorted(p.name for p in job.out_dir.iterdir()))
            self.assertTrue(jobs._owned_job_dir(job.out_dir, job.id))
        self.assertEqual([], self.files())                         # nothing in the local folder

    async def test_a_cancelled_run_still_files_what_finished(self):
        manager = new_manager(self.store)
        with patch("app.jobs.settings_mod.LOCAL_MODE", True):
            job = await manager.submit("https://youtu.be/x", link_settings(self.out), "s")

        async def partly(j, argv, **kwargs):
            await fake_link_run([("00001/Done.mp3", "a", 2)])(j, argv)
            (Path(j.settings["_stage_dir"]) / "00002").mkdir()
            (Path(j.settings["_stage_dir"]) / "00002" / "Half.webm.part").write_bytes(b"x")
            j.status = "cancelled"
            return 1

        with patch.object(manager, "_stream_subprocess", side_effect=partly):
            await manager._run_job(job)
        self.assertEqual("cancelled", job.status)
        self.assertEqual(["Done.mp3"], self.files())
        self.assertEqual([self.out / "Done.mp3"], job.list_files())

    async def test_files_appear_one_by_one_while_a_playlist_runs(self):
        """Not all at the end: a crash part-way through keeps everything already finished."""
        manager = new_manager(self.store)
        with patch("app.jobs.settings_mod.LOCAL_MODE", True):
            job = await manager.submit("https://www.youtube.com/playlist?list=x", link_settings(self.out), "s")
        seen_mid_run = {}

        async def playlist(j, argv, **kwargs):
            await fake_link_run([("00001/First.mp3", "a", 2)])(j, argv)
            for _ in range(100):
                if (self.out / "First.mp3").exists():
                    break
                await asyncio.sleep(0.02)
            seen_mid_run["first"] = (self.out / "First.mp3").exists()
            await fake_link_run([("00002/Second.mp3", "b", 2)])(j, argv)
            return 0

        with patch.object(JobManager, "_FILE_EVERY", 0.05), \
                patch.object(manager, "_stream_subprocess", side_effect=playlist):
            await manager._run_job(job)
        self.assertTrue(seen_mid_run["first"])
        self.assertEqual(["First.mp3", "Second.mp3"], [p.name for p in job.list_files()])
        self.assertFalse(Path(job.settings["_produced_file"]).exists())     # bookkeeping cleaned up
        self.assertFalse(Path(job.settings["_known_file"]).exists())

    def test_a_crashs_leftover_working_folders_are_cleared_on_start(self):
        staging = self.out / STAGING_DIR
        old = time.time() - 2 * 86400
        names = ("0123456789ab", "0123456789ab-3", "fedcba987654", "Mine")
        for name in names:
            (staging / name).mkdir(parents=True)
            (staging / name / "part.webm").write_bytes(b"x")
        for name in ("0123456789ab", "0123456789ab-3", "Mine"):
            os.utime(staging / name, (old, old))
        with patch("app.jobs.settings_mod.LOCAL_MODE", True):
            JobManager._sweep_stale_staging(self.out)
        self.assertEqual(["Mine", "fedcba987654"], sorted(p.name for p in staging.iterdir()))

    async def test_the_known_list_names_only_usable_files_of_the_asked_format(self):
        manager = new_manager(self.store)
        mp3 = make_audio(self.out / "A.mp3", 2)
        opus = make_audio(self.out / "B.opus", 2)
        empty = self.out / "C.mp3"
        empty.write_bytes(b"")
        elsewhere = make_audio(self.tmp / "other" / "D.mp3", 2)
        for path, source, as_ in ((mp3, "youtube:a", "audio:mp3"), (opus, "youtube:b", "audio:opus"),
                                  (empty, "youtube:c", "audio:mp3"), (elsewhere, "youtube:d", "audio:mp3")):
            manager.owners.record(path, kind="link", source=source, **{"as": as_})
        for skip, expected in ((True, {"youtube:a": str(mp3.resolve())}), (False, {})):
            job = Job("x", "youtube", ["x"], "", settings={**link_settings(self.out, skip_existing=skip),
                                                           "_known_file": str(self.tmp / "k.json")})
            manager._write_known(job)
            known = json.loads((self.tmp / "k.json").read_text(encoding="utf-8"))
            self.assertEqual({k: os.path.normcase(v) for k, v in expected.items()},
                             {k: os.path.normcase(str(Path(v).resolve())) for k, v in known.items()})

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("yt-dlp"), "needs ffmpeg and yt-dlp")
    async def test_the_reviewers_reproduction_with_the_real_downloader(self):
        """Two local recordings with one title, given distinct IDs, through the real wrapper,
        yt-dlp and ffmpeg (file URLs, no network) — then a re-run of each after a restart."""
        sources = {"fixture-0": make_audio(self.tmp / "first" / "Same title.wav", 2),
                   "fixture-1": make_audio(self.tmp / "second" / "Same title.wav", 4)}

        async def run(manager, media_id):
            with patch("app.jobs.settings_mod.LOCAL_MODE", True):
                job = await manager.submit(f"https://www.youtube.com/watch?v={media_id}",
                                           link_settings(self.out), "review")
            info = self.tmp / f"{media_id}.json"
            src = sources[media_id]
            info.write_text(json.dumps({"id": media_id, "title": "Same title", "url": src.as_uri(),
                                        "webpage_url": src.as_uri(), "ext": "wav",
                                        "extractor": "generic", "extractor_key": "Generic"}), encoding="utf-8")
            job.argv.pop()                                   # the web address: never fetched
            job.argv.extend(["--enable-file-urls", "--load-info-json", str(info)])
            await manager._run_job(job)
            return job

        manager = new_manager(self.store)
        first, second = await run(manager, "fixture-0"), await run(manager, "fixture-1")
        self.assertEqual(("done", "done"), (first.status, second.status))
        self.assertEqual([2.0], [round(seconds(p)) for p in first.list_files()])
        self.assertEqual([4.0], [round(seconds(p)) for p in second.list_files()])
        self.assertNotIn("already been downloaded", second.output)
        manager.owners.flush(force=True)
        restarted = new_manager(self.store)
        again = await run(restarted, "fixture-1")
        self.assertEqual(second.list_files(), again.list_files())
        self.assertIn("already downloaded", again.output)
        self.assertEqual(["Same title (2).mp3", "Same title.mp3"], self.files())


# ---- F03: a matched song only reuses a file that is that song, across restarts ------------

class RestartIdentityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.out = Path(self._tmp.name) / "music"
        self.out.mkdir()
        self.store = Path(self._tmp.name) / "prov.json"

    def tearDown(self):
        self._tmp.cleanup()

    async def fetch(self, track, *, manager=None, fmt="opus", index=False, length=3):
        """One matched song, as a fresh process would fetch it. Real tags are written (no cover
        is fetched: the tracks have none); the download itself is a local tone."""
        manager = manager or new_manager(self.store)
        settings = {"output_dir": str(self.out), "skip_existing": True, "audio_format": fmt, "concurrency": 1}
        if index:
            settings["_library_index"] = build_library_index(self.out)
        job = Job(track.query, "youtube", None, "", settings=settings)
        job.status = "running"
        started = []

        async def download(_job, argv, emit, **kwargs):
            started.append(output_of(argv))
            write_audio_output(argv, length)
            return 0

        found = Candidate("youtube_music", "https://yt.test/x", track.title, track.artist, length, True)
        with patch("app.jobs.candidate_search.search_all", return_value=[found]), \
                patch.object(manager, "_stream_subprocess", side_effect=download):
            result = await manager._fetch_track(job, track, settings, True, 1, 1)
        manager.owners.flush(force=True)
        job.status = "done"
        return result, job, started

    async def test_the_sanitised_name_collision_survives_a_restart(self):
        """The reviewer's case: "A/B - Song" saved; a new process asked for "A_B - Song"."""
        saved, first_job, _ = await self.fetch(Track("A/B", "Song", 3))
        self.assertEqual("downloaded", saved.status)
        result, job, started = await self.fetch(Track("A_B", "Song", 3))
        self.assertEqual(("downloaded", 1), (result.status, len(started)))
        self.assertEqual("A_B - Song (2).opus", result.saved_as)
        self.assertEqual(("A/B", "Song"), tag_names(self.out / "A_B - Song.opus"))
        self.assertEqual(("A_B", "Song"), tag_names(self.out / "A_B - Song (2).opus"))
        self.assertEqual([self.out / "A_B - Song.opus"], first_job.list_files())
        self.assertEqual([self.out / "A_B - Song (2).opus"], job.list_files())
        # And each is found again by its own request after another restart.
        for track, name in ((Track("A/B", "Song", 3), "A_B - Song.opus"), (Track("A_B", "Song", 3), "A_B - Song (2).opus")):
            again, again_job, started = await self.fetch(track)
            self.assertEqual(("skipped", 0, [self.out / name]), (again.status, len(started), again_job.list_files()))

    async def test_truncated_names_stay_two_songs(self):
        long_title = "x" * 175                 # names are cut at 180 characters
        first = Track("Artist", long_title + " (Part One)", 3)
        second = Track("Artist", long_title + " (Part Two)", 3)
        self.assertEqual(first.filename, second.filename)
        await self.fetch(first)
        result, _, _ = await self.fetch(second)
        self.assertEqual("downloaded", result.status)
        self.assertTrue(result.saved_as.endswith(" (2).opus"))
        again, _, started = await self.fetch(first)
        self.assertEqual(("skipped", 0), (again.status, len(started)))

    async def test_titles_that_differ_only_in_saved_out_characters_are_different_songs(self):
        await self.fetch(Track("Artist", "What?", 3))
        result, _, started = await self.fetch(Track("Artist", "What*", 3))
        self.assertEqual(("downloaded", 1), (result.status, len(started)))
        self.assertEqual("Artist - What_ (2).opus", result.saved_as)

    async def test_a_file_put_there_by_hand_is_kept_and_not_taken_for_the_song(self):
        imported = make_audio(self.out / "A_B - Song.opus", 3)      # no tags, no record
        before = imported.read_bytes()
        result, job, started = await self.fetch(Track("A_B", "Song", 3), index=True)
        self.assertEqual(("downloaded", 1), (result.status, len(started)))   # not "already in library"
        self.assertEqual(before, imported.read_bytes())
        self.assertEqual([self.out / "A_B - Song (2).opus"], job.list_files())

    async def test_a_normal_rerun_still_skips_what_it_saved(self):
        await self.fetch(Track("Nova", "Midnight Run", 3, artists=["Nova", "Guest"]))
        again, _, started = await self.fetch(Track("Nova", "Midnight Run", 3, artists=["Nova", "Guest"]))
        self.assertEqual(("skipped", 0), (again.status, len(started)))

    async def test_m4a_keeps_only_the_lead_artist_and_still_counts(self):
        track = Track("Nova", "Midnight Run", 3, artists=["Nova", "Guest"])
        await self.fetch(track, fmt="m4a")
        self.assertEqual("Nova", tag_names(self.out / "Nova, Guest - Midnight Run.m4a")[0])
        again, _, started = await self.fetch(track, fmt="m4a")
        self.assertEqual(("skipped", 0), (again.status, len(started)))

    async def test_without_tags_the_lasting_record_says_whose_file_it_is(self):
        """A format whose tags couldn't be written: OmniDL's record identifies the file."""
        manager = new_manager(self.store)
        with patch.object(JobManager, "_tag"):
            saved, _, _ = await self.fetch(Track("A/B", "Song", 3), manager=manager)
        self.assertEqual(("", ""), tag_names(self.out / "A_B - Song.opus"))
        with patch.object(JobManager, "_tag"):
            same, _, started_same = await self.fetch(Track("A/B", "Song", 3))
            other, _, started_other = await self.fetch(Track("A_B", "Song", 3))
        self.assertEqual(("skipped", 0), (same.status, len(started_same)))
        self.assertEqual(("downloaded", "A_B - Song (2).opus"), (other.status, other.saved_as))

    async def test_a_pasted_links_file_is_never_taken_for_a_matched_song(self):
        link_file = make_audio(self.out / "Nova - Midnight Run.opus", 3)
        JobManager._tag(link_file, Track("Nova", "Midnight Run"))      # tags even say it's the song
        manager = new_manager(self.store)
        manager.owners.record(link_file, kind="link", source="youtube:musicvideo", **{"as": "audio:opus"})
        manager.owners.flush(force=True)
        result, _, started = await self.fetch(Track("Nova", "Midnight Run", 3))
        self.assertEqual(("downloaded", "Nova - Midnight Run (2).opus"), (result.status, result.saved_as))


class ProvenanceTests(unittest.TestCase):
    def test_the_record_survives_a_restart_and_forgets_deleted_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "prov.json"
            kept, gone = make_audio(Path(tmp) / "a.mp3", 2), make_audio(Path(tmp) / "b.mp3", 2)
            owners = Provenance(store)
            owners.record_track(kept, Track("A", "Song"), "j1")
            owners.record(gone, kind="link", source="youtube:x", **{"as": "audio:mp3"})
            owners.flush(force=True)
            self.assertEqual([], list(Path(tmp).glob("*.tmp")))
            gone.unlink()
            again = Provenance(store)
            self.assertEqual(("track", "A", "Song"),
                             tuple(again.get(kept)[k] for k in ("kind", "artists", "title")))
            self.assertIsNone(again.get(gone))
            self.assertEqual({}, again.links_in(Path(tmp), "audio:mp3"))
            again.prune()
            self.assertEqual(1, len(json.loads(store.read_text(encoding="utf-8"))["files"]))

    def test_a_drive_that_isnt_plugged_in_keeps_its_records(self):
        missing = next((f"{c}:\\" for c in "QRSTUVWXYZ" if not os.path.exists(f"{c}:\\")), None)
        if sys.platform != "win32" or missing is None:
            self.skipTest("needs a free Windows drive letter")
        with tempfile.TemporaryDirectory() as tmp:
            owners = Provenance(Path(tmp) / "prov.json")
            owners.record(missing + "Music\\A - Song.mp3", kind="track", artists="A", title="Song")
            owners.prune()
            self.assertEqual(1, len(json.loads((Path(tmp) / "prov.json").read_text(encoding="utf-8"))["files"]))

    def test_a_damaged_record_starts_empty_instead_of_failing(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "prov.json"
            store.write_text('{"files": {"x": ', encoding="utf-8")
            self.assertIsNone(Provenance(store).get(Path(tmp) / "x.mp3"))

    def test_same_song(self):
        self.assertTrue(same_song("A, B", "Song", "A/B", "Song"))           # tag formats differ
        self.assertTrue(same_song("A", "Song", "A, B", "Song"))             # lead artist only
        self.assertTrue(same_song("a", "song", "A", "Song"))                 # case
        self.assertFalse(same_song("A/B", "Song", "A_B", "Song"))            # the sanitised clash
        self.assertFalse(same_song("A", "What?", "A", "What*"))
        self.assertFalse(same_song("A", "Song", "B", "Song"))
        self.assertFalse(same_song("", "Song", "", "Song"))                  # nothing to compare
        self.assertEqual(["feather"], credits("Feather"))                    # "feat" isn't split inside a word
        self.assertEqual(["a", "b"], credits("A feat. B"))


if __name__ == "__main__":
    unittest.main()
