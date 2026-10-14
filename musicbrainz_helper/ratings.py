#!/usr/bin/env python3
"""Push Navidrome ratings to MusicBrainz.

This script reads ratings from Navidrome over its Subsonic API and submits them
to the MusicBrainz XML API. It supports song, album, and artist ratings.

Song ratings are pushed to recording MBIDs. If a song belongs to a release group
and multiple releases in that group contain the same track title, all matching
recordings are rated instead of stopping at the first match.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
import time
import xml.etree.ElementTree as ET

import requests
from requests.auth import HTTPDigestAuth
from colorama import init, Fore, Style

# Initialize colorama
init(autoreset=False)


MUSICBRAINZ_BASE_URL = "https://musicbrainz.org/ws/2"
MUSICBRAINZ_XML_NS = "http://musicbrainz.org/ns/mmd-2.0#"
from . import __version__
from .musicbrainz import load_dotenv as load_dotenv_file
CLIENT_NAME = "musicbrainz-helper/" + __version__
SCRIPT_VERSION = __version__
NAVIDROME_REQUEST_TIMEOUTS = [10, 20, 30, 45, 60]
NAVIDROME_RETRY_DELAYS = [5, 10, 15, 20, 30]
MUSICBRAINZ_RETRY_DELAYS = [3, 10, 30, 60, 120]

# Colors for logging
LIGHT_PURPLE = Fore.MAGENTA + Style.BRIGHT
LIGHT_GREEN = Fore.GREEN + Style.BRIGHT
LIGHT_RED = Fore.RED + Style.BRIGHT
LIGHT_BLUE = Fore.BLUE + Style.BRIGHT
LIGHT_CYAN = Fore.CYAN + Style.BRIGHT
LIGHT_YELLOW = Fore.YELLOW + Style.BRIGHT
BOLD = Style.BRIGHT
RESET = Style.RESET_ALL


class SafeAsciiFormatter(logging.Formatter):
    """Logging formatter that strips ANSI escape codes and encodes to ASCII."""

    ansi_escape = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")

    def format(self, record):
        rendered = super().format(record)
        rendered = self.ansi_escape.sub("", rendered)
        return rendered.encode("ascii", "backslashreplace").decode("ascii")


# Setup logs (match sptnr logging behavior)
LOG_DIR = "logs"

LOGFILE = os.path.join(LOG_DIR, f"musicbrainz-ratings-helper_{int(time.time())}.log")


@dataclass(frozen=True)
class RatingRow:
    entity_type: str
    navidrome_id: str
    mbid: str
    title: str
    artist: str
    release_group_mbid: str | None
    release_mbid: str | None
    rating: int


PreparedRow = tuple[str, str, int, RatingRow]
SubmissionCounts = dict[str, int]


class MusicBrainzBadRequest(Exception):
    def __init__(self, response: requests.Response) -> None:
        self.response = response
        super().__init__(str(response))


def empty_submission_counts() -> SubmissionCounts:
    return {"artist": 0, "release-group": 0, "recording": 0}


def add_submission_counts(total: SubmissionCounts, increment: SubmissionCounts) -> None:
    for key in ("artist", "release-group", "recording"):
        total[key] = total.get(key, 0) + increment.get(key, 0)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Push Navidrome ratings to MusicBrainz or force MusicBrainz artist release-group ratings."
    )
    parser.add_argument(
        "--navidrome-base-url",
        default=None,
        help="Base URL for Navidrome, for example https://navidrome.example.com.",
    )
    parser.add_argument(
        "--navidrome-username",
        default=None,
        help="Navidrome username for Subsonic API auth.",
    )
    parser.add_argument(
        "--navidrome-password",
        default=None,
        help="Navidrome password for Subsonic API auth.",
    )
    parser.add_argument(
        "--entity",
        action="append",
        choices=["song", "album", "artist"],
        help="Limit export to one or more entity types. Can be repeated.",
    )
    parser.add_argument(
        "--expand-release-groups",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Expand song ratings to all matching recordings in the same release group.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be submitted without posting to MusicBrainz.",
    )
    parser.add_argument(
        "--max-artists",
        type=int,
        default=None,
        help="Limit how many artist ratings are collected. Useful for short artist-only tests.",
    )
    parser.add_argument(
        "--max-albums",
        type=int,
        default=None,
        help="Limit how many album ratings are collected. Useful for short album-only tests.",
    )
    parser.add_argument(
        "--override-rating",
        type=int,
        default=None,
        help="Submit this Navidrome-style rating value instead of each source rating. Use 1 to force one-star MusicBrainz ratings.",
    )
    parser.add_argument(
        "--include-unrated-albums",
        action="store_true",
        help="With --entity album and --override-rating, include albums even when Navidrome has no album rating.",
    )
    parser.add_argument(
        "--force-artist-ratings",
        default=None,
        help="MusicBrainz artist MBID whose release-group ratings should be forced directly, bypassing Navidrome.",
    )
    parser.add_argument(
        "--max-release-groups",
        type=int,
        default=None,
        help="Limit MusicBrainz release-groups collected in --force-artist-ratings mode.",
    )
    parser.add_argument(
        "--artist-id",
        default=None,
        help="Limit album and song processing to a single Navidrome artist ID.",
    )
    parser.add_argument(
        "--start-artist-id",
        default=None,
        help="Resume artist-order processing at this Navidrome artist ID and continue onward.",
    )
    parser.add_argument(
        "--start-album-id",
        default=None,
        help="Resume album/song processing at this Navidrome album ID and continue onward.",
    )
    parser.add_argument(
        "--mb-username",
        default=None,
        help="MusicBrainz username. Defaults to MB_USERNAME.",
    )
    parser.add_argument(
        "--mb-password",
        default=None,
        help="MusicBrainz application token. Defaults to MB_TOKEN (legacy MB_PASSWORD).",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity.",
    )
    # submit order is hard-coded to artist, release-group, recording
    return parser.parse_args()




def normalize_text(value: str | None) -> str:
    return " ".join((value or "").strip().lower().split())


def normalized(value):
    import unicodedata
    return " ".join(unicodedata.normalize("NFKC", str(value)).casefold().split())


def rating_to_musicbrainz(value: int) -> int:
    value = max(0, min(5, value))
    return int(round(value * 20))


def required_arg(value: str | None, env_name: str, label: str) -> str:
    resolved = value or os.environ.get(env_name)
    if not resolved:
        logging.error(f"{LIGHT_RED}Missing required {label}. Set {env_name} or pass --{label}.{RESET}")
        raise SystemExit(1)
    return resolved


def log_blank_line() -> None:
    logging.info("")


def log_artist_header(name: str, navidrome_id: str, index: int) -> None:
    logging.info(f"Artist: {name} ({navidrome_id})[{index}]")


def log_album_header(
    name: str,
    navidrome_id: str,
    rating: int | None = None,
    has_rated_songs: bool = False,
    release_group_mbid: str | None = None,
) -> None:
    """Log an album header with compact rating/skip context and resolved RG MBID.

    Examples:
            Album: Gucci (1MrF6...) | nr:20.0 | mbidRG:45380071-f2f0...
            Album: Foo (abc123) | nr:n/a | contains rated songs | mbidRG:n/a
    """
    if rating is None or rating <= 0:
        rating_str = "n/a"
    else:
        rating_str = f"{rating:.1f}"

    extra = f" | nr:{rating_str}"
    if has_rated_songs and (rating is None or rating <= 0):
        extra += " | contains rated songs"

    rg = release_group_mbid or "n/a"
    extra += f" | mbidRG:{rg}"

    logging.info(f"  Album: {name} ({navidrome_id}){extra}")


def log_skip(name: str, rating: int, entity: str = "Item") -> None:
    logging.info(f"{entity}: s:{format_source_rating(rating)} -> mb:n/a | Skipping: {name}")


def _format_conn_error(exc: Exception, label: str) -> str:
    """Return a compact, human-friendly connection error string.

    Examples:
      MusicBrainz connection error, musicbrainz.org:443; Read timed out (10s)
      Navidrome connection error, nav.example:443; Connection aborted
    """
    text = str(exc)
    # Try to extract host and port from common requests.ConnectionPool formatting
    m = re.search(r"host='(?P<host>[^']+)'\s*,\s*port=(?P<port>\d+)", text)
    hostport = None
    if m:
        host = m.group("host")
        port = m.group("port")
        hostport = f"{host}:{port}"

    # Short message: prefer 'Read timed out' or the first sentence
    short_msg = None
    if "Read timed out" in text:
        # try to find timeout seconds
        tm = re.search(r"read timeout=?(?P<secs>\d+)", text)
        if tm:
            short_msg = f"Read timed out ({tm.group('secs')}s)"
        else:
            short_msg = "Read timed out"
    else:
        # take up to the first period or 120 chars
        short_msg = text.split(".")[0][:120]

    if hostport:
        return f"{label} connection error, {hostport}; {short_msg}"
    return f"{label} connection error; {short_msg}"


class NavidromeClient:
    def __init__(self, base_url: str, username: str, password: str, client_name: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.client_name = client_name
        self.session = requests.Session()
        self._next_request_at = 0.0
        # Stats collected while scanning Navidrome library
        self.stats: dict[str, int] = {
            "tracks": 0,
            "found": 0,
            "skipped": 0,
            "not_found": 0,
        }

    def _throttle(self) -> None:
        now = time.monotonic()
        if now < self._next_request_at:
            time.sleep(self._next_request_at - now)
        self._next_request_at = time.monotonic() + 1.05

    def _request(self, endpoint: str, params: dict[str, object]) -> dict:
        """Make a Navidrome API request with retry logic and exponential backoff."""
        retry_delays = NAVIDROME_RETRY_DELAYS
        request_timeouts = NAVIDROME_REQUEST_TIMEOUTS
        max_retries = len(retry_delays)
        
        for attempt in range(max_retries):
            try:
                self._throttle()
                query = {
                    "u": self.username,
                    "p": self.password,
                    "v": "1.16.1",
                    "c": self.client_name,
                    "f": "json",
                    **{k: v for k, v in params.items() if v is not None},
                }
                # Ensure all query params are strings for requests and type-checkers
                safe_query = {k: str(v) for k, v in query.items()}
                request_timeout = request_timeouts[min(attempt, len(request_timeouts) - 1)]
                response = self.session.get(
                    f"{self.base_url}/rest/{endpoint}",
                    params=safe_query,
                    timeout=request_timeout,
                )
                
                # Handle server errors with retry
                if response.status_code >= 500:
                    if attempt >= max_retries - 1:
                        logging.warning(f"{LIGHT_YELLOW}Navidrome server error {response.status_code}. Attempt {attempt + 1}/{max_retries}. No retries left.{RESET}")
                        continue
                    wait_time = retry_delays[attempt]
                    logging.warning(f"{LIGHT_YELLOW}Navidrome server error {response.status_code} after {request_timeout}s timeout. Attempt {attempt + 1}/{max_retries}. Retrying in {wait_time}s...{RESET}")
                    time.sleep(wait_time)
                    continue
                
                response.raise_for_status()
                payload = response.json()

                if payload.get("error"):
                    logging.error(f"{LIGHT_RED}Navidrome API error for {endpoint}: {payload['error']}{RESET}")
                    raise SystemExit(1)

                return payload.get("subsonic-response", payload)
                
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
                short = _format_conn_error(e, "Navidrome")
                request_timeout = request_timeouts[min(attempt, len(request_timeouts) - 1)]
                if attempt >= max_retries - 1:
                    logging.warning(f"{LIGHT_YELLOW}{short} after {request_timeout}s timeout. Attempt {attempt + 1}/{max_retries}. No retries left.{RESET}")
                    continue
                wait_time = retry_delays[attempt]
                logging.warning(f"{LIGHT_YELLOW}{short} after {request_timeout}s timeout. Attempt {attempt + 1}/{max_retries}. Retrying in {wait_time}s...{RESET}")
                time.sleep(wait_time)
                continue
            except requests.exceptions.RequestException as e:
                logging.error(f"{LIGHT_RED}Navidrome request failed: {e}{RESET}")
                break
        
        # If we get here, all retries failed
        logging.error(f"{LIGHT_RED}Failed after {max_retries} attempts for Navidrome endpoint: {endpoint}{RESET}")
        raise SystemExit(1)

    def get_artists(self) -> list[dict]:
        response = self._request("getArtists", {})
        artists: list[dict] = []
        for index in response.get("artists", {}).get("index", []):
            artists.extend(index.get("artist", []))
        return artists

    def get_album_list_page(self, offset: int, size: int = 500) -> list[dict]:
        response = self._request(
            "getAlbumList2",
            {"type": "alphabeticalByName", "offset": offset, "size": size},
        )
        return response.get("albumList2", {}).get("album", [])

    def get_album(self, album_id: str) -> dict:
        response = self._request("getAlbum", {"id": album_id})
        return response.get("album", {})

    def get_artist(self, artist_id: str) -> dict:
        response = self._request("getArtist", {"id": artist_id})
        return response.get("artist", {})

    def iter_all_albums(self, page_size: int = 500):
        offset = 0
        while True:
            page = self.get_album_list_page(offset, page_size)
            if not page:
                break
            for album in page:
                yield album
            if len(page) < page_size:
                break
            offset += len(page)

    def build_rows(
        self,
        entities: set[str],
        musicbrainz: "MusicBrainzClient",
        max_artists: int | None = None,
        max_albums: int | None = None,
        artist_id: str | None = None,
        start_artist_id: str | None = None,
        start_album_id: str | None = None,
        override_rating: int | None = None,
        include_unrated_albums: bool = False,
    ):
        # When an explicit artist_id is provided we may need the artist twice
        # (for artist rows and for album/song collection). Fetch it once and reuse.
        artist_source: list[dict] | None = None
        if artist_id or start_artist_id or "artist" in entities:
            artist_source = ([self.get_artist(artist_id)] if artist_id else self.get_artists())

        if "artist" in entities:
            artist_rows = 0
            started_artists = bool(artist_id or not start_artist_id)
            found_start_artist = bool(artist_id or not start_artist_id)
            printed_artist_header = False
            for artist_index, artist in enumerate(artist_source or []):
                if not artist:
                    continue
                if not started_artists:
                    if artist.get("id") != start_artist_id:
                        continue
                    started_artists = True
                    found_start_artist = True
                if not printed_artist_header:
                    log_blank_line()
                    printed_artist_header = True
                log_artist_header(artist.get("name", ""), artist.get("id", ""), artist_index)
                rating = int(artist.get("userRating") or 0)
                if rating <= 0:
                    log_skip(artist.get("name", ""), rating, entity="Artist")
                    continue
                mbid = artist.get("musicBrainzId") or ""
                if not mbid:
                    continue
                yield RatingRow(
                    entity_type="artist",
                    navidrome_id=artist.get("id", ""),
                    mbid=mbid,
                    title=artist.get("name", ""),
                    artist=artist.get("name", ""),
                    release_group_mbid=None,
                    release_mbid=None,
                    rating=rating,
                )
                artist_rows += 1
                if max_artists is not None and artist_rows >= max_artists:
                    break
            if start_artist_id and not artist_id and not found_start_artist:
                logging.warning(f"{LIGHT_YELLOW}Start artist ID {start_artist_id} was not found.{RESET}")

        if "album" in entities or "song" in entities:
            album_rows = 0
            page_size = max_albums if max_albums is not None and max_albums > 0 else 500
            # Album/song resume can be either exact album-order resume or artist-order resume.
            selected_artist = None
            album_source: Iterable[tuple[dict, dict | None, int | None]] = []
            if artist_id:
                selected_artist = (artist_source or [self.get_artist(artist_id)])[0]
                album_source = [(album, selected_artist, 0) for album in selected_artist.get("album", [])]
            elif start_artist_id:
                album_rows_by_artist: list[tuple[dict, dict | None, int | None]] = []
                started_album_artists = False
                found_start_artist = False
                for artist_index, artist in enumerate(artist_source or self.get_artists()):
                    if not artist:
                        continue
                    if not started_album_artists:
                        if artist.get("id") != start_artist_id:
                            continue
                        started_album_artists = True
                        found_start_artist = True
                    artist_detail = self.get_artist(artist.get("id", ""))
                    if not artist_detail:
                        continue
                    for album in artist_detail.get("album", []):
                        album_rows_by_artist.append((album, artist_detail, artist_index))
                album_source = album_rows_by_artist
                if not found_start_artist:
                    logging.warning(f"{LIGHT_YELLOW}Start artist ID {start_artist_id} was not found for album/song processing.{RESET}")
            else:
                if start_album_id:
                    logging.info(f"Searching album/song resume point: {start_album_id}")
                album_source = ((album, None, None) for album in self.iter_all_albums(page_size=page_size))

            started_albums = not start_album_id
            found_start_album = not start_album_id
            last_logged_album_artist_id = None
            for album, album_artist, album_artist_index in album_source:
                if not started_albums:
                    if album.get("id") != start_album_id:
                        continue
                    started_albums = True
                    found_start_album = True
                    logging.info(f"Found album/song resume point: {album.get('name', '')} ({album.get('id', '')})")
                if album_artist and "artist" not in entities and album_artist.get("id") != last_logged_album_artist_id:
                    log_blank_line()
                    log_artist_header(album_artist.get("name", ""), album_artist.get("id", ""), album_artist_index or 0)
                    last_logged_album_artist_id = album_artist.get("id")
                album_rows += 1
                album_rating = int(album.get("userRating") or 0)
                source_album_rating = album_rating
                if override_rating is not None:
                    album_rating = override_rating
                album_mbid = album.get("musicBrainzId") or ""
                if album_mbid:
                    logging.debug(f"Resolving release-group for album '{album.get('name','')}' release:{album_mbid}")
                    album_release_group_mbid = resolve_release_group_mbid(musicbrainz, album_mbid)
                    logging.debug(f"Resolved release-group for album '{album.get('name','')}' -> {album_release_group_mbid or 'NONE'}")
                else:
                    album_release_group_mbid = ""

                # If we're collecting songs we need the album detail to know whether
                # to print an album header (only print it when there are rated songs
                # or the album itself has a rating).
                album_detail = None
                has_rated_songs = False
                if "song" in entities:
                    logging.debug(f"Fetching album details for album id {album.get('id', '')}")
                    album_detail = self.get_album(album.get("id", ""))
                    for song in album_detail.get("song", []):
                            if int(song.get("userRating") or 0) > 0:
                                has_rated_songs = True
                                break

                # Print album header only if the album has a rating or contains rated songs
                if album_rating > 0 or has_rated_songs or ("album" in entities and include_unrated_albums and override_rating is not None):
                    yield RatingRow(
                        entity_type="album-boundary",
                        navidrome_id=album.get("id", ""),
                        mbid=album_release_group_mbid or "",
                        title=album.get("name", ""),
                        artist=album.get("artist", ""),
                        release_group_mbid=album_release_group_mbid or None,
                        release_mbid=album_mbid or None,
                        rating=0,
                    )
                    if override_rating is not None and source_album_rating != album_rating:
                        logging.info(f"    Override album rating: source s:{format_source_rating(source_album_rating)} -> forced s:{format_source_rating(album_rating)}")
                    log_album_header(
                        album.get("name", ""),
                        album.get("id", ""),
                        rating=album_rating,
                        has_rated_songs=has_rated_songs,
                        release_group_mbid=album_release_group_mbid or None,
                    )

                if "album" in entities:
                    if album_rating > 0 or (include_unrated_albums and override_rating is not None):
                        if album_release_group_mbid:
                            yield RatingRow(
                                entity_type="album",
                                navidrome_id=album.get("id", ""),
                                mbid=album_release_group_mbid,
                                title=album.get("name", ""),
                                artist=album.get("artist", ""),
                                release_group_mbid=album_release_group_mbid,
                                release_mbid=album_mbid or None,
                                rating=album_rating,
                            )
                            if max_albums is not None and album_rows >= max_albums:
                                break
                        else:
                            logging.warning(f"{LIGHT_YELLOW}Album '{album.get('name', '')}' has rating {album_rating} but no release-group MBID resolved.{RESET}")
                    else:
                        # If the album has no rating and no rated songs, log a compact skip line
                        if not has_rated_songs:
                            log_skip(album.get("name", ""), album_rating, entity="Album")

                if "song" not in entities:
                    continue

                # album_detail may already be fetched above
                if album_detail is None:
                    album_detail = self.get_album(album.get("id", ""))

                for song in album_detail.get("song", []):
                    song_rating = int(song.get("userRating") or 0)
                    # Count every track we inspect
                    try:
                        self.stats["tracks"] += 1
                    except Exception:
                        pass

                    if song_rating <= 0:
                        # Skipped due to no rating
                        try:
                            self.stats["skipped"] += 1
                        except Exception:
                            pass
                        logging.debug(f"nr:{song_rating:.1f} | Skipping Recording: {song.get('title', '')}")
                        continue
                    song_mbid = song.get("musicBrainzId") or ""
                    if not song_mbid:
                        # Rated but no direct MBID found. If we have a release-group
                        # match, still yield the row so the release-group expansion
                        # path can submit the rating instead of skipping it.
                        if album_release_group_mbid:
                            try:
                                self.stats["found"] += 1
                            except Exception:
                                pass
                            logging.debug(
                                f"{LIGHT_YELLOW}nr:{song_rating:.1f} | No song MBID; using release-group fallback for Recording: {song.get('title','')} ({song.get('id','')}){RESET}"
                            )
                            yield RatingRow(
                                entity_type="song",
                                navidrome_id=song.get("id", ""),
                                mbid="",
                                title=song.get("title", ""),
                                artist=song.get("artist", album.get("artist", "")),
                                release_group_mbid=album_release_group_mbid,
                                release_mbid=album_mbid or None,
                                rating=song_rating,
                            )
                            continue

                        # No direct MBID and no release-group to fall back to.
                        try:
                            self.stats["not_found"] += 1
                        except Exception:
                            pass
                        logging.debug(
                            f"{LIGHT_YELLOW}nr:{song_rating:.1f} | Rated but no MBID for Recording: {song.get('title','')} ({song.get('id','')}){RESET}"
                        )
                        continue

                    # We will yield a rated track that maps directly to a recording
                    try:
                        self.stats["found"] += 1
                    except Exception:
                        pass

                    yield RatingRow(
                        entity_type="song",
                        navidrome_id=song.get("id", ""),
                        mbid=song_mbid,
                        title=song.get("title", ""),
                        artist=song.get("artist", album.get("artist", "")),
                        release_group_mbid=album_release_group_mbid,
                        release_mbid=album_mbid or None,
                        rating=song_rating,
                    )

            if start_album_id and not found_start_album:
                logging.warning(f"{LIGHT_YELLOW}Start album ID {start_album_id} was not found.{RESET}")


class MusicBrainzClient:
    def __init__(self, client: str, username: str | None, password: str | None) -> None:
        self.client = client
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": client, "Accept": "application/json"})
        if username and password:
            self.session.auth = HTTPDigestAuth(username, password)
        self._next_request_at = 0.0

    def _throttle(self) -> None:
        now = time.monotonic()
        if now < self._next_request_at:
            time.sleep(self._next_request_at - now)
        self._next_request_at = time.monotonic() + 1.05

    def get_json(self, path: str, params: dict[str, object], allow_404: bool = False) -> dict:
        """Make a MusicBrainz API GET request with retry logic and exponential backoff."""
        retry_delays = MUSICBRAINZ_RETRY_DELAYS
        max_retries = len(retry_delays)
        exhausted_retriable = False
        
        for attempt in range(max_retries):
            try:
                self._throttle()
                # Ensure params are strings to satisfy requests parameter types
                safe_params = {k: str(v) for k, v in params.items() if v is not None}
                response = self.session.get(f"{MUSICBRAINZ_BASE_URL}{path}", params=safe_params, timeout=20)
                
                # Handle rate limiting
                if response.status_code == 429:
                    retry_after = int(response.headers.get('Retry-After', 5))
                    logging.warning(f"{LIGHT_YELLOW}MusicBrainz rate limited. Retrying after {retry_after} seconds...{RESET}")
                    time.sleep(retry_after)
                    continue
                
                # Handle server errors with retry
                if response.status_code >= 500:
                    if attempt >= max_retries - 1:
                        exhausted_retriable = True
                        logging.warning(f"{LIGHT_YELLOW}MusicBrainz server error {response.status_code}. Attempt {attempt + 1}/{max_retries}. No retries left.{RESET}")
                        continue
                    wait_time = retry_delays[attempt]
                    logging.warning(f"{LIGHT_YELLOW}MusicBrainz server error {response.status_code}. Attempt {attempt + 1}/{max_retries}. Waiting {wait_time}s...{RESET}")
                    time.sleep(wait_time)
                    continue
                
                if response.status_code == 404 and allow_404:
                    logging.warning(f"{LIGHT_YELLOW}MusicBrainz resource not found for {path}. Skipping.{RESET}")
                    return {}

                response.raise_for_status()
                return response.json()
                
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
                short = _format_conn_error(e, "MusicBrainz")
                if attempt >= max_retries - 1:
                    exhausted_retriable = True
                    logging.warning(f"{LIGHT_YELLOW}{short}. Attempt {attempt + 1}/{max_retries}. No retries left.{RESET}")
                    continue
                wait_time = retry_delays[attempt]
                logging.warning(f"{LIGHT_YELLOW}{short}. Attempt {attempt + 1}/{max_retries}. Waiting {wait_time}s...{RESET}")
                time.sleep(wait_time)
                continue
            except requests.exceptions.RequestException as e:
                logging.error(f"{LIGHT_RED}MusicBrainz request failed: {e}{RESET}")
                break
        
        # If a transient MusicBrainz lookup keeps failing, skip that lookup and keep the run alive.
        if exhausted_retriable:
            logging.error(f"{LIGHT_RED}Skipping MusicBrainz path after {max_retries} attempts: {path}{RESET}")
            return {}

        logging.error(f"{LIGHT_RED}Failed after {max_retries} attempts for MusicBrainz path: {path}{RESET}")
        raise SystemExit(1)

    def iter_artist_release_groups(self, artist_mbid: str, page_size: int = 100):
        """Yield all release-groups credited to a MusicBrainz artist."""
        offset = 0
        while True:
            payload = self.get_json(
                "/release-group",
                {
                    "artist": artist_mbid,
                    "fmt": "json",
                    "limit": page_size,
                    "offset": offset,
                    "inc": "artist-credits",
                },
                allow_404=True,
            )
            release_groups = payload.get("release-groups", [])
            if not release_groups:
                break
            for release_group in release_groups:
                yield release_group
            if len(release_groups) < page_size:
                break
            offset += len(release_groups)

    def post_xml(self, path: str, xml_body: bytes) -> requests.Response:
        """Make a MusicBrainz API POST request with retry logic and exponential backoff."""
        retry_delays = MUSICBRAINZ_RETRY_DELAYS
        max_retries = len(retry_delays)
        
        for attempt in range(max_retries):
            try:
                self._throttle()
                headers = {"Content-Type": "application/xml; charset=utf-8"}
                response = self.session.post(
                    f"{MUSICBRAINZ_BASE_URL}{path}",
                    params={"client": self.client},
                    data=xml_body,
                    headers=headers,
                    timeout=20,
                )
                
                # Handle rate limiting
                if response.status_code == 429:
                    retry_after = int(response.headers.get('Retry-After', 5))
                    logging.warning(f"{LIGHT_YELLOW}MusicBrainz rate limited. Retrying after {retry_after} seconds...{RESET}")
                    time.sleep(retry_after)
                    continue
                
                # Handle server errors with retry
                if response.status_code >= 500:
                    if attempt >= max_retries - 1:
                        logging.warning(f"{LIGHT_YELLOW}MusicBrainz server error {response.status_code}. Attempt {attempt + 1}/{max_retries}. No retries left.{RESET}")
                        continue
                    wait_time = retry_delays[attempt]
                    logging.warning(f"{LIGHT_YELLOW}MusicBrainz server error {response.status_code}. Attempt {attempt + 1}/{max_retries}. Waiting {wait_time}s...{RESET}")
                    time.sleep(wait_time)
                    continue
                
                if response.status_code == 400:
                    raise MusicBrainzBadRequest(response)

                response.raise_for_status()
                return response
                
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
                short = _format_conn_error(e, "MusicBrainz")
                if attempt >= max_retries - 1:
                    logging.warning(f"{LIGHT_YELLOW}{short}. Attempt {attempt + 1}/{max_retries}. No retries left.{RESET}")
                    continue
                wait_time = retry_delays[attempt]
                logging.warning(f"{LIGHT_YELLOW}{short}. Attempt {attempt + 1}/{max_retries}. Waiting {wait_time}s...{RESET}")
                time.sleep(wait_time)
                continue
            except requests.exceptions.RequestException as e:
                logging.error(f"{LIGHT_RED}MusicBrainz request failed: {e}{RESET}")
                break
        
        # If we get here, all retries failed
        logging.error(f"{LIGHT_RED}Failed after {max_retries} attempts for MusicBrainz path: {path}{RESET}")
        raise SystemExit(1)


def resolve_release_group_mbid(client: MusicBrainzClient, release_mbid: str) -> str:
    payload = client.get_json(
        f"/release/{release_mbid}",
        {"inc": "release-groups", "fmt": "json"},
        allow_404=True,
    )
    return payload.get("release-group", {}).get("id", "")


def release_group_recording_ids(
    client: MusicBrainzClient,
    release_group_mbid: str,
    title: str,
    artist: str,
    release_mbid: str | None = None,
) -> list[str]:
    if not release_group_mbid:
        return []

    wanted_title = normalize_text(title)
    wanted_artist = normalize_text(artist)
    candidates: list[str] = []
    candidate_titles: list[str] = []
    seen: set[str] = set()

    offset = 0
    while True:
        payload = client.get_json(
            "/release",
            {
                "release-group": release_group_mbid,
                "inc": "recordings+artist-credits",
                "fmt": "json",
                "limit": 100,
                "offset": offset,
            },
            allow_404=True,
        )
        releases = payload.get("releases", [])
        if not releases:
            # Diagnostic: show what the API returned so we can debug empty results
            try:
                keys = ",".join(sorted(payload.keys())) if isinstance(payload, dict) else str(type(payload))
            except Exception:
                keys = "(unprintable)"
            logging.info(
                f"{LIGHT_YELLOW}No releases found from /release for RG {release_group_mbid}. Payload keys: {keys}{RESET}"
            )
        if not releases:
            # Try a fallback to the release-group endpoint if /release
            # returned no releases (sometimes the API shape differs).
            logging.debug(f"No releases from /release for RG {release_group_mbid}; trying /release-group fallback")
            fallback = client.get_json(
                f"/release-group/{release_group_mbid}",
                {"inc": "releases+media+recordings+artist-credits", "fmt": "json"},
                allow_404=True,
            )
            releases = fallback.get("releases", []) if isinstance(fallback, dict) else []
            if not releases:
                try:
                    keys = ",".join(sorted(fallback.keys())) if isinstance(fallback, dict) else str(type(fallback))
                except Exception:
                    keys = "(unprintable)"
                logging.info(
                    f"{LIGHT_YELLOW}No releases found from /release-group for RG {release_group_mbid}. Payload keys: {keys}{RESET}"
                )
            if not releases:
                # If we have a specific release MBID (album-level MBID), try
                # fetching that release directly as a last resort.
                if release_mbid:
                    logging.debug(f"No releases in RG; trying specific release {release_mbid}")
                    rel_payload = client.get_json(
                        f"/release/{release_mbid}",
                        {"inc": "recordings+media+artist-credits", "fmt": "json"},
                        allow_404=True,
                    )
                    # Extract tracks from release media
                    for medium in rel_payload.get("media", []):
                        for track in medium.get("tracks", []):
                            recording = track.get("recording") or {}
                            recording_id = recording.get("id")
                            if not recording_id or recording_id in seen:
                                continue
                            track_title = normalize_text(track.get("title"))
                            track_artist = normalize_text(
                                " ".join(
                                    credit.get("name", "")
                                    for credit in track.get("artist-credit", [])
                                    if isinstance(credit, dict)
                                )
                            )
                            recording_artist = normalize_text(
                                " ".join(
                                    credit.get("name", "")
                                    for credit in recording.get("artist-credit", [])
                                    if isinstance(credit, dict)
                                )
                            )
                            if wanted_title and track_title and track_title != wanted_title:
                                continue
                            if wanted_artist and track_artist and track_artist != wanted_artist and recording_artist and recording_artist != wanted_artist:
                                artist_matches = (
                                    not wanted_artist
                                    or wanted_artist in track_artist
                                    or wanted_artist in recording_artist
                                    or track_artist in wanted_artist
                                    or recording_artist in wanted_artist
                                )
                                if not artist_matches:
                                    continue
                            seen.add(recording_id)
                            candidates.append(recording_id)
                            candidate_titles.append(track.get('title',''))
                    if candidates:
                        break
                break

        for release in releases:
            for medium in release.get("media", []):
                for track in medium.get("tracks", []):
                    recording = track.get("recording") or {}
                    recording_id = recording.get("id")
                    if not recording_id or recording_id in seen:
                        continue

                    track_title = normalize_text(track.get("title"))
                    if track_title and wanted_title and track_title != wanted_title:
                        continue

                    track_artist = normalize_text(
                        " ".join(
                            credit.get("name", "")
                            for credit in track.get("artist-credit", [])
                            if isinstance(credit, dict)
                        )
                    )
                    recording_artist = normalize_text(
                        " ".join(
                            credit.get("name", "")
                            for credit in recording.get("artist-credit", [])
                            if isinstance(credit, dict)
                        )
                    )

                    artist_matches = (
                        not wanted_artist
                        or wanted_artist in track_artist
                        or wanted_artist in recording_artist
                        or track_artist in wanted_artist
                        or recording_artist in wanted_artist
                    )
                    if not artist_matches:
                        continue

                    seen.add(recording_id)
                    candidates.append(recording_id)
                    candidate_titles.append(track.get('title',''))

        if len(releases) < 100:
            break
        offset += len(releases)

    # If we found no candidates for an expected title, log a concise
    # diagnostic listing a few candidate track titles to help spot
    # normalization or punctuation mismatches.
    if not candidates and wanted_title:
        sample = ", ".join([t for t in candidate_titles[:10]]) or "(no tracks found)"
        logging.info(
            f"{LIGHT_YELLOW}No recordings matched title '{title}' (normalized '{wanted_title}') in RG {release_group_mbid}. Candidate titles: {sample}{RESET}"
        )

    return candidates


def build_submission(rows: list[tuple[str, str, int]]) -> bytes:
    root = ET.Element("metadata", {"xmlns": MUSICBRAINZ_XML_NS})
    grouped: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for entity_type, entity_id, rating in rows:
        grouped[entity_type].append((entity_id, rating))

    entity_tags = {
        "artist": "artist-list",
        "recording": "recording-list",
        "release": "release-list",
        "release-group": "release-group-list",
        "work": "work-list",
    }

    for entity_type in ["artist", "recording", "release", "release-group", "work"]:
        values = grouped.get(entity_type, [])
        if not values:
            continue
        entity_list = ET.SubElement(root, entity_tags[entity_type])
        for entity_id, rating in values:
            entity = ET.SubElement(entity_list, entity_type, {"id": entity_id})
            user_rating = ET.SubElement(entity, "user-rating")
            user_rating.text = str(rating)

    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def prepare_target_row(
    row: RatingRow,
    client: MusicBrainzClient,
    expand_release_groups: bool,
    ) -> list[PreparedRow]:
    prepared: list[PreparedRow] = []
    rating = rating_to_musicbrainz(row.rating)
    if rating <= 0:
        return prepared

    targets: list[tuple[str, str]] = []

    if row.entity_type == "song":
        if row.mbid:
            targets.append(("recording", row.mbid))
        if expand_release_groups and row.release_group_mbid:
            logging.debug(f"Expanding release-group {row.release_group_mbid} for '{row.title}' / '{row.artist}'")
            recordings = release_group_recording_ids(
                client, row.release_group_mbid, row.title, row.artist, getattr(row, "release_mbid", None)
            )
            logging.debug(f"Expanded release-group {row.release_group_mbid}: {len(recordings)} recordings")
            for recording_id in recordings:
                targets.append(("recording", recording_id))
        if not targets and row.mbid:
            targets.append(("recording", row.mbid))

    elif row.entity_type == "album":
        if row.mbid:
            targets.append(("release-group", row.mbid))
        if row.release_group_mbid and row.release_group_mbid != row.mbid:
            targets.append(("release-group", row.release_group_mbid))

    elif row.entity_type == "artist":
        if row.mbid:
            targets.append(("artist", row.mbid))

    seen_targets: set[tuple[str, str]] = set()
    for entity_type, entity_id in targets:
        if not entity_id:
            continue
        key = (entity_type, entity_id)
        if key in seen_targets:
            continue
        seen_targets.add(key)
        prepared.append((entity_type, entity_id, rating, row))

    return prepared


def format_source_rating(value: int | None) -> str:
    if value is None:
        return "n/a"
    return str(value)


def format_musicbrainz_rating(value: int | None) -> str:
    if value is None:
        return "n/a"
    return str(value)


def rating_entity_label(entity_type: str) -> str:
    return {
        "artist": "Artist",
        "album": "Album",
        "song": "Recording",
    }.get(entity_type, entity_type.capitalize())


def log_rating_result(
    row: RatingRow,
    mb_rating: int,
    status: int | str,
    *,
    color: str = LIGHT_GREEN,
) -> None:
    title = row.title or row.artist or row.mbid
    artist = row.artist or ""
    label = rating_entity_label(row.entity_type)
    logging.info(
        f"{color}{label}: s:{format_source_rating(row.rating)} -> mb:{format_musicbrainz_rating(mb_rating)} | {title} / {artist}: {status}{RESET}"
    )


def submit_rating_values(
    client: MusicBrainzClient,
    entity_type: str,
    values: list[tuple[str, int, RatingRow]],
) -> int:
    submission_rows = [(entity_type, entity_id, rating) for entity_id, rating, _ in values]
    xml_body = build_submission(submission_rows)

    try:
        response = client.post_xml("/rating", xml_body)
    except MusicBrainzBadRequest as exc:
        if len(values) == 1:
            entity_id, mb_rating, row = values[0]
            logging.warning(
                f"{LIGHT_YELLOW}{rating_entity_label(row.entity_type)}: s:{format_source_rating(row.rating)} -> "
                f"mb:{format_musicbrainz_rating(mb_rating)} | Skipping invalid MusicBrainz target "
                f"{entity_type}:{entity_id} | {row.title or row.artist} / {row.artist}: "
                f"{exc.response.status_code}{RESET}"
            )
            return 0

        split_at = len(values) // 2
        logging.warning(
            f"{LIGHT_YELLOW}MusicBrainz rejected {len(values)} {entity_type} ratings with 400. "
            f"Isolating invalid target...{RESET}"
        )
        return submit_rating_values(client, entity_type, values[:split_at]) + submit_rating_values(
            client, entity_type, values[split_at:]
        )

    for entity_id, mb_rating, row in values:
        log_rating_result(row, mb_rating, response.status_code)

    logging.info(f"{LIGHT_GREEN}Submitted {len(values)} {entity_type} ratings: {response.status_code}{RESET}")
    return len(values)


def submit_ratings(client: MusicBrainzClient, rows: list[PreparedRow], dry_run: bool) -> SubmissionCounts:
    # Group values by entity type but keep the original RatingRow for logging
    grouped_by_type: dict[str, list[tuple[str, int, RatingRow]]] = defaultdict(list)
    for entity_type, entity_id, rating, row in rows:
        grouped_by_type[entity_type].append((entity_id, rating, row))

    # Collect counts per entity type to return to the caller
    counts = empty_submission_counts()

    # Submit artist ratings first, then release-groups (albums), then recordings.
    for entity_type in ["artist", "release-group", "recording"]:
        values = grouped_by_type.get(entity_type, [])
        if not values:
            continue

        # For release-group batches, emit a concise summary of how many
        # rg_variants are in this submission batch (only for real runs).
        if entity_type == "release-group" and not dry_run:
            logging.info(f"{LIGHT_BLUE}rg_variants in this batch: {len(values)}{RESET}")

        if dry_run:
            for _, mb_rating, row in values:
                log_rating_result(row, mb_rating, "dry-run")
            logging.info(f"{LIGHT_GREEN}Submitted {len(values)} {entity_type} ratings: dry-run{RESET}")
            counts[entity_type] = len(values)
            continue

        counts[entity_type] = submit_rating_values(client, entity_type, values)

    return counts


def flush_submission_buffer(
    buffer: list[PreparedRow],
    client: MusicBrainzClient,
    dry_run: bool,
) -> SubmissionCounts:
    if not buffer:
        return empty_submission_counts()
    # Deduplicate buffer by (entity_type, entity_id), keeping first occurrence.
    deduped: list[PreparedRow] = []
    seen: set[tuple[str, str]] = set()
    for entity_type, entity_id, rating, row in buffer:
        key = (entity_type, entity_id)
        if key in seen:
            continue
        seen.add(key)
        deduped.append((entity_type, entity_id, rating, row))

    counts = submit_ratings(client, deduped, dry_run)
    buffer.clear()
    return counts

def musicbrainz_artist_release_group_rows(
    client: MusicBrainzClient,
    artist_mbid: str,
    rating: int,
    max_release_groups: int | None = None,
):
    """Build album-style rows from MusicBrainz release-groups for one artist."""
    seen: set[str] = set()
    count = 0
    logging.info(f"Fetching MusicBrainz release-groups for artist MBID: {artist_mbid}")
    for release_group in client.iter_artist_release_groups(artist_mbid):
        release_group_mbid = release_group.get("id") or ""
        if not release_group_mbid or release_group_mbid in seen:
            continue
        seen.add(release_group_mbid)
        artist_name = "".join(
            str(credit.get("name", ""))
            for credit in release_group.get("artist-credit", [])
            if isinstance(credit, dict)
        ).strip()
        title = release_group.get("title", "")
        logging.info(
            f"  Album: {title} | mbidRG:{release_group_mbid} | forced s:{format_source_rating(rating)}"
        )
        yield RatingRow(
            entity_type="album",
            navidrome_id=release_group_mbid,
            mbid=release_group_mbid,
            title=title,
            artist=artist_name,
            release_group_mbid=release_group_mbid,
            release_mbid=None,
            rating=rating,
        )
        count += 1
        if max_release_groups is not None and count >= max_release_groups:
            break

def album_batch_key(row: RatingRow) -> str:
    return row.release_mbid or row.release_group_mbid or row.navidrome_id


def log_missing_target(row: RatingRow) -> None:
    title = row.title or ""
    artist = row.artist or ""
    mbid_field = f" | mbid:{row.mbid}" if row.mbid else ""
    logging.info(
        f"    {LIGHT_YELLOW}{rating_entity_label(row.entity_type)}: s:{format_source_rating(row.rating)} -> mb:0 | (not found) | {title} / {artist} ({row.navidrome_id}){mbid_field}{RESET}"
    )


def log_prepared_targets_debug(row: RatingRow, prepared_rows: list[PreparedRow]) -> None:
    if row.entity_type != "song":
        return
    recording_ids = [
        entity_id
        for entity_type, entity_id, _, _ in prepared_rows
        if entity_type == "recording"
    ]
    recording_list = ", ".join(recording_ids) if recording_ids else "(none)"
    mbid_field = f" | mbid:{row.mbid}" if row.mbid else ""
    logging.debug(
        f"    {LIGHT_BLUE}Song: {row.title} ({row.navidrome_id}){mbid_field} | matched_recordings:{len(recording_ids)} | recordings:{recording_list}{RESET}"
    )


def flush_and_count(
    buffer: list[PreparedRow],
    client: MusicBrainzClient,
    dry_run: bool,
    submitted_counts: SubmissionCounts,
) -> None:
    add_submission_counts(
        submitted_counts,
        flush_submission_buffer(buffer, client, dry_run),
    )


def standalone_main() -> int:
    args = parse_args()
    load_dotenv_file()
    os.makedirs(LOG_DIR, exist_ok=True)

    # Start time for run duration reporting
    start_time = time.time()

    # Set up the stream handler (console logging) without timestamp
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(SafeAsciiFormatter("%(message)s"))
    logging.basicConfig(level=getattr(logging, args.log_level), handlers=[console_handler])

    # Set up the file handler (file logging) with timestamp, matching sptnr.py
    file_handler = logging.FileHandler(LOGFILE, "a", encoding="ascii", errors="backslashreplace")
    file_handler.setFormatter(SafeAsciiFormatter("[%(asctime)s] %(message)s"))
    logging.getLogger().addHandler(file_handler)
    # Ensure colorama auto-reset for console
    try:
        init(autoreset=True)
    except Exception:
        pass

    logging.info(f"Version: musicbrainz-ratings {SCRIPT_VERSION}")
    if args.dry_run:
        logging.info("Preview mode, no changes will be made.")

    force_artist_ratings_mode = bool(args.force_artist_ratings)
    mb_username = args.mb_username or os.environ.get("MB_USERNAME")
    mb_password = args.mb_password or os.environ.get("MB_TOKEN") or os.environ.get("MB_PASSWORD")

    default_entities = ["album"] if force_artist_ratings_mode else (["song", "album"] if args.start_album_id else ["song", "album", "artist"])
    entities = set(args.entity or default_entities)
    if args.override_rating is not None and not 0 <= args.override_rating <= 5:
        logging.error("--override-rating must be between 0 and 5.")
        return 2
    if args.include_unrated_albums and args.override_rating is None:
        logging.error("--include-unrated-albums requires --override-rating.")
        return 2
    if force_artist_ratings_mode:
        if entities != {"album"}:
            logging.error("--force-artist-ratings supports album/release-group ratings only. Use --entity album or omit --entity.")
            return 2
        if args.override_rating is None:
            logging.error("--force-artist-ratings requires --override-rating.")
            return 2
        if any([args.artist_id, args.start_artist_id, args.start_album_id]):
            logging.error("--force-artist-ratings cannot be combined with Navidrome artist/resume filters.")
            return 2
    if args.override_rating is not None:
        logging.info(f"Overriding submitted source ratings to s:{format_source_rating(args.override_rating)} / mb:{format_musicbrainz_rating(rating_to_musicbrainz(args.override_rating))}.")

    client = MusicBrainzClient(CLIENT_NAME, mb_username, mb_password)
    navidrome: NavidromeClient | None = None
    if not force_artist_ratings_mode:
        navidrome_base_url = required_arg(
            args.navidrome_base_url,
            "NAVIDROME_BASE_URL",
            "navidrome-base-url",
        )
        navidrome_username = required_arg(
            args.navidrome_username,
            "NAVIDROME_USERNAME",
            "navidrome-username",
        )
        navidrome_password = required_arg(
            args.navidrome_password,
            "NAVIDROME_PASSWORD",
            "navidrome-password",
        )
        navidrome = NavidromeClient(
            navidrome_base_url,
            navidrome_username,
            navidrome_password,
            CLIENT_NAME,
        )
    total_artists: int | None = 1 if force_artist_ratings_mode else None
    if not force_artist_ratings_mode and "artist" in entities:
        assert navidrome is not None
        if args.artist_id:
            total_artists = 1
        else:
            try:
                total_artists = len(navidrome.get_artists())
            except SystemExit:
                raise
            except Exception as exc:
                logging.warning(f"Could not determine total artists to process: {exc}")
    if total_artists is not None:
        logging.info(f"Total artists to process: {total_artists}")
    submission_buffer: list[PreparedRow] = []
    submit_batch_size = 100
    resolved_any = False
    submitted_counts = empty_submission_counts()
    release_groups_total: set[str] = set()
    album_count = 0
    current_album_key: str | None = None

    if force_artist_ratings_mode:
        row_source = musicbrainz_artist_release_group_rows(
            client,
            args.force_artist_ratings,
            args.override_rating,
            args.max_release_groups,
        )
    else:
        assert navidrome is not None
        row_source = navidrome.build_rows(
            entities,
            client,
            args.max_artists,
            args.max_albums,
            args.artist_id,
            args.start_artist_id,
            args.start_album_id,
            args.override_rating,
            args.include_unrated_albums,
        )

    for row in row_source:
        if row.entity_type == "album-boundary":
            if submission_buffer:
                flush_and_count(submission_buffer, client, args.dry_run, submitted_counts)
            current_album_key = album_batch_key(row)
            continue

        prepared_rows = prepare_target_row(row, client, args.expand_release_groups)
        if not prepared_rows:
            log_missing_target(row)
            continue
        resolved_any = True

        if row.entity_type == "artist":
            if submission_buffer:
                flush_and_count(submission_buffer, client, args.dry_run, submitted_counts)
                current_album_key = None
            add_submission_counts(submitted_counts, submit_ratings(client, prepared_rows, args.dry_run))
            continue

        if row.entity_type in {"album", "song"}:
            next_album_key = album_batch_key(row)
            if current_album_key is None:
                current_album_key = next_album_key
            elif next_album_key != current_album_key:
                flush_and_count(submission_buffer, client, args.dry_run, submitted_counts)
                current_album_key = next_album_key

        rg_ids = {entity_id for entity_type, entity_id, _, _ in prepared_rows if entity_type == "release-group"}
        log_prepared_targets_debug(row, prepared_rows)

        # Track release-groups seen during the run
        for rg in rg_ids:
            if rg:
                release_groups_total.add(rg)
        # Track album count when a source album produced targets
        if row.entity_type == "album" and prepared_rows:
            album_count += 1

        # Always buffer prepared rows; `submit_ratings()` handles formatting
        # so preview mode will match the real-submission output.
        submission_buffer.extend(prepared_rows)
        if len(submission_buffer) >= submit_batch_size:
            flush_and_count(submission_buffer, client, args.dry_run, submitted_counts)

    if submission_buffer:
        flush_and_count(submission_buffer, client, args.dry_run, submitted_counts)

    if not resolved_any:
        logging.info(f"{LIGHT_RED}No MusicBrainz targets could be resolved.{RESET}")
        return 0

    # Emit a concise run summary (Tracks / Found / Skipped / Not Found / Match% / Time)
    elapsed = time.time() - start_time
    minutes = int(elapsed // 60)
    seconds = int(elapsed % 60)
    tracks = getattr(navidrome, "stats", {}).get("tracks", 0)
    found = getattr(navidrome, "stats", {}).get("found", 0)
    skipped = getattr(navidrome, "stats", {}).get("skipped", 0)
    not_found = getattr(navidrome, "stats", {}).get("not_found", 0)
    match_pct = (found / tracks * 100.0) if tracks else 0.0
    release_groups_count = len(release_groups_total)
    artists_count = total_artists if total_artists is not None else getattr(navidrome, "stats", {}).get("artists", 0)
    submitted_label = "Previewed" if args.dry_run else "Submitted"
    logging.info(
        f"Artists: {artists_count} | Albums: {album_count} | rg_variants: {release_groups_count} | Tracks: {tracks} | Found: {found} | Skipped: {skipped} | Not Found: {not_found} | Match: {match_pct:.1f}% | {submitted_label}: Artists {submitted_counts['artist']}, Albums {submitted_counts['release-group']}, Recordings {submitted_counts['recording']} | Time: {minutes}m {seconds}s"
    )

    return 0


def main() -> int:
    import sys
    if len(sys.argv) > 1 and sys.argv[1] in {"enqueue", "work", "library"}:
        load_dotenv_file()
        from .ratings_worker import main as sync_main
        return sync_main()
    return standalone_main()


if __name__ == "__main__":
    raise SystemExit(main())

