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
        self.assertEqual(
            helper.without_trailing_parenthetical("Through the Valley (The Last of Us Part II)"),
            "Through the Valley",
        )

    def test_release_dates_match_on_known_components(self):
        self.assertTrue(helper.compatible_release_dates("2016-12-19", "2016"))
        self.assertTrue(helper.compatible_release_dates("2016-12-19", "2016-12"))
        self.assertTrue(helper.compatible_release_dates("2016-12-19", "2016-12-21"))
        self.assertFalse(helper.compatible_release_dates("2016-12-19", "2016-11-30"))
        self.assertFalse(helper.compatible_release_dates("2016-12-19", "2017"))
        self.assertFalse(helper.compatible_release_dates("2016-12-19", ""))

    def test_release_match_fetches_date_missing_from_search_result(self):
        client = helper.MusicBrainzClient()
        client.search = Mock(
            return_value={
                "releases": [
                    {
                        "id": "release-mbid",
                        "title": "Jungle",
                        "artist-credit": [{"name": "Tash Sultana"}],
                        "score": 100,
                    }
                ]
            }
        )
        client.release_date = Mock(return_value="2016")
        release = helper.AppleRelease(
            apple_id=1881446984,
            title="Jungle",
            store_title="Jungle - Single",
            artist="Tash Sultana",
            artist_id=1105306265,
            release_date="2016-12-19",
            country="US",
            genre="Alternative",
            copyright="",
            explicit=False,
            url="https://music.apple.com/us/album/jungle-single/1881446984",
            primary_type="Single",
            tracks=(),
        )

        matches = client.release_matches(release, "artist-mbid")

        self.assertTrue(matches[0].exact)
        self.assertIn("| 2016 |", matches[0].disambiguation)
        self.assertEqual(matches[0].reasons, ("date differs",))
        client.release_date.assert_called_once_with("release-mbid")

    def test_literal_release_date_match_has_no_note(self):
        client = helper.MusicBrainzClient()
        client.search = Mock(
            return_value={
                "releases": [
                    {
                        "id": "release-mbid",
                        "title": "Example",
                        "date": "2026-01-02",
                        "artist-credit": [{"name": "Example Artist"}],
                        "score": 100,
                    }
                ]
            }
        )
        release = helper.AppleRelease(
            1, "Example", "Example - Single", "Example Artist", 2, "2026-01-02",
            "US", "", "", False, "https://music.apple.com/", "Single", ()
        )

        match = client.release_matches(release, "artist-mbid")[0]

        self.assertTrue(match.exact)
        self.assertEqual(match.reasons, ())

    def test_release_match_explains_date_mismatch(self):
        client = helper.MusicBrainzClient()
        client.search = Mock(
            return_value={
                "releases": [
                    {
                        "id": "release-mbid",
                        "title": "Example",
                        "date": "2025-11-30",
                        "artist-credit": [{"name": "Example Artist"}],
                        "score": 100,
                    }
                ]
            }
        )
        release = helper.AppleRelease(
            1, "Example", "Example - Single", "Example Artist", 2, "2026-01-02",
            "US", "", "", False, "https://music.apple.com/", "Single", ()
        )

        match = client.release_matches(release, "artist-mbid")[0]

        self.assertFalse(match.exact)
        self.assertEqual(match.reasons, ("date differs",))
        self.assertIn('<strong class="reason">date differs</strong>', helper.match_list([match]))

    def test_release_search_retries_without_trailing_parenthetical(self):
        client = helper.MusicBrainzClient()
        client.search = Mock(
            side_effect=[
                {"releases": []},
                {
                    "releases": [
                        {
                            "id": "release-mbid",
                            "title": "Through the Valley",
                            "date": "2020-07-24",
                            "artist-credit": [{"name": "Tash Sultana"}],
                            "score": 100,
                        }
                    ]
                },
            ]
        )
        release = helper.AppleRelease(
            1, "Through the Valley (The Last of Us Part II)", "", "Tash Sultana", 2,
            "2020-07-24", "US", "", "", False, "https://music.apple.com/", "Single", ()
        )

        match = client.release_matches(release, "artist-mbid")[0]

        self.assertFalse(match.exact)
        self.assertEqual(match.reasons, ("title differs",))
        self.assertIn('release:"Through the Valley"', client.search.call_args_list[1].args[1])

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

    def test_suppressed_release_keeps_force_seed_button(self):
        release = helper.AppleRelease(
            apple_id=1881446984,
            title="Jungle",
            store_title="Jungle - Single",
            artist="Tash Sultana",
            artist_id=1105306265,
            release_date="2016-12-19",
            country="US",
            genre="Alternative",
            copyright="",
            explicit=False,
            url="https://music.apple.com/us/album/jungle-single/1881446984",
            primary_type="Single",
            tracks=(),
        )
        args = Mock(label_mbid=None, language=None, script="Latn", country="us")

        report = helper.render_report(
            "Tash Sultana", 1105306265, [], "artist-mbid", "Tash Sultana",
            [(release, [], False)], args
        )

        notice = report.index("Compatible MusicBrainz match found; seed suppressed.")
        force_button = report.index("Force open prefilled release editor")
        self.assertGreater(force_button, notice)
        self.assertIn(helper.MUSICBRAINZ_RELEASE_EDITOR, report)

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
