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

import requests
from requests.auth import HTTPDigestAuth


MB_BASE = "https://musicbrainz.org/ws/2"
LASTFM_BASE = "https://ws.audioscrobbler.com/2.0/"
MB_NS = "http://musicbrainz.org/ns/mmd-2.0#"
CLIENT = "lidarr-lastfm-genres-helper-0.1"
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
    parser.add_argument("--output-dir", type=Path, default=Path("lidarr-lastfm-genre-reports"))
    parser.add_argument("--submit", action="store_true", help="Submit genre upvotes to MusicBrainz.")
    parser.add_argument("--log-level", choices=("DEBUG", "INFO", "WARNING", "ERROR"), default="INFO")
    return parser.parse_args()


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
        response = self.session.get(f"{self.base_url}/api/v1/{endpoint}", params=params, timeout=60)
        response.raise_for_status()
        return response.json()

    def all_albums(self) -> list[dict]:
        payload = self.get("album", includeAllArtistAlbums="true")
        return payload if isinstance(payload, list) else payload.get("records", [])

    def missing_albums(self) -> list[dict]:
        records: list[dict] = []
        page = 1
        while True:
            payload = self.get(
                "wanted/missing", page=page, pageSize=250, sortKey="title",
                sortDirection="ascending", includeArtist="true",
            )
            batch = payload.get("records", [])
            records.extend(batch)
            if not batch or len(records) >= int(payload.get("totalRecords", len(records))):
                return records
            page += 1

    def albums(self, scope: str) -> list[dict]:
        if scope == "all":
            return self.all_albums()
        missing = self.missing_albums()
        if scope == "missing":
            return missing
        missing_ids = {
            str(album.get("id") or album.get("foreignAlbumId") or "") for album in missing
        }
        return [
            album for album in self.all_albums()
            if str(album.get("id") or album.get("foreignAlbumId") or "") not in missing_ids
        ]


class LastFmClient:
    def __init__(self, api_key: str, cache: JsonCache) -> None:
        self.api_key = api_key
        self.cache = cache
        self.next_request = 0.0

    def top_tags(self, entity: str, artist: str, name: str = "") -> list[dict]:
        key = "|".join((normalized(artist), normalized(name)))
        cached = self.cache.get(f"lastfm-{entity}", key)
        if cached is not None:
            return cached
        delay = self.next_request - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        self.next_request = time.monotonic() + 0.25
        params = {"method": f"{entity}.getTopTags", "artist": artist, "api_key": self.api_key, "format": "json"}
        if entity == "album":
            params["album"] = name
        elif entity == "track":
            params["track"] = name
        response = requests.get(LASTFM_BASE, params=params, timeout=30)
        response.raise_for_status()
        payload = response.json()
        if payload.get("error"):
            logging.warning("Last.fm %s lookup failed for %s / %s: %s", entity, artist, name, payload.get("message"))
            tags = []
        else:
            tags = payload.get("toptags", {}).get("tag", [])
        self.cache.put(f"lastfm-{entity}", key, tags)
        return tags


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
        for delay in (1, 3, 10, 0):
            wait = self.next_request - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self.next_request = time.monotonic() + 1.1
            response = self.session.get(f"{MB_BASE}/{endpoint}", params={"fmt": "json", **params}, timeout=45)
            if response.status_code in {429, 500, 502, 503, 504} and delay:
                time.sleep(int(response.headers.get("Retry-After", delay)))
                continue
            response.raise_for_status()
            return response.json()
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
        wait = self.next_request - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self.next_request = time.monotonic() + 1.1
        response = self.session.post(
            f"{MB_BASE}/tag", params={"client": CLIENT}, data=body,
            headers={"Content-Type": "application/xml; charset=utf-8"}, timeout=60,
        )
        response.raise_for_status()


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


def add_target(targets: dict[tuple[str, str], dict], entity: str, mbid: str, name: str, genres: list[str], source: str) -> None:
    if not mbid or not genres:
        return
    row = targets.setdefault((entity, mbid), {"entity": entity, "mbid": mbid, "name": name, "genres": set(), "sources": set()})
    row["genres"].update(genres)
    row["sources"].add(source)


