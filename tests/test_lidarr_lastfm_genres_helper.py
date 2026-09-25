import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

import requests


SCRIPT = Path(__file__).parents[1] / "lidarr-lastfm-genres-helper.py"
SPEC = importlib.util.spec_from_file_location("lidarr_lastfm_genres_helper", SCRIPT)
helper = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = helper
SPEC.loader.exec_module(helper)


class LidarrLastFmGenresHelperTests(unittest.TestCase):
    def test_musicbrainz_user_agent_identifies_name_and_version(self):
        self.assertEqual(helper.CLIENT, "musicbrainz-helper/0.2")

    def test_filters_ranked_tags_to_musicbrainz_genres(self):
        tags = [
            {"name": "seen live", "count": 100},
            {"name": "Hip-Hop", "count": 90},
            {"name": "hip hop", "count": 80},
            {"name": "Alternative Rock", "count": 70},
        ]
        vocabulary = {"hiphop": "hip hop", "alternativerock": "alternative rock"}
        self.assertEqual(
            helper.filtered_genres(tags, vocabulary, 7),
            ["hip hop", "alternative rock"],
        )

    def test_missing_albums_are_paginated(self):
        client = helper.LidarrClient("http://lidarr", "key")
        client.get = Mock(
            side_effect=[
                {"records": [{"id": 1}], "totalRecords": 2},
                {"records": [{"id": 2}], "totalRecords": 2},
            ]
        )
        self.assertEqual([row["id"] for row in client.albums("missing")], [1, 2])
        self.assertEqual(client.get.call_count, 2)

    def test_missing_album_limit_stops_pagination_early(self):
        client = helper.LidarrClient("http://lidarr", "key")
        client.get = Mock(return_value={"records": [{"id": 1}, {"id": 2}], "totalRecords": 1000})

        self.assertEqual([row["id"] for row in client.albums("missing", 2)], [1, 2])
        client.get.assert_called_once_with(
            "wanted/missing",
            page=1,
            pageSize=2,
            sortKey="title",
            sortDirection="ascending",
            includeArtist="true",
        )

    def test_present_scope_excludes_wanted_missing_albums(self):
        client = helper.LidarrClient("http://lidarr", "key")
        client.missing_albums = Mock(return_value=[{"id": 2}])
        client.all_albums = Mock(return_value=[{"id": 1}, {"id": 2}, {"id": 3}])
        self.assertEqual([row["id"] for row in client.albums("present")], [1, 3])

    def test_resume_selection_starts_at_album_and_skips_completed_ids(self):
        albums = [
            {"id": 1, "foreignAlbumId": "rg-1"},
            {"id": 2, "foreignAlbumId": "rg-2"},
            {"id": 3, "foreignAlbumId": "rg-3"},
        ]
        selected = helper.selected_albums(albums, start_album_id="rg-2", completed={"2"})
        self.assertEqual([row["id"] for row in selected], [3])

    def test_lastfm_prefers_mbid_without_name_parameters(self):
        cache = Mock()
        cache.get.return_value = None
        client = helper.LastFmClient("key", cache)
        client._request = Mock(return_value={"toptags": {"tag": [{"name": "rock", "count": 5}]}})
        tags = client.top_tags("artist", "Ambiguous Name", mbid="artist-mbid")
        self.assertEqual(tags[0]["name"], "rock")
        params = client._request.call_args.args[0]
        self.assertEqual(params["mbid"], "artist-mbid")
        self.assertNotIn("artist", params)

    def test_lastfm_info_tags_require_matching_mbid(self):
        cache = Mock()
        cache.get.return_value = {
            "mbid": "matching-release",
            "tags": {"tag": [{"name": "hip hop"}]},
        }
        client = helper.LastFmClient("key", cache)
        tags, source = client.verified_info_tags(
            "album", "Artist", "Album", {"matching-release"}
        )
        self.assertEqual(tags, [{"name": "hip hop"}])
        self.assertEqual(source, "lastfm:album-verified")

    def test_lastfm_info_tags_reject_ambiguous_mbid(self):
        cache = Mock()
        cache.get.return_value = {
            "mbid": "different-release",
            "tags": {"tag": [{"name": "doom metal"}]},
        }
        client = helper.LastFmClient("key", cache)
        tags, source = client.verified_info_tags(
            "album", "Artist", "Album", {"wanted-release"}
        )
        self.assertEqual(tags, [])
        self.assertEqual(source, "lastfm:album-unmatched")

    def test_run_state_persists_completed_albums_and_artists(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "progress.json"
            state = helper.RunState(path, resume=False)
            state.seen_artists.add("artist-mbid")
            state.checkpoint(["album-id"])
            resumed = helper.RunState(path, resume=True)
            self.assertEqual(resumed.completed, {"album-id"})
            self.assertEqual(resumed.seen_artists, {"artist-mbid"})

    def test_submission_failure_isolated_to_one_vote(self):
        rows = [
            {"entity": "artist", "mbid": "good", "name": "Good", "genres": ["rock"], "sources": []},
            {"entity": "artist", "mbid": "bad", "name": "Bad", "genres": ["pop"], "sources": []},
        ]
        musicbrainz = Mock()

        def submit(body):
            if b'id="bad"' in body:
                raise requests.HTTPError("bad target")

        musicbrainz.submit.side_effect = submit
        failures = []
        with tempfile.TemporaryDirectory() as directory:
            ledger = helper.SubmissionLedger(Path(directory) / "submitted.json")
            submitted = helper.submit_resilient(musicbrainz, rows, ledger, failures)
            self.assertEqual(submitted, 1)
            self.assertEqual(len(failures), 1)
            self.assertEqual(failures[0]["mbid"], "bad")
            self.assertEqual(len(ledger.votes), 1)

    def test_submission_deduplicates_votes_and_ledger_skips_repeats(self):
        row = {"entity": "artist", "mbid": "artist-id", "name": "Artist", "genres": ["rock"], "sources": []}
        musicbrainz = Mock()
        with tempfile.TemporaryDirectory() as directory:
            ledger = helper.SubmissionLedger(Path(directory) / "submitted.json")
            self.assertEqual(helper.submit_resilient(musicbrainz, [row, row], ledger, []), 1)
            self.assertEqual(musicbrainz.submit.call_count, 1)
            self.assertEqual(helper.submit_resilient(musicbrainz, [row], ledger, []), 0)
            self.assertEqual(musicbrainz.submit.call_count, 1)

    def test_musicbrainz_genres_are_paginated(self):
        cache = Mock()
        cache.get.return_value = None
        client = helper.MusicBrainzClient(cache)
        client.request = Mock(
            side_effect=[
                {"genres": [{"name": "rock"}], "genre-count": 2},
                {"genres": [{"name": "pop"}], "genre-count": 2},
            ]
        )
        self.assertEqual(client.genres(), {"rock": "rock", "pop": "pop"})
        self.assertEqual(client.request.call_count, 2)

    def test_submission_xml_supports_every_musicbrainz_entity(self):
        rows = [
            {"entity": entity, "mbid": f"{entity}-id", "name": "Example", "genres": ["rock"], "sources": []}
            for entity in helper.ENTITY_LISTS
        ]
        xml = helper.submission_xml(rows).decode()
        for entity in helper.ENTITY_LISTS:
            self.assertIn(f'<{entity} id="{entity}-id">', xml)
        self.assertEqual(xml.count('<user-tag vote="upvote">'), 4)

    def test_submission_xml_consolidates_votes_for_the_same_entity(self):
        rows = [
            {"entity": "artist", "mbid": "artist-id", "genres": ["rock"]},
            {"entity": "artist", "mbid": "artist-id", "genres": ["pop"]},
        ]
        xml = helper.submission_xml(rows).decode()
        self.assertEqual(xml.count('<artist id="artist-id">'), 1)
        self.assertEqual(xml.count('<user-tag vote="upvote">'), 2)


if __name__ == "__main__":
    unittest.main()
