"""MusicBrainz OAuth authorization and token refresh support."""
from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
import time
from urllib.parse import urlencode

import requests


AUTHORIZE_URL = "https://musicbrainz.org/oauth2/authorize"
TOKEN_URL = "https://musicbrainz.org/oauth2/token"
REVOKE_URL = "https://musicbrainz.org/oauth2/revoke"
USERINFO_URL = "https://musicbrainz.org/oauth2/userinfo"
OOB_REDIRECT = "urn:ietf:wg:oauth:2.0:oob"
DEFAULT_SCOPES = ("tag", "rating", "profile")


class OAuthError(RuntimeError):
    pass


def _write_private(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    try:
        os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    os.replace(temporary, path)


class OAuthManager:
    def __init__(self, client_id: str, client_secret: str, token_file: Path,
                 redirect_uri: str = OOB_REDIRECT, session: requests.Session | None = None) -> None:
        if not client_id:
            raise OAuthError("MusicBrainz OAuth client ID is missing")
        self.client_id = client_id
        self.client_secret = client_secret
        self.token_file = token_file
        self.redirect_uri = redirect_uri
        self.http = session or requests.Session()
        self.http.headers.update({"User-Agent": "musicbrainz-helper OAuth (https://github.com/SickProdigy/musicbrainz-helper)"})

    @classmethod
    def configured_from_environment(cls, session: requests.Session | None = None) -> "OAuthManager | None":
        client_id = os.getenv("MB_OAUTH_CLIENT_ID", "").strip()
        token_file = os.getenv("MB_OAUTH_TOKEN_FILE", "").strip()
        if not client_id or not token_file:
            return None
        return cls(client_id, os.getenv("MB_OAUTH_CLIENT_SECRET", "").strip(), Path(token_file),
                   os.getenv("MB_OAUTH_REDIRECT_URI", OOB_REDIRECT).strip() or OOB_REDIRECT, session)

    @classmethod
    def from_environment(cls, session: requests.Session | None = None) -> "OAuthManager | None":
        manager = cls.configured_from_environment(session)
        return manager if manager and manager.token_file.is_file() else None

    @property
    def pending_file(self) -> Path:
        return self.token_file.with_name(f"{self.token_file.name}.pending")

    def load(self) -> dict:
        if not self.token_file.is_file():
            raise OAuthError("MusicBrainz OAuth is not authorized; run musicbrainz-auth authorize")
        try:
            payload = json.loads(self.token_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise OAuthError("MusicBrainz OAuth token file is unreadable") from exc
        if not payload.get("refresh_token") and not payload.get("access_token"):
            raise OAuthError("MusicBrainz OAuth token file contains no usable token")
        return payload

    def save(self, payload: dict, previous: dict | None = None) -> dict:
        result = dict(previous or {})
        result.update(payload)
        if "expires_in" in payload:
            result["expires_at"] = int(time.time()) + int(payload["expires_in"])
        result["client_id"] = self.client_id
        result["redirect_uri"] = self.redirect_uri
        _write_private(self.token_file, result)
        return result

    def authorization(self, scopes: tuple[str, ...] = DEFAULT_SCOPES) -> tuple[str, dict]:
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(24)
        pending = {"code_verifier": verifier, "state": state, "created_at": int(time.time()),
                   "scopes": list(scopes), "redirect_uri": self.redirect_uri}
        query = urlencode({"response_type": "code", "client_id": self.client_id,
                           "redirect_uri": self.redirect_uri, "scope": " ".join(scopes),
                           "state": state, "code_challenge": challenge, "code_challenge_method": "S256"})
        return f"{AUTHORIZE_URL}?{query}", pending

    def begin(self, scopes: tuple[str, ...] = DEFAULT_SCOPES, force: bool = False) -> tuple[str, bool]:
        if self.pending_file.is_file() and not force:
            try:
                pending = json.loads(self.pending_file.read_text(encoding="utf-8"))
                if pending.get("authorization_url") and pending.get("code_verifier"):
                    return str(pending["authorization_url"]), False
            except (OSError, ValueError, TypeError):
                pass
        url, pending = self.authorization(scopes)
        pending["authorization_url"] = url
        _write_private(self.pending_file, pending)
        return url, True

    def complete(self, code: str) -> dict:
        if not self.pending_file.is_file():
            raise OAuthError("No pending MusicBrainz authorization; request a new authorization URL")
        try:
            pending = json.loads(self.pending_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise OAuthError("Pending MusicBrainz authorization is unreadable") from exc
        result = self.exchange(code, pending)
        self.pending_file.unlink(missing_ok=True)
        return result

    def exchange(self, code: str, pending: dict) -> dict:
        response = self.http.post(TOKEN_URL, data={"grant_type": "authorization_code", "code": code.strip(),
                                  "client_id": self.client_id, "client_secret": self.client_secret,
                                  "redirect_uri": pending.get("redirect_uri", self.redirect_uri),
                                  "code_verifier": pending["code_verifier"]}, timeout=30)
        if not response.ok:
            raise OAuthError(f"MusicBrainz OAuth code exchange failed ({response.status_code})")
        payload = response.json()
        access_token = payload.get("access_token")
        if access_token:
            identity = self.http.get(USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"}, timeout=30)
            if identity.ok:
                user = identity.json()
                payload["musicbrainz_username"] = user.get("username") or user.get("sub")
        return self.save(payload)

    def access_token(self) -> str:
        payload = self.load()
        if payload.get("access_token") and int(payload.get("expires_at", 0)) > int(time.time()) + 60:
            return str(payload["access_token"])
        refresh = payload.get("refresh_token")
        if not refresh:
            raise OAuthError("MusicBrainz OAuth access token expired and no refresh token is available")
        response = self.http.post(TOKEN_URL, data={"grant_type": "refresh_token", "refresh_token": refresh,
                                  "client_id": self.client_id, "client_secret": self.client_secret}, timeout=30)
        if not response.ok:
            raise OAuthError(f"MusicBrainz OAuth token refresh failed ({response.status_code}); authorize again")
        return str(self.save(response.json(), payload)["access_token"])

    def apply(self, session: requests.Session) -> None:
        session.headers["Authorization"] = f"Bearer {self.access_token()}"

    def revoke(self) -> None:
        payload = self.load()
        token = payload.get("refresh_token") or payload.get("access_token")
        response = self.http.post(REVOKE_URL, data={"token": token, "client_id": self.client_id,
                                                   "client_secret": self.client_secret}, timeout=30)
        if not response.ok:
            raise OAuthError(f"MusicBrainz OAuth revocation failed ({response.status_code})")
        self.token_file.unlink(missing_ok=True)
        self.pending_file.unlink(missing_ok=True)


def has_oauth_credentials() -> bool:
    manager = OAuthManager.from_environment()
    return bool(manager and manager.token_file.is_file())


def authenticated_username(fallback: str = "") -> str:
    if fallback and (os.getenv("MB_PASSWORD") or os.getenv("MB_TOKEN")):
        return fallback
    manager = OAuthManager.from_environment()
    if manager and manager.token_file.is_file():
        try:
            return str(manager.load().get("musicbrainz_username") or fallback)
        except OAuthError:
            pass
    return fallback


def authorization_notice(new_only: bool = True) -> str:
    manager = OAuthManager.configured_from_environment()
    if not manager or manager.token_file.is_file():
        return ""
    url, created = manager.begin()
    if new_only and not created:
        return ""
    return ("MusicBrainz OAuth authorization is available:\n"
            f"{url}\n"
            "After approval, run MusicBrainzGenres.bash --complete-authorization and paste the one-time code.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Authorize musicbrainz-helper with MusicBrainz OAuth.")
    parser.add_argument("command", choices=("authorize", "begin", "complete", "status", "revoke"))
    parser.add_argument("--client-id", default=os.getenv("MB_OAUTH_CLIENT_ID", ""))
    parser.add_argument("--client-secret", default=os.getenv("MB_OAUTH_CLIENT_SECRET", ""))
    parser.add_argument("--token-file", type=Path,
                        default=Path(os.getenv("MB_OAUTH_TOKEN_FILE", "oauth-tokens.json")))
    parser.add_argument("--redirect-uri", default=os.getenv("MB_OAUTH_REDIRECT_URI", OOB_REDIRECT))
    parser.add_argument("--code", help="Authorization code; omit to be prompted without echoing tokens in arguments.")
    parser.add_argument("--new-only", action="store_true", help="Print a URL only when a new request is created.")
    parser.add_argument("--force", action="store_true", help="Replace an existing pending authorization request.")
    args = parser.parse_args()
    try:
        manager = OAuthManager(args.client_id.strip(), args.client_secret.strip(), args.token_file, args.redirect_uri)
        if args.command == "authorize":
            url, _ = manager.begin(force=args.force)
            print("Open this URL and approve access:\n")
            print(url)
            code = args.code or getpass.getpass("\nPaste the authorization code: ").strip()
            if not code:
                raise OAuthError("No authorization code was provided")
            manager.complete(code)
            print(f"MusicBrainz OAuth authorization saved to {manager.token_file}")
        elif args.command == "begin":
            url, created = manager.begin(force=args.force)
            if created or not args.new_only:
                print("MusicBrainz OAuth authorization is available:\n")
                print(url)
                print("\nAfter approval, run MusicBrainzGenres.bash --complete-authorization and paste the one-time code.")
        elif args.command == "complete":
            code = args.code or getpass.getpass("Paste the authorization code: ").strip()
            if not code:
                raise OAuthError("No authorization code was provided")
            manager.complete(code)
            print(f"MusicBrainz OAuth authorization saved to {manager.token_file}")
        elif args.command == "status":
            token = manager.load()
            expires = int(token.get("expires_at", 0))
            print("authorized" if token.get("refresh_token") or expires > time.time() else "reauthorization required")
        else:
            manager.revoke()
            print("MusicBrainz OAuth authorization revoked")
    except (OAuthError, requests.RequestException) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
