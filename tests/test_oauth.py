import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

import requests

from musicbrainz_helper import musicbrainz
from musicbrainz_helper.oauth import OAuthError, OAuthManager, OOB_REDIRECT, authenticated_username


def response(status=200, payload=None):
    result = Mock()
    result.status_code = status
    result.ok = 200 <= status < 300
    result.json.return_value = payload or {}
    return result


class OAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.token_file = Path(self.temp.name) / "tokens.json"
        self.http = Mock(spec=requests.Session)
        self.http.headers = {}
        self.manager = OAuthManager("client", "secret", self.token_file, session=self.http)

    def tearDown(self):
        self.temp.cleanup()

    def test_authorization_uses_pkce_oob_and_minimal_submission_scopes(self):
        url, pending = self.manager.authorization()
        query = parse_qs(urlparse(url).query)
        self.assertEqual(query["client_id"], ["client"])
        self.assertEqual(query["redirect_uri"], [OOB_REDIRECT])
        self.assertEqual(query["scope"], ["tag rating profile"])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertTrue(query["code_challenge"][0])
        self.assertTrue(pending["code_verifier"])

    def test_exchange_saves_refresh_token_and_account_without_logging_tokens(self):
        self.http.post.return_value = response(payload={"access_token": "access", "refresh_token": "refresh", "expires_in": 3600})
        self.http.get.return_value = response(payload={"sub": "sickprodigy"})
        _, pending = self.manager.authorization()
        result = self.manager.exchange("one-time-code", pending)
        self.assertEqual(result["musicbrainz_username"], "sickprodigy")
        stored = json.loads(self.token_file.read_text(encoding="utf-8"))
        self.assertEqual(stored["refresh_token"], "refresh")
        self.assertGreater(stored["expires_at"], 0)

    def test_expired_access_token_is_refreshed_and_refresh_token_is_preserved(self):
        self.manager.save({"access_token": "old", "refresh_token": "refresh", "expires_in": -1,
                           "musicbrainz_username": "sickprodigy"})
        self.http.post.return_value = response(payload={"access_token": "new", "expires_in": 3600})
        self.assertEqual(self.manager.access_token(), "new")
        stored = json.loads(self.token_file.read_text(encoding="utf-8"))
        self.assertEqual(stored["refresh_token"], "refresh")
        self.assertEqual(stored["musicbrainz_username"], "sickprodigy")

    def test_apply_sets_bearer_header(self):
        self.manager.save({"access_token": "access", "refresh_token": "refresh", "expires_in": 3600})
        session = requests.Session()
        self.manager.apply(session)
        self.assertEqual(session.headers["Authorization"], "Bearer access")

    def test_revoke_removes_local_tokens(self):
        self.manager.save({"access_token": "access", "refresh_token": "refresh", "expires_in": 3600})
        self.http.post.return_value = response()
        self.manager.revoke()
        self.assertFalse(self.token_file.exists())

    def test_missing_authorization_has_actionable_error(self):
        with self.assertRaisesRegex(OAuthError, "musicbrainz-auth authorize"):
            self.manager.load()

    def test_authenticated_username_uses_token_identity(self):
        self.manager.save({"access_token": "access", "refresh_token": "refresh", "expires_in": 3600,
                           "musicbrainz_username": "sickprodigy"})
        with patch.dict(os.environ, {"MB_OAUTH_CLIENT_ID": "client", "MB_OAUTH_TOKEN_FILE": str(self.token_file)}, clear=False):
            self.assertEqual(authenticated_username("fallback"), "sickprodigy")

    def test_shared_client_prefers_oauth_over_digest(self):
        self.manager.save({"access_token": "access", "refresh_token": "refresh", "expires_in": 3600})
        with patch.dict(os.environ, {"MB_OAUTH_CLIENT_ID": "client", "MB_OAUTH_CLIENT_SECRET": "secret",
                                     "MB_OAUTH_TOKEN_FILE": str(self.token_file)}, clear=False):
            client = musicbrainz.MusicBrainzClient(musicbrainz.JsonCache(Path(self.temp.name) / "cache.json"),
                                                   "legacy-user", "legacy-token")
            self.assertIsNone(client.session.auth)
            client.authenticate()
            self.assertEqual(client.session.headers["Authorization"], "Bearer access")


if __name__ == "__main__":
    unittest.main()
