#!/usr/bin/env python3
"""Export Last.fm genres for Lidarr albums to MusicBrainz entities."""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import time
import unicodedata
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path
from typing import Iterable

import requests
from requests.auth import HTTPDigestAuth


MB_BASE = "https://musicbrainz.org/ws/2"
LASTFM_BASE = "https://ws.audioscrobbler.com/2.0/"
MB_NS = "http://musicbrainz.org/ns/mmd-2.0#"
CLIENT = "musicbrainz-helper/0.2"
RETRY_DELAYS = (1, 3, 10, 30)
ENTITY_LISTS = {
    "artist": "artist-list",
    "release-group": "release-group-list",
    "release": "release-list",
    "recording": "recording-list",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Upvote Last.fm genres on MusicBrainz entities represented in Lidarr."
    )
    parser.add_argument("--scope", choices=("missing", "present", "all"), default="missing")
    parser.add_argument("--entity", action="append", choices=tuple(ENTITY_LISTS))
    parser.add_argument("--max-albums", type=int)
    parser.add_argument("--max-tags", type=int, default=7)
    parser.add_argument("--artist-id", help="Limit processing to one Lidarr artist ID or MusicBrainz artist MBID.")
    parser.add_argument("--start-artist-id", help="Resume at the first album for this Lidarr artist ID or artist MBID.")
    parser.add_argument("--start-album-id", help="Resume at this Lidarr album ID or MusicBrainz release-group MBID.")
    parser.add_argument("--resume", action="store_true", help="Skip album IDs recorded in progress.json.")
    parser.add_argument(
        "--name-fallback", action=argparse.BooleanOptionalAction, default=False,
        help="Fall back to ambiguous Last.fm name lookups when an MBID lookup has no tags.",
    )
    parser.add_argument("--checkpoint-every", type=int, default=25, help="Checkpoint reports and progress every N albums.")
    parser.add_argument("--submit-batch-size", type=int, default=100, help="Maximum genre votes per MusicBrainz POST.")
    parser.add_argument("--output-dir", type=Path, default=Path("lidarr-lastfm-genre-reports"))
    parser.add_argument("--submit", action="store_true", help="Submit genre upvotes to MusicBrainz.")
    parser.add_argument("--log-level", choices=("DEBUG", "INFO", "WARNING", "ERROR"), default="INFO")
    return parser.parse_args()


def configure_logging(level: str) -> Path:
    log_dir = Path("logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"lidarr-lastfm-genres-helper_{int(time.time())}.log"
    formatter = logging.Formatter("%(asctime)s %(levelname)s: %(message)s")
    stream = logging.StreamHandler()
    stream.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logging.basicConfig(level=getattr(logging, level), handlers=(stream, file_handler), force=True)
    return log_path


def load_dotenv(path: Path = Path(".env")) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(f"{name} is required in .env or the environment.")
    return value


def normalized(value: str) -> str:
    folded = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "", folded.casefold())


class JsonCache:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}

    def get(self, namespace: str, key: str):
        return self.data.get(namespace, {}).get(key)

    def put(self, namespace: str, key: str, value) -> None:
        self.data.setdefault(namespace, {})[key] = value
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2, sort_keys=True), encoding="utf-8")


