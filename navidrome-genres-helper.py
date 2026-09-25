#!/usr/bin/env python3
"""Export embedded Navidrome genres as MusicBrainz genre upvotes."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import os
import re
import secrets
import time
import unicodedata
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path

import requests
from requests.auth import HTTPDigestAuth


MB_BASE = "https://musicbrainz.org/ws/2"
MB_NS = "http://musicbrainz.org/ns/mmd-2.0#"
CLIENT = "musicbrainz-helper/0.2"
ENTITY_LISTS = {
    "artist": "artist-list",
    "release-group": "release-group-list",
    "release": "release-list",
    "recording": "recording-list",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Upvote genres from a tagged Navidrome library on matching MusicBrainz entities."
    )
    parser.add_argument("--entity", action="append", choices=tuple(ENTITY_LISTS))
    parser.add_argument("--artist-id", help="Limit processing to one Navidrome artist ID.")
    parser.add_argument("--max-albums", type=int, help="Limit albums for a short or resumable run.")
    parser.add_argument("--max-tags", type=int, default=7, help="Maximum genres per MusicBrainz entity.")
    parser.add_argument("--output-dir", type=Path, default=Path("navidrome-genre-reports"))
    parser.add_argument("--submit", action="store_true", help="Submit the generated genre upvotes.")
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
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(f"{name} is required in .env or the environment.")
    return value


def normalized(value: str) -> str:
    folded = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "", folded.casefold())


def item_genres(item: dict) -> list[str]:
    """Read legacy Subsonic and multi-value OpenSubsonic genre fields."""
    values: list[str] = []
    legacy = item.get("genre")
    if isinstance(legacy, str):
        values.extend(part.strip() for part in legacy.split(";") if part.strip())
    for entry in item.get("genres") or []:
        value = entry.get("name", "") if isinstance(entry, dict) else str(entry)
        if value.strip():
            values.append(value.strip())
    return list(dict.fromkeys(values))


class JsonCache:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}

    def get(self, namespace: str, key: str):
        return self.data.get(namespace, {}).get(key)

    def put(self, namespace: str, key: str, value) -> None:
        self.data.setdefault(namespace, {})[key] = value
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2), encoding="utf-8")


class NavidromeClient:
    def __init__(self, base_url: str, username: str, password: str) -> None:
        self.base_url = base_url.rstrip("/")
        salt = secrets.token_hex(8)
        token = hashlib.md5(f"{password}{salt}".encode(), usedforsecurity=False).hexdigest()
        self.auth = {"u": username, "t": token, "s": salt, "v": "1.16.1", "c": CLIENT, "f": "json"}
        self.session = requests.Session()
        self.artist_cache: dict[str, dict] = {}

    def request(self, endpoint: str, **params) -> dict:
        for attempt, delay in enumerate((1, 3, 10), start=1):
            try:
                response = self.session.get(
                    f"{self.base_url}/rest/{endpoint}", params={**self.auth, **params}, timeout=45
                )
                response.raise_for_status()
                payload = response.json().get("subsonic-response", {})
                if payload.get("status") == "failed" or payload.get("error"):
                    raise RuntimeError(f"Navidrome {endpoint} failed: {payload.get('error', {})}")
                return payload
            except (requests.ConnectionError, requests.Timeout):
                if attempt == 3:
                    raise SystemExit(f"Navidrome connection failed for {endpoint} after {attempt} attempts.") from None
                time.sleep(delay)
            except requests.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else "unknown"
                raise SystemExit(f"Navidrome {endpoint} returned HTTP {status}.") from None
            except requests.RequestException:
                raise SystemExit(f"Navidrome request failed for {endpoint}.") from None
        raise RuntimeError(f"Navidrome request failed: {endpoint}")

    def get_album(self, album_id: str) -> dict:
        return self.request("getAlbum", id=album_id).get("album", {})

    def get_artist(self, artist_id: str) -> dict:
        if artist_id not in self.artist_cache:
            self.artist_cache[artist_id] = self.request("getArtist", id=artist_id).get("artist", {})
        return self.artist_cache[artist_id]

    def iter_albums(self, limit: int | None = None, artist_id: str | None = None):
        yielded = 0
        if artist_id:
            source = self.get_artist(artist_id).get("album", [])
            for album in source[:limit]:
                yield album
            return
        offset = 0
        while limit is None or yielded < limit:
            size = min(500, limit - yielded) if limit is not None else 500
            page = self.request(
                "getAlbumList2", type="alphabeticalByName", size=size, offset=offset
            ).get("albumList2", {}).get("album", [])
            if not page:
                break
            for album in page:
                yield album
                yielded += 1
            if len(page) < size:
                break
            offset += len(page)


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
            response = self.session.get(
                f"{MB_BASE}/{endpoint}", params={"fmt": "json", **params}, timeout=45
            )
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

    def release_group_mbid(self, release_mbid: str) -> str:
        cached = self.cache.get("release-groups-by-release", release_mbid)
        if cached is not None:
            return str(cached)
        payload = self.request(f"release/{release_mbid}", inc="release-groups")
        mbid = str((payload.get("release-group") or {}).get("id", ""))
        self.cache.put("release-groups-by-release", release_mbid, mbid)
        return mbid

    def submit(self, body: bytes) -> None:
        wait = self.next_request - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self.next_request = time.monotonic() + 1.1
        response = self.session.post(
            f"{MB_BASE}/tag",
            params={"client": CLIENT},
            data=body,
            headers={"Content-Type": "application/xml; charset=utf-8"},
            timeout=60,
        )
        response.raise_for_status()


def accepted_genres(values: list[str], vocabulary: dict[str, str], unmatched: Counter) -> list[str]:
    accepted: list[str] = []
    for value in values:
        canonical = vocabulary.get(normalized(value))
        if canonical:
            if canonical not in accepted:
                accepted.append(canonical)
        else:
            unmatched[value] += 1
    return accepted


def add_target(
    targets: dict[tuple[str, str], dict],
    entity: str,
    mbid: str,
    name: str,
    artist: str,
    genres: list[str],
    source: str,
) -> None:
    if not mbid or not genres:
        return
    row = targets.setdefault(
        (entity, mbid),
        {"entity": entity, "mbid": mbid, "name": name, "artist": artist, "counts": Counter(), "sources": set()},
    )
    row["counts"].update(genres)
    row["sources"].add(source)


def ranked_rows(targets: dict[tuple[str, str], dict], max_tags: int) -> list[dict]:
    rows = []
    for row in targets.values():
        genres = [name for name, _ in row["counts"].most_common(max_tags)]
        rows.append({**row, "genres": genres, "sources": sorted(row["sources"])})
        rows[-1].pop("counts")
    return sorted(rows, key=lambda row: (row["entity"], row["artist"].casefold(), row["name"].casefold(), row["mbid"]))


def submission_xml(rows: list[dict]) -> bytes:
    root = ET.Element("metadata", {"xmlns": MB_NS})
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["entity"]].append(row)
    for entity, list_name in ENTITY_LISTS.items():
        if not grouped[entity]:
            continue
        entity_list = ET.SubElement(root, list_name)
        for row in grouped[entity]:
            item = ET.SubElement(entity_list, entity, {"id": row["mbid"]})
            tag_list = ET.SubElement(item, "user-tag-list")
            for genre in row["genres"]:
                user_tag = ET.SubElement(tag_list, "user-tag", {"vote": "upvote"})
                ET.SubElement(user_tag, "name").text = genre
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def write_reports(output_dir: Path, rows: list[dict], unmatched: Counter, summary: dict) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    with (output_dir / "genres.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=("entity", "mbid", "name", "artist", "genres", "sources"))
        writer.writeheader()
        for row in rows:
            writer.writerow({**row, "genres": "; ".join(row["genres"]), "sources": "; ".join(row["sources"])})
    with (output_dir / "unmatched-genres.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(("genre", "occurrences"))
        writer.writerows(sorted(unmatched.items(), key=lambda item: (-item[1], item[0].casefold())))
    (output_dir / "musicbrainz-tags.xml").write_bytes(submission_xml(rows))
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")


def main() -> int:
    args = parse_args()
    load_dotenv()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(levelname)s: %(message)s")
    if args.max_tags < 1:
        raise SystemExit("--max-tags must be at least 1.")
    if args.max_albums is not None and args.max_albums < 1:
        raise SystemExit("--max-albums must be at least 1.")

    entities = set(args.entity or ENTITY_LISTS)
    cache = JsonCache(args.output_dir / "cache.json")
    navidrome = NavidromeClient(
        required_env("NAVIDROME_BASE_URL"),
        required_env("NAVIDROME_USERNAME"),
        required_env("NAVIDROME_PASSWORD"),
    )
    username, password = os.getenv("MB_USERNAME", ""), os.getenv("MB_PASSWORD", "")
    if args.submit and not (username and password):
        raise SystemExit("--submit requires MB_USERNAME and MB_PASSWORD.")
    musicbrainz = MusicBrainzClient(cache, username, password)
    vocabulary = musicbrainz.genres()

    targets: dict[tuple[str, str], dict] = {}
    unmatched: Counter = Counter()
    scanned = {"albums": 0, "songs": 0, "albums_without_mbid": 0, "songs_without_mbid": 0}
    for album_summary in navidrome.iter_albums(args.max_albums, args.artist_id):
        album = navidrome.get_album(str(album_summary.get("id", "")))
        scanned["albums"] += 1
        title = str(album.get("name") or album.get("title") or album_summary.get("name") or "")
        artist_name = str(album.get("artist") or album_summary.get("artist") or "")
        logging.info("Album %d: %s / %s", scanned["albums"], artist_name, title)

        artist_id = str(album.get("artistId") or album_summary.get("artistId") or "")
        artist = navidrome.get_artist(artist_id) if artist_id else {}
        artist_mbid = str(artist.get("musicBrainzId") or "")
        release_mbid = str(album.get("musicBrainzId") or album_summary.get("musicBrainzId") or "")
        if not release_mbid:
            scanned["albums_without_mbid"] += 1

        album_values = item_genres(album) or item_genres(album_summary)
        album_genres = accepted_genres(album_values, vocabulary, unmatched)
        song_genres_for_album: list[str] = []
        for song in album.get("song", []):
            scanned["songs"] += 1
            values = item_genres(song)
            genres = accepted_genres(values, vocabulary, unmatched)
            song_genres_for_album.extend(genres)
            recording_mbid = str(song.get("musicBrainzId") or "")
            if not recording_mbid:
                scanned["songs_without_mbid"] += 1
            if "recording" in entities:
                add_target(
                    targets, "recording", recording_mbid, str(song.get("title", "")),
                    str(song.get("artist") or artist_name), genres, "navidrome:song",
                )

        combined_album_genres = album_genres + song_genres_for_album
        if "artist" in entities:
            add_target(
                targets, "artist", artist_mbid, str(artist.get("name") or artist_name),
                artist_name, combined_album_genres, "navidrome:library",
            )
        if "release" in entities:
            add_target(
                targets, "release", release_mbid, title, artist_name,
                combined_album_genres, "navidrome:album",
            )
        if "release-group" in entities and release_mbid and combined_album_genres:
            release_group_mbid = musicbrainz.release_group_mbid(release_mbid)
            add_target(
                targets, "release-group", release_group_mbid, title, artist_name,
                combined_album_genres, "navidrome:album",
            )

    rows = ranked_rows(targets, args.max_tags)
    summary = {
        "albums": scanned["albums"],
        "songs": scanned["songs"],
        "albums_without_mbid": scanned["albums_without_mbid"],
        "songs_without_mbid": scanned["songs_without_mbid"],
        "max_tags": args.max_tags,
        "entities": sorted(entities),
        "targets": {entity: sum(row["entity"] == entity for row in rows) for entity in ENTITY_LISTS},
        "unmatched_genres": len(unmatched),
        "submitted": bool(args.submit),
    }
    write_reports(args.output_dir, rows, unmatched, summary)
    if args.submit:
        for offset in range(0, len(rows), 100):
            musicbrainz.submit(submission_xml(rows[offset:offset + 100]))
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
