import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET

from musicbrainz_helper import ratings, ratings_worker as helper

RECORDING = "00000000-0000-4000-8000-000000000004"
OTHER = "00000000-0000-4000-8000-000000000005"
SONG = {"id": "song1", "musicBrainzId": RECORDING, "userRating": 0,
        "artist": "Artist", "album": "Album", "title": "Song", "path": "Artist/Album/Song.flac"}
TRACK = {"recording": RECORDING, "artist": "Artist", "album": "Album", "title": "Song",
         "path": "/music/Artist/Album/Song.flac"}

class FakeSubsonic:
    def __init__(self, *songs):
        self.data = {str(s["id"]): copy.deepcopy(s) for s in songs}
        self.scans = 0
    def songs(self):
        self.scans += 1
        return copy.deepcopy(self.data)
    def get_song(self, identity):
        return copy.deepcopy(self.data[identity])
    def set_rating(self, *args):
        raise AssertionError("The helper must never write Subsonic ratings")

class FakeMB:
    def __init__(self):
        self.writes = []
        self.fail = False
    def submit(self, body, endpoint="rating"):
        if self.fail:
            raise RuntimeError("temporary failure")
        self.writes.append((endpoint, ET.fromstring(body)))

class RatingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.nav = FakeSubsonic(SONG)
        self.mb = FakeMB()
    def run_worker(self, now=1000, **kwargs):
        return helper.run_worker(self.root, self.nav, self.mb, submit=True,
                                 account="account", interval=10, now=now, **kwargs)
    def job(self):
        helper.genres.write_json(self.root / "jobs/job.json", {"tracks": [TRACK]})
    def state(self):
        return json.loads((self.root / "state.json").read_text())
    def votes(self):
        return [int(tree.find(".//{*}user-rating").text) for _, tree in self.mb.writes]
    def test_unrated_import_stays_unrated_and_is_not_submitted(self):
        self.job()
        self.run_worker()
        self.assertEqual(self.nav.data["song1"]["userRating"], 0)
        self.assertEqual(self.mb.writes, [])
        self.assertFalse((self.root / "jobs/job.json").exists())
    def test_rated_import_submits_existing_rating_once(self):
        self.nav.data["song1"]["userRating"] = 4
        self.job()
        self.run_worker()
        self.run_worker(now=1011)
        self.assertEqual(self.votes(), [80])
        self.assertEqual(self.nav.data["song1"]["userRating"], 4)
    def test_observation_baseline_then_change_and_clear(self):
        self.nav.data["song1"]["userRating"] = 2
        self.run_worker()
        self.assertEqual(self.mb.writes, [])
        self.nav.data["song1"]["userRating"] = 4
        self.run_worker(now=1011)
        self.nav.data["song1"]["userRating"] = 0
        self.run_worker(now=1022)
        self.run_worker(now=1033)
        self.assertEqual(self.votes(), [80, 0])
    def test_explicit_library_pass_submits_existing_positive_ratings(self):
        self.nav.data["song1"]["userRating"] = 3
        self.run_worker(backfill=True)
        self.run_worker(now=1011, backfill=True)
        self.assertEqual(self.votes(), [60])
    def test_failed_submission_retries_without_subsonic_changes(self):
        self.nav.data["song1"]["userRating"] = 5
        self.mb.fail = True
        self.assertEqual(self.run_worker(backfill=True), 1)
        self.mb.fail = False
        self.run_worker(now=1011)
        self.assertEqual(self.votes(), [100])
        self.assertEqual(self.nav.data["song1"]["userRating"], 5)
    def test_failed_scan_never_withdraws_a_vote(self):
        self.nav.data["song1"]["userRating"] = 3
        self.run_worker(backfill=True)
        self.nav.songs = Mock(side_effect=RuntimeError("scan failed"))
        with self.assertRaises(RuntimeError):
            self.run_worker(now=1011)
        self.assertEqual(self.votes(), [60])
    def test_removed_song_never_withdraws_a_vote(self):
        self.nav.data["song1"]["userRating"] = 3
        self.run_worker(backfill=True)
        self.nav.data.clear()
        self.run_worker(now=1011)
        self.assertEqual(self.votes(), [60])
    def test_conflicting_duplicate_recording_ratings_are_not_submitted(self):
        self.nav = FakeSubsonic({**SONG, "userRating": 3}, {**SONG, "id": "song2", "userRating": 4})
        self.run_worker(backfill=True)
        self.assertEqual(self.mb.writes, [])
    def test_account_specific_history_is_preserved(self):
        self.nav.data["song1"]["userRating"] = 3
        self.run_worker(backfill=True)
        helper.run_worker(self.root, self.nav, self.mb, submit=True, account="other", interval=10, now=1011)
        self.assertEqual(self.votes(), [60, 60])
        self.assertEqual(len(list(self.root.glob("submitted-*.json"))), 2)
    def test_pending_old_generation_is_not_resumed(self):
        helper.genres.write_json(self.root / "state.json", {"version": 1, "last_scan": 0, "songs": {
            "song1": {"recording": RECORDING, "observed": 0, "source": "unverified",
                      "initialized": False, "pending_generation": {"value": 4, "source": "lastfm"},
                      "retry_track": TRACK}}})
        self.run_worker()
        self.assertEqual(self.nav.data["song1"]["userRating"], 0)
        self.assertEqual(self.mb.writes, [])
        self.assertNotIn("pending_generation", self.state()["songs"]["song1"])
    def test_known_musicbrainz_derived_rating_is_not_automatically_resubmitted(self):
        self.nav.data["song1"]["userRating"] = 3
        helper.genres.write_json(self.root / "state.json", {"version": 1, "last_scan": 0, "songs": {
            "song1": {"recording": RECORDING, "observed": 3, "source": "musicbrainz", "initialized": True}}})
        self.run_worker(backfill=True)
        self.assertEqual(self.mb.writes, [])
        self.nav.data["song1"]["userRating"] = 4
        self.run_worker(now=1011)
        self.assertEqual(self.votes(), [80])
    def test_import_rechecks_current_rating(self):
        self.job()
        self.nav.get_song = lambda _: {**SONG, "userRating": 5}
        self.run_worker()
        self.assertEqual(self.votes(), [100])
    def test_conflicting_import_identity_retains_job(self):
        self.job()
        self.nav.get_song = lambda _: {**SONG, "musicBrainzId": OTHER, "userRating": 5}
        self.run_worker()
        self.assertEqual(self.mb.writes, [])
        self.assertTrue((self.root / "jobs/job.json").exists())
    def test_periodic_scan_interval(self):
        self.run_worker()
        self.run_worker(now=1001)
        self.assertEqual(self.nav.scans, 1)
        self.assertEqual(helper.interval_seconds("15m"), 900)
        with self.assertRaises(ValueError):
            helper.interval_seconds("0m")
    def test_submission_disabled_preserves_observed_state(self):
        self.nav.data["song1"]["userRating"] = 3
        helper.run_worker(self.root, self.nav, self.mb, submit=False, backfill=True, now=1000)
        self.assertEqual(self.mb.writes, [])
        self.run_worker(now=1011)
        self.assertEqual(self.votes(), [60])
    def test_shared_submission_builder_handles_all_entities(self):
        body=ratings.build_submission([("artist", RECORDING, 40), ("release-group", RECORDING, 60), ("recording", RECORDING, 80)])
        tree=ET.fromstring(body)
        self.assertEqual([int(n.text) for n in tree.findall(".//{*}user-rating")], [40, 80, 60])
        self.assertEqual(ET.fromstring(helper.rating_xml(RECORDING, 4)).find(".//{*}user-rating").text, "80")
    def test_subsonic_client_is_read_only_and_uses_personal_stars(self):
        self.assertFalse(hasattr(helper.Subsonic, "set_rating"))
        self.assertEqual(helper.stars({"userRating": 2, "averageRating": 5}), 2)
        self.assertEqual(helper.stars({"averageRating": 5}), 0)
    def test_musicbrainz_generated_import_is_not_submitted_back(self):
        self.nav.data["song1"]["userRating"] = 4
        self.job()
        provenance = {"song1": {"value": 4, "source": "musicbrainz", "recording": RECORDING, "updated": 900}}
        self.run_worker(generated_ratings=provenance)
        self.assertEqual(self.votes(), [])
        self.assertEqual(self.state()["songs"]["song1"]["source"], "musicbrainz")
        self.run_worker(now=2000, backfill=True, generated_ratings=provenance)
        self.assertEqual(self.votes(), [])

    def test_other_generated_sources_submit_and_personal_changes_take_precedence(self):
        provenance = {"song1": {"value": 4, "source": "lastfm", "recording": RECORDING, "updated": 900}}
        self.nav.data["song1"]["userRating"] = 4
        self.run_worker(generated_ratings=provenance)
        self.assertEqual(self.votes(), [80])
        self.nav.data["song1"]["userRating"] = 3
        self.run_worker(now=2000, generated_ratings=provenance)
        self.assertEqual(self.votes(), [80, 60])
        self.nav.data["song1"]["userRating"] = 4
        self.run_worker(now=3000, generated_ratings=provenance)
        self.assertEqual(self.votes(), [80, 60, 80])
        self.assertEqual(self.state()["songs"]["song1"]["source"], "subsonic-change")

    def test_provenance_for_another_recording_does_not_change_source(self):
        self.nav.data["song1"]["userRating"] = 4
        provenance = {"song1": {"value": 4, "source": "lastfm", "recording": OTHER, "updated": 900}}
        self.run_worker(generated_ratings=provenance)
        self.assertEqual(self.votes(), [])
        self.assertEqual(self.state()["songs"]["song1"]["source"], "unverified")

    def test_helper_imports_with_sonicratings_unavailable(self):
        code="""import importlib.abc,sys
class Block(importlib.abc.MetaPathFinder):
 def find_spec(self,fullname,path=None,target=None):
  if fullname.startswith('sonicratings'): raise ModuleNotFoundError(fullname)
sys.meta_path.insert(0,Block())
sys.path.insert(0,sys.argv[1])
from musicbrainz_helper import ratings,ratings_worker,musicbrainz_genres
"""
        result=subprocess.run([sys.executable,"-I","-c",code,str(Path(__file__).parents[1])],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_identity_match_rejects_ambiguous_songs_and_wrong_recordings(self):
        self.assertEqual(helper.find_song(TRACK, {"1": SONG}), SONG)
        self.assertIsNone(helper.find_song(TRACK, {"1": SONG, "2": {**SONG, "id": "song2"}}))
        self.assertIsNone(helper.find_song(TRACK, {"1": {**SONG, "musicBrainzId": OTHER}}))
        self.assertIsNone(helper.find_song(TRACK, {"1": {**SONG, "path": "elsewhere.flac", "title": "Song (Live)"}}))

    def test_subsonic_uses_salted_auth_and_checks_api_errors(self):
        nav = helper.Subsonic("https://example.test/music", "user", "fake-secret")
        response = Mock(ok=True)
        response.json.return_value = {"subsonic-response": {"status": "ok", "song": SONG}}
        nav.session.get = Mock(return_value=response)
        self.assertEqual(nav.get_song("song1"), SONG)
        params = nav.session.get.call_args.kwargs["params"]
        self.assertNotIn("p", params)
        self.assertNotIn("fake-secret", str(params))
        self.assertIn("t", params)
        response.json.return_value = {"subsonic-response": {"status": "failed", "error": {"code": 40}}}
        with self.assertRaisesRegex(RuntimeError, "code 40"):
            nav.get_song("song1")

    def test_full_library_pagination_and_deduplication(self):
        nav = helper.Subsonic("https://example.test", "user", "fake")
        def call(endpoint, **params):
            if endpoint == "getAlbumList2":
                return {"albumList2": {"album": [{"id": str(i)} for i in range(500)] if params["offset"] == 0 else [{"id": "500"}]}}
            return {"album": {"song": [{**SONG, "id": params["id"]}]}}
        nav.call = Mock(side_effect=call)
        self.assertEqual(len(nav.songs()), 501)
        self.assertEqual(nav.call.call_args_list[-2].kwargs["offset"], 500)

    def test_import_snapshot_uses_embedded_recording_and_rejects_wrong_group(self):
        from types import SimpleNamespace
        group = "00000000-0000-4000-8000-000000000001"
        tags = {"musicbrainz_trackid": [RECORDING], "musicbrainz_releasegroupid": [group],
                "title": ["Song"], "artist": ["Artist"], "album": ["Album"]}
        fetch = lambda endpoint: {"foreignAlbumId": group, "title": "Album"} if endpoint.startswith("album/") else [{"path": TRACK["path"]}]
        self.assertEqual(helper.enqueue(self.root, 1, fetch, lambda path: SimpleNamespace(tags=tags)), 1)
        job = json.loads(next((self.root / "jobs").glob("*.json")).read_text())
        self.assertEqual(job["tracks"][0]["recording"], RECORDING)
        tags["musicbrainz_releasegroupid"] = [OTHER]
        self.assertEqual(helper.enqueue(self.root, 1, fetch, lambda path: SimpleNamespace(tags=tags)), 0)

    def test_shared_musicbrainz_client_posts_rating_endpoint(self):
        from musicbrainz_helper import musicbrainz
        mb = musicbrainz.MusicBrainzClient(musicbrainz.JsonCache(self.root / "cache.json"), "user", "fake-token")
        response = Mock(status_code=200)
        mb.session.post = Mock(return_value=response)
        mb.submit(helper.rating_xml(RECORDING, 4), endpoint="rating")
        self.assertEqual(mb.session.post.call_args.args[0], "https://musicbrainz.org/ws/2/rating")
        self.assertEqual(mb.session.auth.password, "fake-token")

    def test_musicbrainz_gate_and_cooldown_are_shared_between_accounts(self):
        base = self.root / "musicbrainz-helper"
        with patch.object(helper.genres.time, "time", return_value=100), patch.object(helper.genres.time, "sleep") as sleep:
            with helper.genres.musicbrainz_lock(base / "profiles/account1"):
                pass
            with helper.genres.musicbrainz_lock(base / "profiles/account2"):
                pass
            self.assertAlmostEqual(sleep.call_args.args[0], 1.1)
        self.assertTrue((base / "worker-cooldown.json").exists())
        self.assertTrue((base / "worker.lock").exists())

    def test_busy_musicbrainz_worker_is_a_clean_service_retry(self):
        with patch.dict(os.environ, {"SUBSONIC_URL": "http://navidrome", "SUBSONIC_USERNAME": "user",
                                     "SUBSONIC_PASSWORD": "password"}), \
                patch.object(sys, "argv", ["musicbrainz-ratings", "work", "--state-dir", str(self.root / "ratings")]), \
                patch.object(helper, "Subsonic", return_value=Mock()), \
                patch.object(helper.genres, "musicbrainz_lock", side_effect=helper.genres.WorkerBusy("busy")), \
                self.assertLogs(level="INFO") as logs:
            self.assertEqual(helper.main(), 0)
        self.assertIn("worker already active", "\n".join(logs.output))

    def test_subsonic_environment_names_preserve_legacy_settings(self):
        legacy = {"NAVIDROME_URL": "https://server.example.test/", "NAVIDROME_USERNAME": "listener",
                  "NAVIDROME_PASSWORD": "fake-secret"}
        with patch.dict(os.environ, legacy, clear=True):
            before = helper.subsonic_settings()
        renamed = {key.replace("NAVIDROME_", "SUBSONIC_"): value for key, value in legacy.items()}
        with patch.dict(os.environ, renamed, clear=True):
            self.assertEqual(helper.subsonic_settings(), before)
        with patch.dict(os.environ, {**legacy, **renamed, "SUBSONIC_USERNAME": "new-user"}, clear=True):
            self.assertEqual(helper.subsonic_settings(), ("https://server.example.test", "new-user", "fake-secret"))

    def test_legacy_rating_change_provenance_is_still_eligible(self):
        self.nav.data["song1"]["userRating"] = 4
        helper.genres.write_json(self.root / "state.json", {"version": 1, "last_scan": 0, "songs": {
            "song1": {"recording": RECORDING, "observed": 4, "initialized": True,
                      "source": "navidrome-change", "updated": 900}}})
        self.run_worker()
        self.assertEqual(self.votes(), [80])
        self.nav.data["song1"]["userRating"] = 5
        self.run_worker(now=1011)
        self.assertEqual(self.state()["songs"]["song1"]["source"], "subsonic-change")

if __name__ == "__main__":
    unittest.main()