class LidarrClient:
    def __init__(self, base_url: str, api_key: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({"X-Api-Key": api_key, "Accept": "application/json"})

    def get(self, endpoint: str, **params):
        for attempt, delay in enumerate(RETRY_DELAYS, start=1):
            try:
                response = self.session.get(f"{self.base_url}/api/v1/{endpoint}", params=params, timeout=60)
                if response.status_code in {429, 500, 502, 503, 504}:
                    if attempt < len(RETRY_DELAYS):
                        time.sleep(int(response.headers.get("Retry-After", delay)))
                        continue
                response.raise_for_status()
                return response.json()
            except (requests.ConnectionError, requests.Timeout):
                if attempt == len(RETRY_DELAYS):
                    raise RuntimeError(f"Lidarr request failed after {attempt} attempts: {endpoint}") from None
                time.sleep(delay)
        raise RuntimeError(f"Lidarr request failed: {endpoint}")

    def all_albums(self) -> list[dict]:
        payload = self.get("album", includeAllArtistAlbums="true")
        return payload if isinstance(payload, list) else payload.get("records", [])

    def missing_albums(self, limit: int | None = None) -> list[dict]:
        return list(self.iter_missing_albums(limit))

    def iter_missing_albums(self, limit: int | None = None):
        yielded = 0
        page = 1
        while limit is None or yielded < limit:
            page_size = min(250, limit - yielded) if limit is not None else 250
            payload = self.get(
                "wanted/missing", page=page, pageSize=page_size, sortKey="title",
                sortDirection="ascending", includeArtist="true",
            )
            batch = payload.get("records", [])
            if not batch:
                break
            for album in batch:
                yield album
                yielded += 1
                if limit is not None and yielded >= limit:
                    return
            if yielded >= int(payload.get("totalRecords", yielded)):
                break
            page += 1

    def albums(self, scope: str, limit: int | None = None) -> list[dict]:
        if scope == "all":
            return self.all_albums()[:limit]
        missing = self.missing_albums(limit if scope == "missing" else None)
        if scope == "missing":
            return missing
        missing_ids = {
            str(album.get("id") or album.get("foreignAlbumId") or "") for album in missing
        }
        albums = [
            album for album in self.all_albums()
            if str(album.get("id") or album.get("foreignAlbumId") or "") not in missing_ids
        ]
        return albums[:limit]

    def iter_albums(self, scope: str):
        if scope == "missing":
            yield from self.iter_missing_albums()
        else:
            yield from self.albums(scope)


class LastFmClient:
    def __init__(self, api_key: str, cache: JsonCache) -> None:
        self.api_key = api_key
        self.cache = cache
        self.session = requests.Session()
        self.next_request = 0.0

    def _request(self, params: dict[str, str]) -> dict:
        for attempt, delay in enumerate(RETRY_DELAYS, start=1):
            wait = self.next_request - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self.next_request = time.monotonic() + 0.25
            try:
                response = self.session.get(LASTFM_BASE, params=params, timeout=30)
                if response.status_code in {429, 500, 502, 503, 504}:
                    if attempt < len(RETRY_DELAYS):
                        time.sleep(int(response.headers.get("Retry-After", delay)))
                        continue
                try:
                    payload = response.json()
                except requests.JSONDecodeError:
                    payload = {}
                if payload.get("error"):
                    if int(payload.get("error", 0) or 0) in {11, 16, 29} and attempt < len(RETRY_DELAYS):
                        time.sleep(delay)
                        continue
                    return payload
                response.raise_for_status()
                return payload
            except (requests.ConnectionError, requests.Timeout):
                if attempt == len(RETRY_DELAYS):
                    raise RuntimeError(f"Last.fm request failed after {attempt} attempts.") from None
                time.sleep(delay)
            except requests.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else "unknown"
                raise RuntimeError(f"Last.fm returned HTTP {status}.") from None
            except requests.RequestException:
                raise RuntimeError("Last.fm request failed.") from None
        raise RuntimeError("Last.fm request failed.")

    def top_tags(
        self,
        entity: str,
        artist: str,
        name: str = "",
        mbid: str = "",
        name_fallback: bool = False,
    ) -> list[dict]:
        identity = f"mbid:{mbid}" if mbid else f"name:{normalized(artist)}|{normalized(name)}"
        key = f"v2|{identity}|fallback:{int(name_fallback)}"
        cached = self.cache.get(f"lastfm-{entity}", key)
        if cached is not None:
            return cached

        params = {"method": f"{entity}.getTopTags", "api_key": self.api_key, "format": "json", "autocorrect": "0"}
        if mbid:
            params["mbid"] = mbid
        else:
            params["artist"] = artist
            if entity == "album":
                params["album"] = name
            elif entity == "track":
                params["track"] = name
        payload = self._request(params)
        tags = payload.get("toptags", {}).get("tag", []) if not payload.get("error") else []

        if mbid and not tags and name_fallback:
            logging.warning("Last.fm %s MBID lookup had no tags; falling back to names for %s / %s", entity, artist, name)
            tags = self.top_tags(entity, artist, name, name_fallback=False)
        if payload.get("error"):
            logging.warning(
                "Last.fm %s lookup failed for %s / %s%s: %s",
                entity, artist, name, f" ({mbid})" if mbid else "", payload.get("message"),
            )
        self.cache.put(f"lastfm-{entity}", key, tags)
        return tags

    def verified_info_tags(
        self,
        entity: str,
        artist: str,
        name: str,
        valid_mbids: set[str],
        name_fallback: bool = False,
        warn: bool = True,
    ) -> tuple[list[dict], str]:
        key = f"v1|{normalized(artist)}|{normalized(name)}"
        cached = self.cache.get(f"lastfm-{entity}-info", key)
        if cached is None:
            params = {
                "method": f"{entity}.getInfo", "artist": artist,
                entity: name, "api_key": self.api_key, "format": "json", "autocorrect": "0",
            }
            payload = self._request(params)
            cached = payload.get(entity, {}) if not payload.get("error") else {}
            self.cache.put(f"lastfm-{entity}-info", key, cached)

        returned_mbid = str(cached.get("mbid", "")).strip()
        valid = {value.casefold() for value in valid_mbids if value}
        if returned_mbid.casefold() in valid:
            tags = (cached.get("tags") or cached.get("toptags") or {}).get("tag", [])
            return tags, f"lastfm:{entity}-verified"

        if returned_mbid and warn:
            logging.warning(
                "Skipping ambiguous Last.fm %s match for %s / %s: returned MBID %s",
                entity, artist, name, returned_mbid,
            )
        elif not returned_mbid and warn:
            logging.warning("Skipping unverified Last.fm %s match for %s / %s: no MBID returned", entity, artist, name)
        if name_fallback:
            if warn:
                logging.warning("Using requested name fallback for Last.fm %s: %s / %s", entity, artist, name)
            return self.top_tags(entity, artist, name), f"lastfm:{entity}-name"
        return [], f"lastfm:{entity}-unmatched"


class MusicBrainzClient:
    def __init__(self, cache: JsonCache, username: str = "", password: str = "") -> None:
        contact = os.getenv("MB_CONTACT", "https://github.com/SickProdigy/musicbrainz-helper")
        self.cache = cache
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": f"{CLIENT} ({contact})", "Accept": "application/json"})
        if username and password:
            self.session.auth = HTTPDigestAuth(username, password)
        self.next_request = 0.0

    def request(self, endpoint: str, **params) -> dict:
        for attempt, delay in enumerate(RETRY_DELAYS, start=1):
            wait = self.next_request - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self.next_request = time.monotonic() + 1.1
            try:
                response = self.session.get(f"{MB_BASE}/{endpoint}", params={"fmt": "json", **params}, timeout=45)
                if response.status_code in {429, 500, 502, 503, 504} and attempt < len(RETRY_DELAYS):
                    time.sleep(int(response.headers.get("Retry-After", delay)))
                    continue
                response.raise_for_status()
                return response.json()
            except (requests.ConnectionError, requests.Timeout):
                if attempt == len(RETRY_DELAYS):
                    raise RuntimeError(f"MusicBrainz request failed after {attempt} attempts: {endpoint}") from None
                time.sleep(delay)
        raise RuntimeError(f"MusicBrainz request failed: {endpoint}")

    def genres(self) -> dict[str, str]:
        cached = self.cache.get("musicbrainz", "genres-v2")
        if cached is None:
            cached = []
            offset = 0
            while True:
                payload = self.request("genre/all", limit=100, offset=offset)
                batch = [row["name"] for row in payload.get("genres", [])]
                cached.extend(batch)
                offset += len(batch)
                if not batch or offset >= int(payload.get("genre-count", offset)):
                    break
            self.cache.put("musicbrainz", "genres-v2", cached)
        return {normalized(name): name for name in cached}

    def release_group(self, mbid: str) -> dict:
        cached = self.cache.get("release-groups", mbid)
        if cached is not None:
            return cached
        releases: list[dict] = []
        offset = 0
        while True:
            payload = self.request(
                "release", **{"release-group": mbid, "inc": "recordings+artist-credits", "limit": 100, "offset": offset}
            )
            batch = payload.get("releases", [])
            releases.extend(batch)
            offset += len(batch)
            if not batch or offset >= int(payload.get("release-count", offset)):
                break
        result = {"releases": releases}
        self.cache.put("release-groups", mbid, result)
        return result

    def submit(self, body: bytes) -> None:
        for attempt, delay in enumerate(RETRY_DELAYS, start=1):
            wait = self.next_request - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self.next_request = time.monotonic() + 1.1
            try:
                response = self.session.post(
                    f"{MB_BASE}/tag", params={"client": CLIENT}, data=body,
                    headers={"Content-Type": "application/xml; charset=utf-8"}, timeout=60,
                )
                if response.status_code in {429, 500, 502, 503, 504} and attempt < len(RETRY_DELAYS):
                    time.sleep(int(response.headers.get("Retry-After", delay)))
                    continue
                response.raise_for_status()
                return
            except (requests.ConnectionError, requests.Timeout):
                if attempt == len(RETRY_DELAYS):
                    raise RuntimeError(f"MusicBrainz submission failed after {attempt} attempts.") from None
                time.sleep(delay)
        raise RuntimeError("MusicBrainz submission failed.")


def filtered_genres(tags: list[dict], vocabulary: dict[str, str], maximum: int) -> list[str]:
    ranked = sorted(tags, key=lambda row: int(row.get("count", 0) or 0), reverse=True)
    result: list[str] = []
    for row in ranked:
        genre = vocabulary.get(normalized(str(row.get("name", ""))))
        if genre and genre not in result:
            result.append(genre)
        if len(result) >= maximum:
            break
    return result


def album_identity(album: dict) -> tuple[str, str, str]:
    artist = album.get("artist") or {}
    return (
        str(artist.get("artistName") or album.get("artistName") or "").strip(),
        str(artist.get("foreignArtistId") or album.get("foreignArtistId") or "").strip(),
        str(album.get("foreignAlbumId") or "").strip(),
    )


def album_key(album: dict) -> str:
    return str(album.get("id") or album.get("foreignAlbumId") or "").strip()


def artist_keys(album: dict) -> set[str]:
    artist = album.get("artist") or {}
    return {
        str(value).strip()
        for value in (
            artist.get("id"), artist.get("foreignArtistId"),
            album.get("artistId"), album.get("foreignArtistId"),
        )
        if value is not None and str(value).strip()
    }


def album_keys(album: dict) -> set[str]:
    return {
        str(value).strip()
        for value in (album.get("id"), album.get("foreignAlbumId"))
        if value is not None and str(value).strip()
    }


def selected_albums(
    albums: Iterable[dict],
    limit: int | None = None,
    artist_id: str | None = None,
    start_artist_id: str | None = None,
    start_album_id: str | None = None,
    completed: set[str] | None = None,
):
    started_artist = not start_artist_id
    started_album = not start_album_id
    yielded = 0
    found_artist = started_artist
    found_album = started_album
    completed = completed or set()
    for album in albums:
        if artist_id and artist_id not in artist_keys(album):
            continue
        if not started_artist:
            if start_artist_id not in artist_keys(album):
                continue
            started_artist = True
            found_artist = True
        if not started_album:
            if start_album_id not in album_keys(album):
                continue
            started_album = True
            found_album = True
        if album_key(album) in completed:
            continue
        yield album
        yielded += 1
        if limit is not None and yielded >= limit:
            break
    if start_artist_id and not found_artist:
        logging.warning("Start artist ID was not found: %s", start_artist_id)
    if start_album_id and not found_album:
        logging.warning("Start album ID was not found: %s", start_album_id)


class RunState:
    def __init__(self, path: Path, resume: bool) -> None:
        self.path = path
        if resume and path.is_file():
            self.data = json.loads(path.read_text(encoding="utf-8"))
        else:
            self.data = {"completed_album_ids": [], "seen_artist_mbids": [], "processed_albums": 0}
        self.completed = set(self.data.get("completed_album_ids", []))
        self.seen_artists = set(self.data.get("seen_artist_mbids", []))

    def checkpoint(self, album_ids: list[str]) -> None:
        self.completed.update(album_ids)
        self.data.update(
            {
                "completed_album_ids": sorted(self.completed),
                "seen_artist_mbids": sorted(self.seen_artists),
                "processed_albums": len(self.completed),
                "updated_at": int(time.time()),
            }
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        temporary.replace(self.path)


class SubmissionLedger:
    def __init__(self, path: Path) -> None:
        self.path = path
        payload = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {"votes": []}
        self.votes = set(payload.get("votes", []))

    @staticmethod
    def key(row: dict) -> str:
        return "|".join((row["entity"], row["mbid"], row["genres"][0]))

    def contains(self, row: dict) -> bool:
        return self.key(row) in self.votes

    def record(self, rows: list[dict]) -> None:
        self.votes.update(self.key(row) for row in rows)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"votes": sorted(self.votes)}, indent=2), encoding="utf-8")
        temporary.replace(self.path)


