import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock


SCRIPT = Path(__file__).parents[1] / "lidarr-lastfm-genres-helper.py"
SPEC = importlib.util.spec_from_file_location("lidarr_lastfm_genres_helper", SCRIPT)
helper = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = helper
SPEC.loader.exec_module(helper)


class LidarrLastFmGenresHelperTests(unittest.TestCase):
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

    def test_present_scope_excludes_wanted_missing_albums(self):
        client = helper.LidarrClient("http://lidarr", "key")
        client.missing_albums = Mock(return_value=[{"id": 2}])
        client.all_albums = Mock(return_value=[{"id": 1}, {"id": 2}, {"id": 3}])
        self.assertEqual([row["id"] for row in client.albums("present")], [1, 3])

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


if __name__ == "__main__":
    unittest.main()
