import importlib.util
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import sys
from musicbrainz_helper import musicbrainz_genres as helper

GROUP = "00000000-0000-4000-8000-000000000001"
RELEASE = "00000000-0000-4000-8000-000000000002"
OTHER_RELEASE = "00000000-0000-4000-8000-000000000003"
RECORDING = "00000000-0000-4000-8000-000000000004"
REMIX = "00000000-0000-4000-8000-000000000005"


class FakeClient:
    calls = []

    def __init__(self, *args):
        pass

    def genres(self):
        return {"rock": "rock", "pop": "pop"}

    def release_group(self, group):
        assert group == GROUP
        return {"releases": [
            {"id": RELEASE, "title": "Single", "media": [{"tracks": [
                {"recording": {"id": RECORDING, "title": "Song"}},
                {"recording": {"id": REMIX, "title": "Song"}},
            ]}]},
            {"id": OTHER_RELEASE, "title": "Single (other edition)", "media": [{"tracks": [
                {"recording": {"id": RECORDING, "title": "Song"}},
            ]}]},
        ]}

    def submit(self, body):
        self.calls.append(body)


class ImportedGenresTests(unittest.TestCase):
    def test_app_token_is_used_as_digest_secret(self):
        from requests.auth import HTTPDigestAuth
        with tempfile.TemporaryDirectory() as directory:
            cache = helper.core.JsonCache(Path(directory) / "cache.json")
            client = helper.core.MusicBrainzClient(cache, "test-user", "application-token")
            self.assertIsInstance(client.session.auth, HTTPDigestAuth)
            self.assertEqual(client.session.auth.username, "test-user")
            self.assertEqual(client.session.auth.password, "application-token")

    def test_automatic_account_profile_handles_usernames_and_avoids_collisions(self):
        self.assertEqual(helper.account_profile(""), "local")
        first = helper.account_profile("A User/Name")
        self.assertTrue(first.startswith("A_User_Name-"))
        self.assertNotEqual(first, helper.account_profile("A_User_Name"))
        self.assertNotEqual(first, helper.account_profile("a user/name"))
        root = Path("queue")
        self.assertEqual(helper.profile_root(root, first).parent, root / "profiles")

    def test_named_profiles_are_isolated_and_legacy_queue_is_preserved(self):
        root = Path("queue")
        self.assertEqual(helper.profile_root(root, None), root)
        self.assertEqual(helper.profile_root(root, "sickprodigy"), root / "profiles" / "sickprodigy")
        self.assertNotEqual(helper.profile_root(root, "alice"), helper.profile_root(root, "bob"))
        for invalid in ("../alice", "/alice", "..", "alice/bob"):
            with self.assertRaises(ValueError):
                helper.profile_root(root, invalid)

    def test_library_scan_exports_raw_genres_and_empty_files_without_mb_requests(self):
        albums = [{"id": number, "foreignAlbumId": GROUP, "title": "Single"} for number in (1, 2, 3)]
        def fetch(endpoint):
            if endpoint == "album":
                return albums
            return [{"path": endpoint}] if not endpoint.endswith("3") else []
        def read(path):
            return SimpleNamespace(tags={
                "title": ["Song"], "musicbrainz_trackid": [RECORDING],
                "musicbrainz_albumid": [RELEASE],
                "genre": ["Rock", "My custom genre"] if path.endswith("1") else [],
            })
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = helper.scan_library(fetch, root, "embedded-beets", read)
            self.assertEqual((result["queued"], result["missing"], len(result["albums"])), (1, 1, 2))
            self.assertEqual(result["failures"], [])
            self.assertEqual(result["albums"][0]["files"][0]["tags"]["genre"], ["Rock", "My custom genre"])
            self.assertEqual(result["albums"][1]["files"][0]["tags"]["genre"], [])
            export = next((root / "library-snapshots").glob("*.json"))
            self.assertEqual(json.loads(export.read_text()), result)
            def retagged(path):
                return SimpleNamespace(tags={"genre": ["Pop"], "musicbrainz_albumid": [RELEASE]})
            helper.scan_library(fetch, root, "embedded-beets", retagged)
            self.assertEqual(len(list((root / "library-snapshots").glob("*.json"))), 2)
            self.assertEqual(len(list((root / "jobs").glob("*.json"))), 3)

    def test_library_scan_keeps_other_albums_when_one_file_is_unreadable(self):
        albums = [{"id": number, "foreignAlbumId": GROUP} for number in (1, 2)]
        def fetch(endpoint):
            return albums if endpoint == "album" else [{"path": endpoint}]
        def read(path):
            if path.endswith("1"):
                raise OSError("file unreadable")
            return SimpleNamespace(tags={"genre": ["Rock"]})
        with tempfile.TemporaryDirectory() as directory:
            result = helper.scan_library(fetch, Path(directory), "embedded-provider", read)
            self.assertEqual(result["queued"], 1)
            self.assertEqual(result["failures"][0]["album_id"], "1")

    def test_artist_contribution_requires_explicit_target(self):
        job = self.job()
        job["artist"] = REMIX
        rows, _ = helper.targets(job, FakeClient(), 7, ["release-group"])
        self.assertFalse(any(row["entity"] == "artist" for row in rows))
        rows, _ = helper.targets(job, FakeClient(), 7, ["artist"])
        self.assertEqual(rows[0]["mbid"], REMIX)
        self.assertEqual(rows[0]["genres"], ["rock"])

    def job(self):
        return {"version": 1, "album_id": "12", "release_group": GROUP,
                "name": "Single", "source": "embedded-beets", "tracks": [
                    {"title": "Song", "recording": RECORDING, "release": RELEASE,
                     "genres": ["Rock", "my favorite"]}
                ]}

    def test_single_expands_releases_without_tagging_same_named_remix(self):
        rows, unmatched = helper.targets(self.job(), FakeClient(), 7, ["release-group", "release", "recording"])
        self.assertEqual({(row["entity"], row["mbid"]) for row in rows}, {
            ("release-group", GROUP), ("release", RELEASE), ("release", OTHER_RELEASE),
            ("recording", RECORDING),
        })
        self.assertEqual(unmatched, ["my favorite"])
        self.assertTrue(all(row["genres"] == ["rock"] for row in rows))

    def test_conflicting_release_is_rejected(self):
        job = self.job()
        job["tracks"][0]["release"] = REMIX
        rows, _ = helper.targets(job, FakeClient(), 7, ["release-group", "release", "recording"])
        self.assertEqual(rows, [])

    def test_snapshot_skips_conflicting_groups_and_coalesces_reordered_files(self):
        def tags(path):
            return SimpleNamespace(tags={
                "musicbrainz_releasegroupid": [REMIX if path == "wrong.flac" else GROUP],
                "musicbrainz_albumid": [RELEASE], "musicbrainz_trackid": [RECORDING],
                "genre": ["Rock; Pop", "Rock"], "title": ["Song"],
            })
        album = {"id": 12, "title": "Single", "foreignAlbumId": GROUP}
        files = [{"path": name} for name in ["a.flac", "wrong.flac", "b.flac"]]
        first = helper.snapshot(album, files, "embedded-provider", tags)
        second = helper.snapshot(album, list(reversed(files)), "embedded-provider", tags)
        self.assertEqual(first, second)
        self.assertEqual(len(first["tracks"]), 1)
        self.assertEqual(first["tracks"][0]["genres"], ["Pop", "Rock"])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(helper.enqueue(root, first), helper.enqueue(root, second))
            self.assertEqual(len(list((root / "jobs").glob("*.json"))), 1)

    def test_preview_then_submit_and_repeated_event_preserve_ledger(self):
        FakeClient.calls = []
        with tempfile.TemporaryDirectory() as directory, patch.object(helper.core, "MusicBrainzClient", FakeClient), \
                patch.dict(os.environ, {"MB_USERNAME": "test-account", "MB_TOKEN": "test-password"}):
            root = Path(directory)
            job = self.job()
            helper.enqueue(root, job)
            self.assertEqual(helper.process(root, False, 7, ["release-group", "release", "recording"]), 0)
            self.assertEqual(FakeClient.calls, [])
            self.assertEqual(helper.process(root, True, 7, ["release-group", "release", "recording"]), 0)
            self.assertEqual(len(FakeClient.calls), 1)
            # A changed source creates a fresh snapshot but must not repeat accepted votes.
            job["source"] = "embedded-provider"
            helper.enqueue(root, job)
            helper.process(root, True, 7, ["release-group", "release", "recording"])
            self.assertEqual(len(FakeClient.calls), 1)
            ledger = json.loads(next(root.glob("submitted-votes-*.json")).read_text())
            self.assertEqual(len(ledger["votes"]), 4)

    def test_failed_vote_is_retained_and_retried(self):
        class FailingClient(FakeClient):
            def submit(self, body):
                raise RuntimeError("temporary failure")
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, {"MB_USERNAME": "test", "MB_TOKEN": "test"}):
            root = Path(directory)
            helper.enqueue(root, self.job())
            with patch.object(helper.core, "MusicBrainzClient", FailingClient):
                self.assertEqual(helper.process(root, True, 7, ["recording"]), 1)
            self.assertEqual(list(root.glob("reports/**/complete.json")), [])
            with patch.object(helper.core, "MusicBrainzClient", FakeClient):
                self.assertEqual(helper.process(root, True, 7, ["recording"]), 0)
            self.assertEqual(len(list(root.glob("reports/**/complete.json"))), 1)


if __name__ == "__main__":
    unittest.main()