def add_target(
    targets: dict[tuple[str, str], dict],
    entity: str,
    mbid: str,
    name: str,
    genres: list[str],
    source: str,
) -> list[dict]:
    if not mbid or not genres:
        return []
    row = targets.setdefault((entity, mbid), {"entity": entity, "mbid": mbid, "name": name, "genres": set(), "sources": set()})
    row["genres"].update(genres)
    row["sources"].add(source)
    return [
        {"entity": entity, "mbid": mbid, "name": name, "genres": [genre], "sources": [source]}
        for genre in sorted(set(genres))
    ]


def finalized_rows(targets: dict[tuple[str, str], dict]) -> list[dict]:
    rows = [
        {**row, "genres": sorted(row["genres"]), "sources": sorted(row["sources"])}
        for row in targets.values()
    ]
    return sorted(rows, key=lambda row: (row["entity"], row["name"].casefold(), row["mbid"]))


def load_report(path: Path) -> dict[tuple[str, str], dict]:
    targets: dict[tuple[str, str], dict] = {}
    if not path.is_file():
        return targets
    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            key = (row["entity"], row["mbid"])
            targets[key] = {
                "entity": row["entity"], "mbid": row["mbid"], "name": row["name"],
                "genres": {value.strip() for value in row["genres"].split(";") if value.strip()},
                "sources": {value.strip() for value in row["sources"].split(";") if value.strip()},
            }
    return targets


