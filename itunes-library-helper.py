#!/usr/bin/env python3
"""Export ratings and catalog metadata from iTunes library/playlist exports."""

from __future__ import annotations

import argparse
import csv
import json
import os
import plistlib
import re
import time
import unicodedata
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Iterable

import requests
from requests.auth import HTTPDigestAuth


MB_BASE = "https://musicbrainz.org/ws/2"
MB_NS = "http://musicbrainz.org/ns/mmd-2.0#"
CLIENT = "itunes-library-helper-0.1"


@dataclass(frozen=True)
class Track:
    source_id: str
    persistent_id: str
    name: str
    artist: str
    album_artist: str
    album: str
    genre: str
    year: str
    disc_number: str
    track_number: str
    duration_seconds: str
    rating_100: str
    rating_stars: str
    loved: str
    disliked: str
    album_loved: str
    album_rating_100: str
    album_rating_computed: str
    play_count: str
    skip_count: str
    date_added: str
    last_played: str
    location: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create reviewable CSV/JSON reports from an iTunes Library.xml or playlist text export."
    )
    parser.add_argument("source", type=Path, help="iTunes Library.xml or tab-separated playlist export.")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("itunes-library-reports"),
        help="Report directory (default: itunes-library-reports).",
    )
    parser.add_argument("--musicbrainz", action="store_true", help="Match entities and create a contribution preview.")
    parser.add_argument(
        "--match-scope", choices=("rated", "preferences", "all"), default="rated",
        help="Tracks to match (default: rated). Use all for full genre export.",
    )
    parser.add_argument("--max-matches", type=int, help="Limit uncached MusicBrainz matches for a short run.")
    parser.add_argument("--submit", action="store_true", help="Submit accepted ratings and genre upvotes.")
    parser.add_argument("--mb-username", default=None, help="Defaults to MB_USERNAME from .env.")
    parser.add_argument("--mb-password", default=None, help="Defaults to MB_PASSWORD from .env.")
    return parser.parse_args()


def text(value: object | None) -> str:
    if value is None:
        return ""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def rating_stars(value: object | None) -> str:
    if value in (None, ""):
        return ""
    try:
        rating = int(value)
    except (TypeError, ValueError):
        return ""
    return f"{rating / 20:g}" if 0 <= rating <= 100 else ""


def load_dotenv(path: Path = Path(".env")) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def normalize(value: str) -> str:
    value = re.sub(r"\s*[\[(](?:feat(?:uring)?\.?|ft\.?)\s+.+?[\])]$", "", value, flags=re.I)
    value = unicodedata.normalize("NFKD", value).casefold()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", value).split())


def base_track_title(value: str) -> str:
    return re.sub(
        r"\s*[\[(](?:feat(?:uring)?\.?|ft\.?)\s+.+?[\])]$", "", value.strip(), flags=re.I
    ).strip()


