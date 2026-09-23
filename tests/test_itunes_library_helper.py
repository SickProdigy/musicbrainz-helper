import importlib.util
import plistlib
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "itunes-library-helper.py"
SPEC = importlib.util.spec_from_file_location("itunes_library_helper", SCRIPT)
helper = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = helper
SPEC.loader.exec_module(helper)


class ItunesLibraryHelperTests(unittest.TestCase):
    def test_rating_conversion(self):
        self.assertEqual(helper.rating_stars(100), "5")
        self.assertEqual(helper.rating_stars("60"), "3")
        self.assertEqual(helper.rating_stars(""), "")

    def test_feature_credit_is_removed_from_recording_search_title(self):
        self.assertEqual(
            helper.base_track_title("Tomorrow Til Infinity (feat. Gunna)"),
            "Tomorrow Til Infinity",
        )

    def test_reads_xml_ratings_genres_and_playlists(self):
        library = {
            "Application Version": "12.12",
            "Tracks": {
                "1": {
                    "Track ID": 1,
                    "Persistent ID": "ABC",
                    "Name": "Song",
                    "Artist": "Artist",
                    "Album": "Album",
                    "Genre": "Hip-Hop/Rap",
                    "Rating": 80,
                    "Loved": True,
                    "Album Loved": True,
                    "Album Rating": 100,
                    "Album Rating Computed": True,
                    "Total Time": 123456,
                }
            },
            "Playlists": [{"Name": "Favorites", "Playlist Items": [{"Track ID": 1}]}],
        }
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "Library.xml"
            with source.open("wb") as handle:
                plistlib.dump(library, handle)
            tracks, playlist_items, metadata = helper.read_source(source)

        self.assertEqual(tracks[0].rating_stars, "4")
        self.assertEqual(tracks[0].loved, "true")
        self.assertEqual(tracks[0].album_loved, "true")
        self.assertEqual(tracks[0].album_rating_computed, "true")
        self.assertEqual(tracks[0].genre, "Hip-Hop/Rap")
        self.assertEqual(tracks[0].duration_seconds, "123.456")
        self.assertEqual(playlist_items[0]["playlist"], "Favorites")
        self.assertEqual(metadata["source_type"], "itunes-library-xml")

    def test_reads_utf16_playlist_export(self):
        content = "Name\tArtist\tGenre\tMy Rating\tPlays\nSong\tArtist\tR&B/Soul\t60\t12\n"
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "playlist.txt"
            source.write_text(content, encoding="utf-16")
            tracks, playlist_items, metadata = helper.read_source(source)

        self.assertEqual(tracks[0].rating_stars, "3")
        self.assertEqual(tracks[0].play_count, "12")
        self.assertEqual(playlist_items[0]["playlist"], "playlist")
        self.assertEqual(metadata["source_type"], "itunes-playlist-text")


if __name__ == "__main__":
    unittest.main()
