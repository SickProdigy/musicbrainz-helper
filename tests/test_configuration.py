"""Configuration behavior shared by the formerly separate standalone tools."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from musicbrainz_helper import musicbrainz, new_artist, itunes_library, ratings


class ConfigurationTests(unittest.TestCase):
    def test_each_tool_preserves_process_settings_and_reads_windows_env_files(self):
        loaders = (musicbrainz.load_dotenv, new_artist.load_dotenv,
                   itunes_library.load_dotenv, ratings.load_dotenv_file)
        for loader in loaders:
            with self.subTest(loader=loader), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / ".env"
                text = "# comment\nHELPER_TEST_USER=file-user\nHELPER_TEST_TOKEN='token=value'\nHELPER_TEST_LITERAL='literal\n=ignored\n"
                path.write_bytes(text.encode("utf-8-sig"))
                with patch.dict(os.environ, {"HELPER_TEST_USER": "process-user"}):
                    os.environ.pop("HELPER_TEST_TOKEN", None)
                    os.environ.pop("HELPER_TEST_LITERAL", None)
                    loader(str(path))
                    self.assertEqual(os.environ["HELPER_TEST_USER"], "process-user")
                    self.assertEqual(os.environ["HELPER_TEST_TOKEN"], "token=value")
                    self.assertEqual(os.environ["HELPER_TEST_LITERAL"], "'literal")

    def test_missing_env_or_directory_is_optional(self):
        with tempfile.TemporaryDirectory() as directory:
            musicbrainz.load_dotenv(directory)
            musicbrainz.load_dotenv(Path(directory) / "missing.env")

    def test_itunes_accepts_token_option_and_legacy_password_alias(self):
        for option in ("--mb-token", "--mb-password"):
            with self.subTest(option=option), patch("sys.argv", ["musicbrainz-itunes-library", "library.xml", option, "test-token"]):
                self.assertEqual(itunes_library.parse_args().mb_password, "test-token")


if __name__ == "__main__":
    unittest.main()