def lucene(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def genre_tag(value: str) -> str:
    aliases = {
        "hip hop rap": "hip hop", "hip hop": "hip hop", "r b soul": "rhythm and blues",
        "r b": "rhythm and blues", "rap": "hip hop",
    }
    return aliases.get(normalize(value), value.strip().casefold())


class MusicBrainzClient:
    def __init__(self, username: str | None = None, password: str | None = None) -> None:
        contact = os.getenv("MB_CONTACT", "https://github.com/SickProdigy/musicbrainz-helper")
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": f"{CLIENT} ({contact})", "Accept": "application/json"})
        if username and password:
            self.session.auth = HTTPDigestAuth(username, password)
        self.next_request = 0.0

    def throttle(self) -> None:
        delay = self.next_request - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        self.next_request = time.monotonic() + 1.1

    def search_recording(self, track: Track) -> list[dict]:
        query = f"recording:{lucene(base_track_title(track.name))} AND artist:{lucene(track.artist)}"
        if track.album:
            query += f" AND release:{lucene(track.album)}"
        for delay in (1, 2, 5, 10):
            self.throttle()
            response = self.session.get(
                f"{MB_BASE}/recording", params={"query": query, "fmt": "json", "limit": 10}, timeout=30
            )
            if response.status_code == 429 or response.status_code >= 500:
                time.sleep(int(response.headers.get("Retry-After", delay)))
                continue
            response.raise_for_status()
            return response.json().get("recordings", [])
        response.raise_for_status()
        return []

    def post(self, endpoint: str, body: bytes) -> None:
        self.throttle()
        response = self.session.post(
            f"{MB_BASE}/{endpoint}", params={"client": CLIENT}, data=body,
            headers={"Content-Type": "application/xml; charset=utf-8"}, timeout=30,
        )
        response.raise_for_status()


def credit_name(candidate: dict) -> str:
    return "".join(
        part.get("name", "") + part.get("joinphrase", "")
        for part in candidate.get("artist-credit", []) if isinstance(part, dict)
    )


def match_candidate(track: Track, candidates: list[dict]) -> tuple[str, dict | None, str]:
    valid = []
    wanted_duration = float(track.duration_seconds or 0) * 1000
    for candidate in candidates:
        releases = candidate.get("releases", [])
        if normalize(candidate.get("title", "")) != normalize(track.name):
            continue
        if normalize(track.artist) not in normalize(credit_name(candidate)):
            continue
        matching_releases = [r for r in releases if normalize(r.get("title", "")) == normalize(track.album)]
        if track.album and not matching_releases:
            continue
        duration_delta = abs(int(candidate.get("length") or 0) - wanted_duration) if wanted_duration else 0
        if wanted_duration and duration_delta > 5000:
            continue
        release = matching_releases[0] if matching_releases else (releases[0] if releases else {})
        valid.append((duration_delta, candidate, release))
    if not valid:
        return "unmatched", None, "no exact title/artist/album/duration match"
    valid.sort(key=lambda item: item[0])
    if len(valid) > 1 and valid[0][0] == valid[1][0]:
        return "ambiguous", None, f"{len(valid)} equally close recordings"
    delta, candidate, release = valid[0]
    artist = next((p.get("artist", {}) for p in candidate.get("artist-credit", []) if isinstance(p, dict)), {})
    rg = release.get("release-group", {})
    return "accepted", {
        "recording_mbid": candidate.get("id", ""), "release_group_mbid": rg.get("id", ""),
        "artist_mbid": artist.get("id", ""), "matched_title": candidate.get("title", ""),
        "matched_artist": credit_name(candidate), "duration_delta_ms": int(delta),
    }, "unique exact metadata match"


def match_musicbrainz(tracks: list[Track], output_dir: Path, scope: str, maximum: int | None, client: MusicBrainzClient) -> list[dict]:
    cache_path = output_dir / "musicbrainz-match-cache.json"
    cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.is_file() else {}
    selected = [t for t in tracks if t.rating_100] if scope == "rated" else [
        t for t in tracks if scope == "all" or t.loved or t.disliked or t.album_loved
    ]
    rows, new_count = [], 0
    for track in selected:
        key = "v2|" + "|".join((normalize(track.name), normalize(track.artist), normalize(track.album), track.duration_seconds))
        cached = cache.get(key)
        if cached is None:
            if maximum is not None and new_count >= maximum:
                continue
            status, match, reason = match_candidate(track, client.search_recording(track))
            cached = {"status": status, "match": match, "reason": reason}
            cache[key] = cached
            cache_path.write_text(json.dumps(cache, indent=2), encoding="utf-8")
            new_count += 1
        row = {**asdict(track), **(cached.get("match") or {}), "match_status": cached["status"], "match_reason": cached["reason"]}
        rows.append(row)
    fields = list(Track.__dataclass_fields__) + [
        "recording_mbid", "release_group_mbid", "artist_mbid", "matched_title", "matched_artist",
        "duration_delta_ms", "match_status", "match_reason",
    ]
    for status in ("accepted", "ambiguous", "unmatched"):
        write_csv(output_dir / f"musicbrainz-{status}.csv", [r for r in rows if r["match_status"] == status], fields)
    return rows


def submission_xml(rows: list[dict], kind: str) -> bytes:
    root = ET.Element("metadata", {"xmlns": MB_NS})
    if kind == "rating":
        entity_list = ET.SubElement(root, "recording-list")
        for row in rows:
            if not row["rating_100"]:
                continue
            entity = ET.SubElement(entity_list, "recording", {"id": row["recording_mbid"]})
            ET.SubElement(entity, "user-rating").text = row["rating_100"]
    else:
        grouped: dict[tuple[str, str], set[str]] = defaultdict(set)
        for row in rows:
            if not row["genre"]:
                continue
            tag = genre_tag(row["genre"])
            for entity_type, field in (("recording", "recording_mbid"), ("release-group", "release_group_mbid"), ("artist", "artist_mbid")):
                if row.get(field):
                    grouped[(entity_type, row[field])].add(tag)
        for entity_type, list_name in (("artist", "artist-list"), ("release-group", "release-group-list"), ("recording", "recording-list")):
            entity_list = ET.SubElement(root, list_name)
            for (group_type, mbid), tags in grouped.items():
                if group_type != entity_type:
                    continue
                entity = ET.SubElement(entity_list, entity_type, {"id": mbid})
                tag_list = ET.SubElement(entity, "user-tag-list")
                for tag in sorted(tags):
                    user_tag = ET.SubElement(tag_list, "user-tag", {"vote": "upvote"})
                    ET.SubElement(user_tag, "name").text = tag
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def track_from_mapping(row: dict, source_id: object = "") -> Track:
    rating = row.get("Rating", row.get("My Rating", ""))
    duration_ms = row.get("Total Time")
    duration_seconds = row.get("Time", "")
    if duration_ms not in (None, ""):
        duration_seconds = round(int(duration_ms) / 1000, 3)
    return Track(
        source_id=text(source_id or row.get("Track ID")),
        persistent_id=text(row.get("Persistent ID")),
        name=text(row.get("Name")),
        artist=text(row.get("Artist")),
        album_artist=text(row.get("Album Artist")),
        album=text(row.get("Album")),
        genre=text(row.get("Genre")),
        year=text(row.get("Year")),
        disc_number=text(row.get("Disc Number")),
        track_number=text(row.get("Track Number")),
        duration_seconds=text(duration_seconds),
        rating_100=text(rating),
        rating_stars=rating_stars(rating),
        loved="true" if row.get("Loved") else "",
        disliked="true" if row.get("Disliked") else "",
        album_loved="true" if row.get("Album Loved") else "",
        album_rating_100=text(row.get("Album Rating")),
        album_rating_computed="true" if row.get("Album Rating Computed") else "",
        play_count=text(row.get("Play Count", row.get("Plays"))),
        skip_count=text(row.get("Skip Count", row.get("Skips"))),
        date_added=text(row.get("Date Added")),
        last_played=text(row.get("Play Date UTC", row.get("Last Played"))),
        location=text(row.get("Location")),
    )


def read_xml(path: Path) -> tuple[list[Track], list[dict], dict]:
    with path.open("rb") as handle:
        library = plistlib.load(handle)
    raw_tracks = library.get("Tracks", {})
    tracks = [track_from_mapping(row, track_id) for track_id, row in raw_tracks.items()]
    playlists = []
    for playlist in library.get("Playlists", []):
        playlist_name = text(playlist.get("Name"))
        playlist_id = text(playlist.get("Playlist Persistent ID", playlist.get("Playlist ID")))
        for position, item in enumerate(playlist.get("Playlist Items", []), start=1):
            playlists.append(
                {
                    "playlist": playlist_name,
                    "playlist_id": playlist_id,
                    "position": position,
                    "track_id": text(item.get("Track ID")),
                }
            )
    metadata = {
        "source_type": "itunes-library-xml",
        "library_date": text(library.get("Date")),
        "application_version": text(library.get("Application Version")),
        "library_persistent_id": text(library.get("Library Persistent ID")),
        "playlist_count": len(library.get("Playlists", [])),
    }
    return tracks, playlists, metadata


def detect_text_encoding(path: Path) -> str:
    prefix = path.read_bytes()[:4]
    if prefix.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "utf-16"
    if prefix.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    return "utf-8-sig"


def read_playlist_text(path: Path) -> tuple[list[Track], list[dict], dict]:
    with path.open("r", encoding=detect_text_encoding(path), newline="") as handle:
        rows = list(csv.DictReader(handle, delimiter="\t"))
    tracks = [track_from_mapping(row, index) for index, row in enumerate(rows, start=1)]
    playlist_name = path.stem
    playlists = [
        {"playlist": playlist_name, "playlist_id": "", "position": index, "track_id": track.source_id}
        for index, track in enumerate(tracks, start=1)
    ]
    return tracks, playlists, {"source_type": "itunes-playlist-text", "playlist_count": 1}


def read_source(path: Path) -> tuple[list[Track], list[dict], dict]:
    with path.open("rb") as handle:
        prefix = handle.read(100).lstrip()
    if prefix.startswith(b"<?xml") or prefix.startswith(b"<plist"):
        return read_xml(path)
    return read_playlist_text(path)


def write_csv(path: Path, rows: Iterable[dict], fieldnames: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def artist_rows(tracks: list[Track]) -> list[dict]:
    grouped: dict[str, list[Track]] = defaultdict(list)
    for track in tracks:
        artist = track.album_artist or track.artist
        if artist:
            grouped[artist].append(track)
    rows = []
    for artist, artist_tracks in grouped.items():
        genres = Counter(track.genre for track in artist_tracks if track.genre)
        ratings = [float(track.rating_stars) for track in artist_tracks if track.rating_stars]
        rows.append(
            {
                "artist": artist,
                "track_count": len(artist_tracks),
                "rated_track_count": len(ratings),
                "average_track_rating": f"{sum(ratings) / len(ratings):.2f}" if ratings else "",
                "genres": "; ".join(f"{genre} ({count})" for genre, count in genres.most_common()),
            }
        )
    return sorted(rows, key=lambda row: row["artist"].casefold())


def genre_rows(tracks: list[Track]) -> list[dict]:
    genre_tracks: Counter[str] = Counter()
    genre_artists: dict[str, set[str]] = defaultdict(set)
    for track in tracks:
        if not track.genre:
            continue
        genre_tracks[track.genre] += 1
        artist = track.album_artist or track.artist
        if artist:
            genre_artists[track.genre].add(artist)
    return [
        {"genre": genre, "track_count": count, "artist_count": len(genre_artists[genre])}
        for genre, count in genre_tracks.most_common()
    ]


def export_reports(source: Path, output_dir: Path) -> dict:
    tracks, playlist_items, metadata = read_source(source)
    output_dir.mkdir(parents=True, exist_ok=True)
    track_rows = [asdict(track) for track in tracks]
    rated_rows = [row for row in track_rows if row["rating_stars"]]
    preference_rows = [row for row in track_rows if row["loved"] or row["disliked"] or row["album_loved"]]
    artists = artist_rows(tracks)
    genres = genre_rows(tracks)
    write_csv(output_dir / "tracks.csv", track_rows, list(Track.__dataclass_fields__))
    write_csv(output_dir / "ratings.csv", rated_rows, list(Track.__dataclass_fields__))
    write_csv(output_dir / "preferences.csv", preference_rows, list(Track.__dataclass_fields__))
    write_csv(
        output_dir / "artists.csv",
        artists,
        ["artist", "track_count", "rated_track_count", "average_track_rating", "genres"],
    )
    write_csv(output_dir / "genres.csv", genres, ["genre", "track_count", "artist_count"])
    write_csv(
        output_dir / "playlist-tracks.csv",
        playlist_items,
        ["playlist", "playlist_id", "position", "track_id"],
    )
    summary = {
        **metadata,
        "source": str(source.resolve()),
        "track_count": len(tracks),
        "rated_track_count": len(rated_rows),
        "loved_track_count": sum(track.loved == "true" for track in tracks),
        "disliked_track_count": sum(track.disliked == "true" for track in tracks),
        "album_loved_track_field_count": sum(track.album_loved == "true" for track in tracks),
        "computed_album_rating_track_field_count": sum(
            track.album_rating_computed == "true" for track in tracks
        ),
        "artist_count": len(artists),
        "genre_count": len(genres),
        "playlist_item_count": len(playlist_items),
        "rating_counts": dict(sorted(Counter(row["rating_stars"] for row in rated_rows).items())),
    }
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def main() -> int:
    args = parse_args()
    load_dotenv()
    if not args.source.is_file():
        raise SystemExit(f"Source does not exist or is not a file: {args.source}")
    summary = export_reports(args.source, args.output_dir)
    if args.musicbrainz or args.submit:
        tracks, _, _ = read_source(args.source)
        username = args.mb_username or os.getenv("MB_USERNAME")
        password = args.mb_password or os.getenv("MB_PASSWORD")
        if args.submit and not (username and password):
            raise SystemExit("--submit requires MB_USERNAME and MB_PASSWORD in .env or CLI arguments.")
        client = MusicBrainzClient(username, password)
        matches = match_musicbrainz(tracks, args.output_dir, args.match_scope, args.max_matches, client)
        accepted = [row for row in matches if row["match_status"] == "accepted"]
        summary["musicbrainz"] = {
            "scope": args.match_scope,
            "accepted": len(accepted),
            "ambiguous": sum(row["match_status"] == "ambiguous" for row in matches),
            "unmatched": sum(row["match_status"] == "unmatched" for row in matches),
            "ratings": sum(bool(row["rating_100"]) for row in accepted),
            "submitted": bool(args.submit),
        }
        rating_xml = submission_xml(accepted, "rating")
        genre_xml = submission_xml(accepted, "tag")
        (args.output_dir / "musicbrainz-ratings.xml").write_bytes(rating_xml)
        (args.output_dir / "musicbrainz-genres.xml").write_bytes(genre_xml)
        if args.submit:
            if any(row["rating_100"] for row in accepted):
                client.post("rating", rating_xml)
            if any(row["genre"] for row in accepted):
                client.post("tag", genre_xml)
        (args.output_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
