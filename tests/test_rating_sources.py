import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock
import xml.etree.ElementTree as ET

from musicbrainz_helper import ratings_worker as helper
from musicbrainz_helper.rating_sources import LastFmRatingSource, score_lastfm
from test_integrated_ratings import FakeSubsonic, FakeMB, SONG, TRACK, RECORDING, OTHER


class ContributionSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.nav, self.mb, self.source = FakeSubsonic(SONG), FakeMB(), Mock()
        self.source.rating.return_value = (4, "lastfm")

    def work(self, now=1000, backfill=True):
        return helper.run_worker(self.root, self.nav, self.mb, submit=True,
            account="account", interval=10, now=now, backfill=backfill, rating_source=self.source)

    def test_lastfm_contribution_goes_to_musicbrainz_without_rating_subsonic(self):
        self.work()
        self.work(now=1011, backfill=False)
        self.assertEqual(self.nav.data["song1"]["userRating"], 0)
        self.assertEqual(len(self.mb.writes), 1)
        self.assertEqual(self.mb.writes[0][1].find(".//{*}user-rating").text, "80")
        self.source.rating.assert_called_once()

    def test_existing_personal_rating_has_priority(self):
        self.nav.data["song1"]["userRating"] = 3
        self.work()
        self.source.rating.assert_not_called()
        self.assertEqual(self.mb.writes[0][1].find(".//{*}user-rating").text, "60")

    def test_source_failure_keeps_inputs_for_retry_after_import_job_is_consumed(self):
        helper.genres.write_json(self.root / "jobs/job.json", {"tracks": [TRACK]})
        self.source.rating.side_effect = [RuntimeError("unavailable"), (4, "lastfm")]
        self.assertEqual(self.work(backfill=False), 1)
        self.assertEqual(self.mb.writes, [])
        self.work(now=1011, backfill=False)
        self.assertEqual(len(self.mb.writes), 1)
        self.assertEqual(self.nav.data["song1"]["userRating"], 0)

    def test_missing_source_data_leaves_all_destinations_unchanged(self):
        self.source.rating.return_value = None
        self.work()
        self.assertEqual(self.mb.writes, [])
        self.assertEqual(self.nav.data["song1"]["userRating"], 0)

    def test_personal_clear_is_not_replaced_by_a_source_vote(self):
        self.nav.data["song1"]["userRating"] = 3
        self.work()
        self.nav.data["song1"]["userRating"] = 0
        self.work(now=1011)
        self.source.rating.assert_not_called()
        self.assertEqual([t.find(".//{*}user-rating").text for _, t in self.mb.writes], ["60", "0"])

    def test_lastfm_rejects_wrong_recording_and_version(self):
        source = LastFmRatingSource("fake-key")
        for title, recording in [("Song (Live)", RECORDING), ("Song", OTHER)]:
            source.lastfm = Mock(return_value={"track": {"name": title,
                "artist": {"name": "Artist"}, "mbid": recording}})
            self.assertIsNone(source.rating(TRACK))

    def test_lastfm_verified_recording_uses_independent_scoring(self):
        source = LastFmRatingSource("fake-key")
        source.lastfm = Mock(side_effect=[{"track": {"name": "Song", "artist": {"name": "Artist"},
            "mbid": RECORDING, "listeners": "1000000", "playcount": "10000000"}},
            {"toptracks": {"track": [{"name": "Song", "listeners": "1000000", "playcount": "10000000"}]}}])
        self.assertEqual(source.rating(TRACK), (5, "lastfm"))
        self.assertEqual(source.lastfm.call_args_list[0].kwargs["mbid"], RECORDING)
        self.assertEqual(score_lastfm(1000000, 10000000, 1), 5)
        self.assertLessEqual(score_lastfm(100, 1000, 100), 3)


if __name__ == "__main__":
    unittest.main()