def submission_xml(rows: list[dict]) -> bytes:
    root = ET.Element("metadata", {"xmlns": MB_NS})
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["entity"]].append(row)
    for entity, list_name in ENTITY_LISTS.items():
        entity_list = ET.SubElement(root, list_name)
        for row in grouped[entity]:
            item = ET.SubElement(entity_list, entity, {"id": row["mbid"]})
            tag_list = ET.SubElement(item, "user-tag-list")
            for genre in row["genres"]:
                user_tag = ET.SubElement(tag_list, "user-tag", {"vote": "upvote"})
                ET.SubElement(user_tag, "name").text = genre
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


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


def main() -> int:
    args = parse_args()
    load_dotenv()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(levelname)s: %(message)s")
    if args.max_tags < 1:
        raise SystemExit("--max-tags must be at least 1.")
    entities = set(args.entity or ENTITY_LISTS)
    cache = JsonCache(args.output_dir / "cache.json")
    lidarr = LidarrClient(required_env("LIDARR_URL"), required_env("LIDARR_API_KEY"))
    lastfm = LastFmClient(required_env("LASTFM_API_KEY"), cache)
    username, password = os.getenv("MB_USERNAME", ""), os.getenv("MB_PASSWORD", "")
    if args.submit and not (username and password):
        raise SystemExit("--submit requires MB_USERNAME and MB_PASSWORD.")
    musicbrainz = MusicBrainzClient(cache, username, password)
    vocabulary = musicbrainz.genres()
    albums = lidarr.albums(args.scope)
    if args.max_albums is not None:
        albums = albums[: args.max_albums]
    targets: dict[tuple[str, str], dict] = {}
    seen_artists: set[str] = set()
    for index, album in enumerate(albums, start=1):
        artist_name, artist_mbid, release_group_mbid = album_identity(album)
        title = str(album.get("title", "")).strip()
        if not (artist_name and release_group_mbid):
            logging.warning("Skipping album without artist/release-group identity: %s", title)
            continue
        logging.info("Album %d/%d: %s / %s", index, len(albums), artist_name, title)
        if "artist" in entities and artist_mbid and artist_mbid not in seen_artists:
            genres = filtered_genres(lastfm.top_tags("artist", artist_name), vocabulary, args.max_tags)
            add_target(targets, "artist", artist_mbid, artist_name, genres, "lastfm:artist")
            seen_artists.add(artist_mbid)
        album_genres = filtered_genres(lastfm.top_tags("album", artist_name, title), vocabulary, args.max_tags)
        if "release-group" in entities:
            add_target(targets, "release-group", release_group_mbid, title, album_genres, "lastfm:album")
        expanded = musicbrainz.release_group(release_group_mbid)
        recordings: dict[str, str] = {}
        for release in expanded["releases"]:
            if "release" in entities:
                add_target(targets, "release", str(release.get("id", "")), str(release.get("title", title)), album_genres, "lastfm:album")
            for medium in release.get("media", []):
                for track in medium.get("tracks", []):
                    recording = track.get("recording") or {}
                    if recording.get("id"):
                        recordings[str(recording["id"])] = str(recording.get("title") or track.get("title") or "")
        if "recording" in entities:
            title_genres: dict[str, list[str]] = {}
            for recording_mbid, track_title in recordings.items():
                key = normalized(track_title)
                if key not in title_genres:
                    title_genres[key] = filtered_genres(
                        lastfm.top_tags("track", artist_name, track_title), vocabulary, args.max_tags
                    )
                add_target(targets, "recording", recording_mbid, track_title, title_genres[key], "lastfm:track")
    rows = [
        {**row, "genres": sorted(row["genres"]), "sources": sorted(row["sources"])}
        for row in targets.values()
    ]
    rows.sort(key=lambda row: (row["entity"], row["name"].casefold(), row["mbid"]))
    summary = {
        "scope": args.scope, "albums": len(albums), "max_tags": args.max_tags,
        "entities": sorted(entities), "targets": {entity: sum(r["entity"] == entity for r in rows) for entity in ENTITY_LISTS},
        "submitted": bool(args.submit),
    }
    write_reports(args.output_dir, rows, summary)
    if args.submit:
        for offset in range(0, len(rows), 100):
            musicbrainz.submit(submission_xml(rows[offset:offset + 100]))
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
