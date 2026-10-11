import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET

from musicbrainz_helper import ratings_helper as helper
from sonicratings_engine.ratings import score_lastfm

RECORDING = "00000000-0000-4000-8000-000000000004"
OTHER = "00000000-0000-4000-8000-000000000005"
SONG = {"id": "song1", "musicBrainzId": RECORDING, "userRating": 0,
        "artist": "Artist", "album": "Album", "title": "Song", "path": "Artist/Album/Song.flac"}
TRACK = {"recording": RECORDING, "artist": "Artist", "album": "Album", "title": "Song",
         "path": "/music/Artist/Album/Song.flac"}


class FakeSubsonic:
    def __init__(self, *songs):
        self.data = {str(song["id"]): copy.deepcopy(song) for song in songs}
        self.writes = []
        self.fail = False
        self.lost_response = False
        self.scans = 0

    def songs(self):
        self.scans += 1
        if self.fail:
            raise RuntimeError("Unavailable")
        return copy.deepcopy(self.data)

    def get_song(self, song_id):
        return copy.deepcopy(self.data[song_id])

    def set_rating(self, song_id, rating):
        self.data[song_id]["userRating"] = rating
        self.writes.append((song_id, rating))
        if self.lost_response:
            raise RuntimeError("Timeout after write")


class FakeMB:
    def __init__(self):
        self.writes = []
        self.fail = False

    def submit(self, payload, endpoint="tag"):
        if self.fail:
            raise RuntimeError("Unavailable")
        self.writes.append((endpoint, ET.fromstring(payload)))


class RatingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.nav = FakeSubsonic(SONG)
        self.mb = FakeMB()
        self.provider = Mock()
        self.provider.rating.return_value = (4, "lastfm")

    def job(self):
        helper.genres.write_json(self.root / "jobs/job.json", {"tracks": [TRACK]})

    def run_worker(self, now=1000, **kwargs):
        return helper.run_worker(self.root, self.nav, self.provider, self.mb,
                                 submit=True, account="account", interval=10, now=now, **kwargs)

    def state(self):
        return json.loads((self.root / "state.json").read_text())

    def submitted_values(self):
        return [int(tree.find(".//{*}user-rating").text) for _, tree in self.mb.writes]

    def test_lastfm_import_rates_then_submits_once(self):
        self.job()
        self.assertEqual(self.run_worker(), 0)
        self.assertEqual(self.nav.writes, [("song1", 4)])
        self.assertEqual(self.submitted_values(), [80])
        self.assertEqual(self.mb.writes[0][0], "rating")
        self.run_worker(now=1011)
        self.assertEqual(self.submitted_values(), [80])
        self.assertEqual(self.state()["songs"]["song1"]["source"], "lastfm")
        self.assertFalse((self.root / "jobs/job.json").exists())

    def test_musicbrainz_source_stays_local_then_manual_change_and_clear_sync(self):
        self.provider.rating.return_value = (3, "musicbrainz")
        self.job()
        self.run_worker()
        self.assertEqual(self.nav.writes, [("song1", 3)])
        self.assertEqual(self.mb.writes, [])
        self.nav.data["song1"]["userRating"] = 5
        self.run_worker(now=1011)
        self.nav.data["song1"]["userRating"] = 0
        self.run_worker(now=1022)
        self.run_worker(now=1033)
        self.assertEqual(self.submitted_values(), [100, 0])
        self.assertEqual(self.provider.rating.call_count, 1)

    def test_clearing_musicbrainz_derived_rating_does_not_withdraw_unknown_vote(self):
        self.provider.rating.return_value = (3, "musicbrainz")
        self.job()
        self.run_worker()
        self.nav.data["song1"]["userRating"] = 0
        self.run_worker(now=1011)
        self.assertEqual(self.mb.writes, [])

    def test_existing_ratings_baseline_then_changes_submit_without_overwrite(self):
        self.nav.data["song1"]["userRating"] = 2
        self.job()
        self.run_worker()
        self.assertEqual(self.mb.writes, [])
        self.assertEqual(self.nav.writes, [])
        self.provider.rating.assert_not_called()
        self.nav.data["song1"]["userRating"] = 4
        self.run_worker(now=1011)
        self.assertEqual(self.submitted_values(), [80])

    def test_rating_changed_between_scan_and_write_is_preserved(self):
        self.job()
        self.nav.get_song = lambda song_id: {**self.nav.data[song_id], "userRating": 5}
        self.run_worker()
        self.assertEqual(self.nav.writes, [])
        self.assertEqual(self.submitted_values(), [100])

    def test_waits_for_subsonic_scan(self):
        self.nav.data = {}
        self.job()
        self.run_worker()
        self.assertTrue((self.root / "jobs/job.json").exists())
        self.nav.data["song1"] = copy.deepcopy(SONG)
        self.run_worker(now=1001)
        self.assertEqual(self.submitted_values(), [80])

    def test_submission_failure_retains_pending_value_for_periodic_retry(self):
        self.mb.fail = True
        self.job()
        self.assertEqual(self.run_worker(), 1)
        self.mb.fail = False
        self.run_worker(now=1011)
        self.assertEqual(self.submitted_values(), [80])
        self.assertEqual(self.nav.writes, [("song1", 4)])

    def test_missing_song_and_failed_scan_do_not_withdraw(self):
        self.job()
        self.run_worker()
        self.nav.data = {}
        self.run_worker(now=1011)
        self.assertEqual(self.submitted_values(), [80])
        self.nav.fail = True
        with self.assertRaises(RuntimeError):
            self.run_worker(now=1022)
        self.assertEqual(self.submitted_values(), [80])

    def test_lost_musicbrainz_generated_write_response_keeps_provenance(self):
        self.provider.rating.return_value = (3, "musicbrainz")
        self.nav.lost_response = True
        self.job()
        self.assertEqual(self.run_worker(), 1)
        self.nav.lost_response = False
        self.run_worker(now=1001)
        self.assertEqual(self.mb.writes, [])
        self.assertEqual(self.nav.writes, [("song1", 3)])
        self.assertEqual(self.state()["songs"]["song1"]["source"], "musicbrainz")

    def test_library_provider_failure_retries_without_an_import_job(self):
        self.provider.rating.side_effect = RuntimeError("Unavailable")
        self.assertEqual(self.run_worker(backfill=True), 1)
        self.provider.rating.side_effect = None
        self.run_worker(now=1011)
        self.assertEqual(self.submitted_values(), [80])

    def test_different_accounts_have_independent_submission_ledgers(self):
        self.job()
        self.run_worker()
        helper.run_worker(self.root, self.nav, self.provider, self.mb,
                          submit=True, account="other-account", interval=1, now=1011)
        self.assertEqual(self.submitted_values(), [80, 80])

    def test_conflicting_copies_of_same_recording_are_not_submitted(self):
        self.job()
        self.run_worker()
        self.nav.data["song2"] = {**SONG, "id": "song2", "userRating": 2}
        self.nav.data["song1"]["userRating"] = 5
        self.run_worker(now=1011)
        self.assertEqual(self.submitted_values(), [80])

    def test_average_rating_is_never_a_personal_rating(self):
        self.assertEqual(helper.stars({"averageRating": 5, "rating": 4}), 0)
        self.assertEqual(helper.stars({"userRating": "3"}), 3)
        with self.assertRaises(ValueError):
            helper.stars({"userRating": 6})

    def test_identity_match_rejects_ambiguous_songs_and_wrong_recordings(self):
        self.assertEqual(helper.find_song(TRACK, {"1": SONG}), SONG)
        self.assertIsNone(helper.find_song(TRACK, {"1": SONG, "2": {**SONG, "id": "song2"}}))
        self.assertIsNone(helper.find_song(TRACK, {"1": {**SONG, "musicBrainzId": OTHER}}))
        self.assertIsNone(helper.find_song(TRACK, {"1": {**SONG, "path": "elsewhere.flac", "title": "Song (Live)"}}))

    def test_no_rating_remains_unrated_and_explicit_library_can_recheck(self):
        self.provider.rating.return_value = None
        self.job()
        self.run_worker()
        self.assertEqual(self.nav.writes, [])
        self.provider.rating.return_value = (3, "lastfm")
        self.run_worker(now=1011, backfill=True)
        self.assertEqual(self.submitted_values(), [60])

    def test_interval_avoids_unnecessary_full_scans(self):
        self.run_worker()
        self.run_worker(now=1001)
        self.assertEqual(self.nav.scans, 1)
        self.assertEqual(helper.interval_seconds("15m"), 900)
        with self.assertRaises(ValueError):
            helper.interval_seconds("0m")

    def test_musicbrainz_provider_uses_exact_recording_not_title_search(self):
        mb = Mock()
        mb.request.return_value = {"rating": {"value": 3.8}}
        provider = helper.RatingProvider(mb)
        self.assertEqual(provider.rating(TRACK), (4, "musicbrainz"))
        mb.request.assert_called_once_with(f"recording/{RECORDING}", inc="ratings")
        mb.request.return_value = {"rating": None}
        self.assertIsNone(provider.rating(TRACK))

    def test_lastfm_scoring_and_version_checks(self):
        self.assertEqual(score_lastfm(1000000, 10000000, 1), 5)
        self.assertLessEqual(score_lastfm(100, 1000, 100), 3)
        provider = helper.RatingProvider(Mock(), "fake-key")
        provider.lastfm = Mock(return_value={"track": {"name": "Song (Live)", "artist": {"name": "Artist"}}})
        self.assertIsNone(provider.rating(TRACK))
        provider.lastfm.return_value = {"track": {"name": "Song", "artist": {"name": "Artist"}, "mbid": OTHER}}
        self.assertIsNone(provider.rating(TRACK))

    def test_lastfm_verified_track_and_artist_rank_generate_stars(self):
        provider = helper.RatingProvider(Mock(), "fake-key")
        def api(method, **params):
            if method == "track.getInfo":
                return {"track": {"name": "Song", "artist": {"name": "Artist"}, "mbid": RECORDING,
                                  "listeners": "1000000", "playcount": "10000000"}}
            return {"toptracks": {"track": [{"name": "Song", "listeners": "1000000", "playcount": "10000000"}]}}
        provider.lastfm = Mock(side_effect=api)
        self.assertEqual(provider.rating(TRACK), (5, "lastfm"))
        self.assertEqual(provider.lastfm.call_args_list[0].kwargs["mbid"], RECORDING)

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
        self.assertEqual(self.submitted_values(), [80])
        self.nav.data["song1"]["userRating"] = 5
        self.run_worker(now=1011)
        self.assertEqual(self.state()["songs"]["song1"]["source"], "subsonic-change")


if __name__ == "__main__":
    unittest.main()
