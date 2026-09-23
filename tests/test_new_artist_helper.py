import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock


SCRIPT = Path(__file__).parents[1] / "musicbrainz-new-artist-helper.py"
SPEC = importlib.util.spec_from_file_location("new_artist_helper", SCRIPT)
helper = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = helper
SPEC.loader.exec_module(helper)


class NewArtistHelperTests(unittest.TestCase):
    def test_extracts_artist_and_album_ids(self):
        self.assertEqual(
            helper.extract_apple_id("https://music.apple.com/us/artist/youngvynn-q/6776781654"),
            (6776781654, "artist"),
        )
        self.assertEqual(
            helper.extract_apple_id(
                "https://music.apple.com/us/album/he-said-i-looked-fine-single/6803960307"
            ),
            (6803960307, "album"),
        )

    def test_cleans_store_suffixes_and_infers_types(self):
        self.assertEqual(helper.strip_store_suffix("He Said I Looked Fine - Single"), "He Said I Looked Fine")
        self.assertEqual(helper.strip_store_suffix("Example - EP"), "Example")
        self.assertEqual(helper.infer_primary_type("Example - Single", 1), "Single")
        self.assertEqual(helper.infer_primary_type("Example - EP", 5), "EP")
        self.assertEqual(helper.infer_primary_type("Example", 10), "Album")

    def test_moves_featured_artist_from_track_title_to_credit(self):
        self.assertEqual(
            helper.normalize_track_credit("Final Battle (feat. T-Blaze)", "Spark"),
            ("Final Battle", "Spark feat. T-Blaze"),
        )
        self.assertEqual(
            helper.normalize_track_credit("Song (Live)", "Artist"),
            ("Song (Live)", "Artist"),
        )

    def test_release_seed_contains_exact_track_duration(self):
        release = helper.AppleRelease(
            apple_id=6803960307,
            title="He Said I Looked Fine",
            store_title="He Said I Looked Fine - Single",
            artist="YoungVynn Q",
            artist_id=6776781654,
            release_date="2026-08-28",
            country="US",
            genre="Hip-Hop/Rap",
            copyright="℗ 2026 YoungVynnRecords",
            explicit=False,
            url="https://music.apple.com/us/album/he-said-i-looked-fine-single/6803960307",
            primary_type="Single",
            tracks=(
                helper.AppleTrack(
                    number=1,
                    title="He Said I Looked Fine",
                    artist="YoungVynn Q",
                    duration_ms=287760,
                    apple_id=6803960308,
                ),
            ),
        )
        fields = dict(helper.release_seed(release, "artist-mbid", None, "eng", "Latn"))
        self.assertEqual(fields["mediums.0.track.0.length"], "287760")
        self.assertEqual(fields["labels.0.name"], "YoungVynnRecords")
        self.assertEqual(fields["events.0.date.day"], "28")
        self.assertEqual(fields["artist_credit.names.0.mbid"], "artist-mbid")
        self.assertNotIn("Please verify", fields["edit_note"])

    def test_releases_exclude_appears_on_collections(self):
        client = helper.AppleClient("us")
        artist_id = 123
        client.lookup = Mock(
            side_effect=[
                [
                    {
                        "wrapperType": "collection",
                        "collectionId": 10,
                        "collectionName": "Own Release - Single",
                        "artistName": "Example Artist",
                        "artistId": artist_id,
                        "releaseDate": "2026-01-01T00:00:00Z",
                    },
                    {
                        "wrapperType": "collection",
                        "collectionId": 20,
                        "collectionName": "Featured Elsewhere - Single",
                        "artistName": "Another Artist",
                        "artistId": 456,
                        "releaseDate": "2026-02-01T00:00:00Z",
                    },
                ],
                [
                    {
                        "wrapperType": "track",
                        "trackNumber": 1,
                        "trackName": "Own Track",
                        "artistName": "Example Artist",
                        "trackTimeMillis": 180000,
                        "trackId": 11,
                    }
                ],
            ]
        )

        releases = client.releases(artist_id, "Example Artist", keep_store_suffixes=False, limit=None)

        self.assertEqual([release.apple_id for release in releases], [10])

    def test_joint_artist_credit_links_only_the_target_artist(self):
        fields: list[tuple[str, str]] = []

        helper.add_artist_credit_fields(
            fields,
            "artist_credit.names",
            "On The Radar & Jessie Renee",
            "Jessie Renee",
            "jessie-mbid",
        )

        self.assertIn(("artist_credit.names.0.name", "On The Radar"), fields)
        self.assertIn(("artist_credit.names.0.join_phrase", " & "), fields)
        self.assertNotIn(("artist_credit.names.0.mbid", "jessie-mbid"), fields)
        self.assertIn(("artist_credit.names.1.name", "Jessie Renee"), fields)
        self.assertIn(("artist_credit.names.1.mbid", "jessie-mbid"), fields)


if __name__ == "__main__":
    unittest.main()
