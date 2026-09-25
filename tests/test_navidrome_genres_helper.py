import importlib.util
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import Mock, patch

import requests


SCRIPT = Path(__file__).parents[1] / "navidrome-genres-helper.py"
SPEC = importlib.util.spec_from_file_location("navidrome_genres_helper", SCRIPT)
helper = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = helper
SPEC.loader.exec_module(helper)


class NavidromeGenresHelperTests(unittest.TestCase):
    def test_navidrome_auth_uses_a_salted_token(self):
        client = helper.NavidromeClient("http://navidrome", "user", "secret")
        self.assertNotIn("p", client.auth)
        self.assertIn("t", client.auth)
        self.assertIn("s", client.auth)
        self.assertNotIn("secret", str(client.auth))

    @patch.object(helper.time, "sleep")
    def test_navidrome_connection_error_does_not_expose_credentials(self, _sleep):
        client = helper.NavidromeClient("http://navidrome", "user", "secret")
        client.session.get = Mock(side_effect=requests.ConnectionError("secret URL"))
        with self.assertRaises(SystemExit) as raised:
            client.request("getAlbumList2")
        self.assertNotIn("secret", str(raised.exception))

    def test_reads_legacy_and_opensubsonic_genres(self):
        item = {
            "genre": "Hip-Hop; Alternative Rock",
            "genres": [{"name": "Hip-Hop"}, {"name": "East Coast"}],
        }
        self.assertEqual(helper.item_genres(item), ["Hip-Hop", "Alternative Rock", "East Coast"])

    def test_filters_and_reports_unmatched_genres(self):
        unmatched = Counter()
        genres = helper.accepted_genres(
            ["Hip-Hop", "seen live", "Alternative Rock"],
            {"hiphop": "hip hop", "alternativerock": "alternative rock"},
            unmatched,
        )
        self.assertEqual(genres, ["hip hop", "alternative rock"])
        self.assertEqual(unmatched, Counter({"seen live": 1}))

    def test_aggregates_and_ranks_genres_per_entity(self):
        targets = {}
        helper.add_target(targets, "artist", "artist-id", "Example", "Example", ["rock", "pop"], "album")
        helper.add_target(targets, "artist", "artist-id", "Example", "Example", ["rock", "jazz"], "song")
        rows = helper.ranked_rows(targets, 2)
        self.assertEqual(rows[0]["genres"], ["rock", "pop"])
        self.assertEqual(rows[0]["sources"], ["album", "song"])

    def test_album_limit_controls_first_page_size(self):
        client = helper.NavidromeClient("http://navidrome", "user", "password")
        client.request = Mock(return_value={"albumList2": {"album": [{"id": "one"}, {"id": "two"}]}})
        self.assertEqual([row["id"] for row in client.iter_albums(limit=2)], ["one", "two"])
        client.request.assert_called_once_with(
            "getAlbumList2", type="alphabeticalByName", size=2, offset=0
        )

    def test_release_group_resolution_is_cached(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = helper.JsonCache(Path(directory) / "cache.json")
            client = helper.MusicBrainzClient(cache)
            client.request = Mock(return_value={"release-group": {"id": "group-id"}})
            self.assertEqual(client.release_group_mbid("release-id"), "group-id")
            self.assertEqual(client.release_group_mbid("release-id"), "group-id")
            client.request.assert_called_once()

    def test_submission_xml_supports_every_entity(self):
        rows = [
            {"entity": entity, "mbid": f"{entity}-id", "genres": ["rock"]}
            for entity in helper.ENTITY_LISTS
        ]
        xml = helper.submission_xml(rows).decode()
        for entity in helper.ENTITY_LISTS:
            self.assertIn(f'<{entity} id="{entity}-id">', xml)
        self.assertEqual(xml.count('<user-tag vote="upvote">'), 4)


if __name__ == "__main__":
    unittest.main()