def submission_xml(rows: list[dict]) -> bytes:
    root = ET.Element("metadata", {"xmlns": MB_NS})
    grouped: dict[str, dict[str, dict]] = defaultdict(dict)
    for row in rows:
        item = grouped[row["entity"]].setdefault(
            row["mbid"], {"mbid": row["mbid"], "genres": set()}
        )
        item["genres"].update(row["genres"])
    for entity, list_name in ENTITY_LISTS.items():
        if not grouped[entity]:
            continue
        entity_list = ET.SubElement(root, list_name)
        for row in grouped[entity].values():
            item = ET.SubElement(entity_list, entity, {"id": row["mbid"]})
            tag_list = ET.SubElement(item, "user-tag-list")
            for genre in sorted(row["genres"]):
                user_tag = ET.SubElement(tag_list, "user-tag", {"vote": "upvote"})
                ET.SubElement(user_tag, "name").text = genre
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def submit_resilient(
    musicbrainz: MusicBrainzClient,
    rows: list[dict],
    ledger: SubmissionLedger,
    failures: list[dict],
) -> int:
    pending: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        key = ledger.key(row)
        if key not in ledger.votes and key not in seen:
            pending.append(row)
            seen.add(key)
    if not pending:
        return 0
    try:
        musicbrainz.submit(submission_xml(pending))
    except (requests.RequestException, RuntimeError) as exc:
        if len(pending) > 1:
            midpoint = len(pending) // 2
            return submit_resilient(musicbrainz, pending[:midpoint], ledger, failures) + submit_resilient(
                musicbrainz, pending[midpoint:], ledger, failures
            )
        row = pending[0]
        failure = {
            "entity": row["entity"], "mbid": row["mbid"], "name": row["name"],
            "genre": row["genres"][0], "error": str(exc),
        }
        failures.append(failure)
        logging.error("Submission failed for %s %s genre %s: %s", row["entity"], row["mbid"], row["genres"][0], exc)
        return 0
    ledger.record(pending)
    return len(pending)


