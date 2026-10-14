"""Exercise executable package interfaces without network calls or submission."""
from pathlib import Path
import subprocess
import sys
import unittest


class PackageEntryPointTests(unittest.TestCase):
    def test_each_tool_runs_as_a_module(self):
        for module in ("ratings", "musicbrainz_genres", "new_artist", "itunes_library",
                       "lastfm_genres", "navidrome_genres"):
            with self.subTest(module=module):
                result = subprocess.run(
                    [sys.executable, "-B", "-m", "musicbrainz_helper." + module, "--help"],
                    cwd=Path(__file__).parents[1], capture_output=True, text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("usage:", result.stdout)

    def test_ratings_queue_commands_expose_source_and_submission_options(self):
        for command in ("enqueue", "work", "library"):
            with self.subTest(command=command):
                result = subprocess.run(
                    [sys.executable, "-B", "-m", "musicbrainz_helper.ratings", command, "--help"],
                    cwd=Path(__file__).parents[1], capture_output=True, text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("--rating-source", result.stdout)
                self.assertIn("--submit", result.stdout)


if __name__ == "__main__":
    unittest.main()