def write_reports(output_dir: Path, rows: list[dict], summary: dict) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    with (output_dir / "genres.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=("entity", "mbid", "name", "genres", "sources"))
        writer.writeheader()
        writer.writerows(
            {
                **row,
                "genres": "; ".join(row["genres"]),
                "sources": "; ".join(row["sources"]),
            }
            for row in rows
        )
    (output_dir / "musicbrainz-tags.xml").write_bytes(submission_xml(rows))
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")


def write_failures(output_dir: Path, failures: list[dict]) -> None:
    with (output_dir / "failed-submissions.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=("entity", "mbid", "name", "genre", "error"))
        writer.writeheader()
        writer.writerows(failures)


def write_album_failures(output_dir: Path, failures: list[dict]) -> None:
    with (output_dir / "failed-albums.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=("album_id", "release_group_mbid", "artist", "title", "error"))
        writer.writeheader()
        writer.writerows(failures)


def main() -> int:
    args = parse_args()
    load_dotenv()
    log_path = configure_logging(args.log_level)
    if args.max_tags < 1:
        raise SystemExit("--max-tags must be at least 1.")
    if args.max_albums is not None and args.max_albums < 1:
        raise SystemExit("--max-albums must be at least 1.")
    if args.checkpoint_every < 1:
        raise SystemExit("--checkpoint-every must be at least 1.")
    if args.submit_batch_size < 1 or args.submit_batch_size > 100:
        raise SystemExit("--submit-batch-size must be between 1 and 100.")
    if args.artist_id and args.start_artist_id:
        raise SystemExit("--artist-id and --start-artist-id cannot be combined.")

    entities = set(args.entity or ENTITY_LISTS)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    cache = JsonCache(args.output_dir / "cache.json")
    lidarr = LidarrClient(required_env("LIDARR_URL"), required_env("LIDARR_API_KEY"))
    lastfm = LastFmClient(required_env("LASTFM_API_KEY"), cache)
    username, password = os.getenv("MB_USERNAME", ""), os.getenv("MB_PASSWORD", "")
    if args.submit and not (username and password):
        raise SystemExit("--submit requires MB_USERNAME and MB_PASSWORD.")
    musicbrainz = MusicBrainzClient(cache, username, password)
    vocabulary = musicbrainz.genres()

    progress_name = "progress-submit.json" if args.submit else "progress-preview.json"
    state = RunState(args.output_dir / progress_name, args.resume)
    ledger = SubmissionLedger(args.output_dir / "submitted-votes.json")
    targets = load_report(args.output_dir / "genres.csv") if args.resume else {}
    pending_votes: list[dict] = []
    submission_failures: list[dict] = []
    album_failures: list[dict] = []
    checkpoint_ids: list[str] = []
    processed_this_run = 0
    submitted_this_run = 0

    def summary() -> dict:
        rows = finalized_rows(targets)
        return {
            "scope": args.scope,
            "albums_processed_this_run": processed_this_run,
            "albums_completed": len(state.completed) + len(set(checkpoint_ids) - state.completed),
            "max_tags": args.max_tags,
            "entities": sorted(entities),
            "targets": {entity: sum(row["entity"] == entity for row in rows) for entity in ENTITY_LISTS},
            "submitted": bool(args.submit),
            "submitted_votes_this_run": submitted_this_run,
            "submitted_votes_recorded": len(ledger.votes),
            "failed_submission_votes": len(submission_failures),
            "failed_albums": len(album_failures),
            "log_file": str(log_path),
        }

    def checkpoint() -> None:
        nonlocal submitted_this_run
        failures_before = len(submission_failures)
        if args.submit:
            for offset in range(0, len(pending_votes), args.submit_batch_size):
                submitted_this_run += submit_resilient(
                    musicbrainz,
                    pending_votes[offset:offset + args.submit_batch_size],
                    ledger,
                    submission_failures,
                )
        pending_votes.clear()
        if len(submission_failures) == failures_before:
            state.checkpoint(checkpoint_ids)
        else:
            logging.warning(
                "Leaving %d albums uncheckpointed so failed votes can be retried with --resume.",
                len(checkpoint_ids),
            )
        checkpoint_ids.clear()
        rows = finalized_rows(targets)
        write_reports(args.output_dir, rows, summary())
        write_failures(args.output_dir, submission_failures)
        write_album_failures(args.output_dir, album_failures)
        logging.info(
            "Checkpoint: %d albums complete, %d targets, %d submitted votes, %d failures",
            len(state.completed), len(rows), submitted_this_run,
            len(submission_failures) + len(album_failures),
        )

    albums = selected_albums(
        lidarr.iter_albums(args.scope),
        limit=args.max_albums,
        artist_id=args.artist_id,
        start_artist_id=args.start_artist_id,
        start_album_id=args.start_album_id,
        completed=state.completed if args.resume else set(),
    )
    for index, album in enumerate(albums, start=1):
        processed_this_run += 1
        artist_name, artist_mbid, release_group_mbid = album_identity(album)
        title = str(album.get("title", "")).strip()
        if not (artist_name and release_group_mbid):
            logging.warning("Skipping album without artist/release-group identity: %s", title)
            album_failures.append(
                {
                    "album_id": album_key(album), "release_group_mbid": release_group_mbid,
                    "artist": artist_name, "title": title, "error": "Missing artist or release-group identity",
                }
            )
            continue
        logging.info("Album %d: %s / %s [%s]", index, artist_name, title, album_key(album))
        try:
            if "artist" in entities and artist_mbid and artist_mbid not in state.seen_artists:
                genres = filtered_genres(
                    lastfm.top_tags(
                        "artist", artist_name, mbid=artist_mbid, name_fallback=args.name_fallback
                    ),
                    vocabulary,
                    args.max_tags,
                )
                pending_votes.extend(add_target(targets, "artist", artist_mbid, artist_name, genres, "lastfm:artist-mbid"))
                state.seen_artists.add(artist_mbid)

            expanded = musicbrainz.release_group(release_group_mbid)
            release_mbids = {str(release.get("id", "")) for release in expanded["releases"] if release.get("id")}
            album_tags, album_source = lastfm.verified_info_tags(
                "album", artist_name, title, release_mbids, name_fallback=args.name_fallback
            )
            album_genres = filtered_genres(album_tags, vocabulary, args.max_tags)
            if "release-group" in entities:
                pending_votes.extend(
                    add_target(targets, "release-group", release_group_mbid, title, album_genres, album_source)
                )

            recordings_by_title: dict[str, dict] = {}
            for release in expanded["releases"]:
                if "release" in entities:
                    pending_votes.extend(
                        add_target(
                            targets, "release", str(release.get("id", "")),
                            str(release.get("title", title)), album_genres, album_source,
                        )
                    )
                for medium in release.get("media", []):
                    for track in medium.get("tracks", []):
                        recording = track.get("recording") or {}
                        if recording.get("id"):
                            track_title = str(recording.get("title") or track.get("title") or "")
                            title_key = normalized(track_title)
                            group = recordings_by_title.setdefault(
                                title_key,
                                {"title": track_title, "mbids": set(), "lastfm_mbids": set()},
                            )
                            group["mbids"].add(str(recording["id"]))
                            group["lastfm_mbids"].add(str(recording["id"]))
                            if track.get("id"):
                                group["lastfm_mbids"].add(str(track["id"]))

            if "recording" in entities:
                skipped_track_matches = 0
                for recording_group in recordings_by_title.values():
                    track_title = recording_group["title"]
                    track_tags, track_source = lastfm.verified_info_tags(
                        "track", artist_name, track_title, recording_group["lastfm_mbids"],
                        name_fallback=args.name_fallback,
                        warn=False,
                    )
                    if track_source == "lastfm:track-unmatched":
                        skipped_track_matches += 1
                    genres = filtered_genres(track_tags, vocabulary, args.max_tags)
                    for recording_mbid in recording_group["mbids"]:
                        pending_votes.extend(
                            add_target(
                                targets, "recording", recording_mbid, track_title,
                                genres, track_source,
                            )
                        )
                if skipped_track_matches:
                    logging.info(
                        "Skipped %d unverified Last.fm track matches for %s / %s",
                        skipped_track_matches, artist_name, title,
                    )
        except Exception as exc:
            logging.exception("Album failed and will remain eligible for --resume: %s / %s", artist_name, title)
            album_failures.append(
                {
                    "album_id": album_key(album), "release_group_mbid": release_group_mbid,
                    "artist": artist_name, "title": title, "error": str(exc),
                }
            )
        else:
            checkpoint_ids.append(album_key(album))

        if processed_this_run % args.checkpoint_every == 0:
            checkpoint()

    if checkpoint_ids or pending_votes or processed_this_run == 0 or album_failures or submission_failures:
        checkpoint()
    final_summary = summary()
    write_reports(args.output_dir, finalized_rows(targets), final_summary)
    print(json.dumps(final_summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
