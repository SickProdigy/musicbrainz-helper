#!/usr/bin/env python3
"""Observe existing Subsonic ratings and contribute changes to MusicBrainz.

This adapter reads existing Subsonic ratings and submits changes to MusicBrainz. It owns
Subsonic synchronization and MusicBrainz contribution history.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import secrets
import time
import xml.etree.ElementTree as ET

import mutagen
import requests


from . import musicbrainz_genres as genres
from . import musicbrainz as core
from .ratings import build_submission, normalized
from .rating_sources import LastFmRatingSource




def load_json(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def interval_seconds(value):
    match = re.fullmatch(r"([1-9][0-9]*)([smhd]?)", str(value))
    if not match:
        raise ValueError("ratingsScriptInterval must be a positive duration such as 15m")
    return int(match[1]) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[match[2]]


def stars(song):
    # userRating is personal; averageRating must never be used as the user's vote.
    value = song.get("userRating", 0)
    if value is None or value == "":
        return 0
    number = int(value)
    if not 0 <= number <= 5 or float(value) != number:
        raise ValueError("Invalid Subsonic user rating")
    return number


class Subsonic:
    def __init__(self, url, username, password):
        if not (url and username and password):
            raise ValueError("Ratings requires subsonicUrl, subsonicUsername and subsonicPassword")
        if not url.startswith(("http://", "https://")):
            raise ValueError("subsonicUrl must be an HTTP or HTTPS URL")
        self.url, self.username, self.password = url.rstrip("/"), username, password
        self.session = requests.Session()

    def call(self, endpoint, **params):
        for attempt in range(3):
            salt = secrets.token_hex(12)
            auth = {"u": self.username, "s": salt,
                    "t": hashlib.md5((self.password + salt).encode()).hexdigest(),
                    "v": "1.16.1", "c": "arr-scripts-ratings", "f": "json"}
            try:
                response = self.session.get(f"{self.url}/rest/{endpoint}",
                                            params={**params, **auth}, timeout=(10, 45))
                if response.status_code in {429, 500, 502, 503, 504}:
                    raise requests.ConnectionError()
                if not response.ok:
                    raise RuntimeError(f"Subsonic {endpoint} returned HTTP {response.status_code}")
                result = response.json()["subsonic-response"]
                if result.get("status") != "ok":
                    raise RuntimeError(f"Subsonic {endpoint} failed (code {result.get('error', {}).get('code', '?')})")
                return result
            except (requests.ConnectionError, requests.Timeout):
                if attempt == 2:
                    raise RuntimeError(f"Subsonic {endpoint} unavailable") from None
                time.sleep(2 ** attempt)
            except (ValueError, KeyError):
                raise RuntimeError(f"Invalid Subsonic {endpoint} response") from None

    def songs(self):
        # One complete, paginated scan also observes songs whose ratings were cleared.
        result = {}
        offset = 0
        while True:
            albums = self.call("getAlbumList2", type="alphabeticalByName", size=500,
                               offset=offset).get("albumList2", {}).get("album", [])
            for album in albums:
                for song in self.call("getAlbum", id=album["id"])["album"].get("song", []):
                    result[str(song["id"])] = song
            if len(albums) < 500:
                return result
            offset += len(albums)

    def get_song(self, song_id):
        return self.call("getSong", id=song_id)["song"]



def enqueue(root, album_id, fetch=None, read_tags=None):
    if fetch is None:
        session = requests.Session()
        session.headers["X-Api-Key"] = os.environ["LIDARR_API_KEY"]

        def fetch(endpoint):
            response = session.get(f"{os.environ['LIDARR_URL'].rstrip('/')}/api/v1/{endpoint}", timeout=(10, 30))
            if not response.ok:
                raise RuntimeError(f"Lidarr returned HTTP {response.status_code}")
            return response.json()
    album = fetch(f"album/{album_id}")
    files = fetch(f"trackfile?albumId={album_id}")
    read_tags = read_tags or (lambda path: mutagen.File(path, easy=True))
    tracks = []
    for item in files:
        audio = read_tags(item["path"])
        tags = getattr(audio, "tags", None) or {}
        recording = genres.values(tags, "musicbrainz_trackid")
        recording = genres.mbid(recording[0]) if len(recording) == 1 else ""
        group = genres.values(tags, "musicbrainz_releasegroupid")
        if group and {genres.mbid(value) for value in group} != {genres.mbid(album.get("foreignAlbumId"))}:
            continue
        if not recording:
            continue
        def first(key, fallback=""):
            values = genres.values(tags, key)
            return values[0] if values else fallback
        tracks.append({"recording": recording, "path": item["path"],
                       "title": first("title"), "artist": first("artist"),
                       "album": first("album", album.get("title", ""))})
    if tracks:
        job = {"version": 1, "album_id": str(album_id), "tracks": sorted(tracks, key=lambda x: x["path"])}
        digest = hashlib.sha256(json.dumps(job, sort_keys=True).encode()).hexdigest()
        genres.write_json(root / "jobs" / f"{digest}.json", job)
    return len(tracks)


def find_song(track, songs):
    matches = []
    local_path = str(track.get("path", "")).replace("\\", "/")
    for song in songs.values():
        recording = genres.mbid(song.get("musicBrainzId"))
        if recording and recording != track["recording"]:
            continue
        remote_path = str(song.get("path", "")).replace("\\", "/").lstrip("/")
        path_match = bool(remote_path and local_path.endswith("/" + remote_path))
        identity_match = (recording == track["recording"] and
                          normalized(song.get("album", "")) == normalized(track["album"]) and
                          normalized(song.get("artist", "")) == normalized(track["artist"]) and
                          normalized(song.get("title", "")) == normalized(track["title"]))
        if path_match or identity_match:
            matches.append(song)
    return matches[0] if len(matches) == 1 else None


def rating_xml(recording, value):
    return build_submission([("recording", recording, value * 20)])


def observe(state, song, recording, now):
    song_id = str(song["id"])
    current = stars(song)
    row = state["songs"].get(song_id)
    if row is None or row["recording"] != recording:
        # Unknown pre-existing ratings might themselves have come from MusicBrainz.
        # Baseline them; a subsequent change is eligible as a personal rating.
        row = {"recording": recording, "observed": current, "source": "unverified",
               "initialized": bool(current), "updated": now}
        state["songs"][song_id] = row
    elif row.get("pending_generation", {}).get("value") == current:
        generated = row.pop("pending_generation")
        row.update(observed=current, source=generated["source"], initialized=True,
                   generated=current, generated_source=generated["source"], updated=now)
    elif row["observed"] != current:
        row.update(observed=current, source="subsonic-change", initialized=True, updated=now)
        row.pop("pending_generation", None)
    return row


def apply_generated(row, song_id, generated_ratings):
    """Read optional provenance supplied by the caller; no producer import is needed."""
    generated = (generated_ratings or {}).get(song_id)
    if not generated or generated.get("value") != row["observed"]:
        return
    recording = genres.mbid(generated.get("recording"))
    if not recording or recording != row["recording"]:
        return
    stamp = generated.get("updated", 0)
    if row.get("generated_seen") == stamp:
        return
    source = generated.get("source")
    if source not in {"musicbrainz", "lastfm", "spotify", "tidal", "discogs"}:
        return
    row.update(source=source, generated=row["observed"], generated_source=source, generated_seen=stamp, initialized=True)
    row.pop("contribution", None)


def synchronize(state, songs, ledger, mb, save_ledger):
    grouped = defaultdict(list)
    for song_id, row in state["songs"].items():
        if song_id in songs:
            grouped[row["recording"]].append(row)
    failures = 0
    for recording, rows in grouped.items():
        def contribution(row):
            if row["source"] in {"unverified", "no-provider-rating"} and row.get("contribution"):
                return row["contribution"]["value"]
            return row["observed"]
        ratings = {contribution(row) for row in rows}
        if len(ratings) != 1:
            logging.warning("Conflicting ratings for recording %s; submission skipped", recording)
            continue
        value = ratings.pop()
        if not any(row["source"] in {"lastfm", "spotify", "tidal", "discogs", "subsonic-change", "navidrome-change", "subsonic-import"} or (row["source"] in {"unverified", "no-provider-rating"} and row.get("contribution")) for row in rows):
            continue
        if ledger.get(recording) == value or (value == 0 and recording not in ledger):
            continue
        try:
            mb.submit(rating_xml(recording, value), endpoint="rating")
        except Exception:
            # Requests exceptions may contain credential-bearing URLs. Never print them.
            logging.warning("MusicBrainz rating submission failed for %s; will retry", recording)
            failures += 1
            continue
        ledger[recording] = value
        save_ledger()
    return failures


def run_worker(root, server, mb, submit=False, account="", interval=900, backfill=False, now=None, rating_source=None, generated_ratings=None):
    """Observe existing ratings and contribute them; never write to Subsonic."""
    now = time.time() if now is None else now
    state_path = root / "state.json"
    state = load_json(state_path, {"version": 1, "songs": {}, "last_scan": 0})
    jobs = sorted((root / "jobs").glob("*.json"))
    if not backfill and not jobs and now - state["last_scan"] < interval:
        return 0
    songs = server.songs()  # Failed scans must never imply rating withdrawal.
    failures = 0
    for song_id, song in songs.items():
        recording = genres.mbid(song.get("musicBrainzId")) or state["songs"].get(song_id, {}).get("recording", "")
        if not recording:
            continue
        row = observe(state, song, recording, now)
        apply_generated(row, song_id, generated_ratings)
        # Preserve old provenance/history, but never resume queued generation.
        row.pop("pending_generation", None)
        row.pop("retry_track", None)
        if backfill and stars(song) and row["source"] in {"unverified", "no-provider-rating"}:
            row.update(source="subsonic-import", initialized=True)
    contribution_tracks = []
    if backfill:
        contribution_tracks.extend((song, {"recording": genres.mbid(song.get("musicBrainzId")), "artist": song.get("artist", ""), "title": song.get("title", ""), "album": song.get("album", "")}) for song in songs.values() if genres.mbid(song.get("musicBrainzId")))
    for path in jobs:
        try:
            job = load_json(path, {})
            remaining = []
            for track in job["tracks"]:
                song = find_song(track, songs)
                if song is None:
                    remaining.append(track)
                    continue
                latest = server.get_song(str(song["id"]))
                recording = genres.mbid(latest.get("musicBrainzId"))
                if recording and recording != track["recording"]:
                    remaining.append(track)
                    continue
                songs[str(song["id"])] = latest
                contribution_tracks.append((latest, track))
                row = observe(state, latest, track["recording"], now)
                apply_generated(row, str(latest["id"]), generated_ratings)
                if rating_source is not None and not stars(latest) and row["source"] in {"unverified", "no-provider-rating"}:
                    row["contribution_track"] = track
                if stars(latest) and row["source"] in {"unverified", "no-provider-rating"}:
                    row.update(source="subsonic-import", initialized=True)
            genres.write_json(state_path, state)  # Persist observations before consuming the job.
            if remaining:
                genres.write_json(path, {**job, "tracks": remaining})
            else:
                path.unlink()
        except Exception:
            logging.warning("Rating import observation failed; job retained for retry")
            failures += 1
    if rating_source is not None:
        seen = set()
        for song, track in contribution_tracks:
            identity = str(song["id"])
            row = state["songs"][identity]
            if identity in seen or stars(song) or row["source"] not in {"unverified", "no-provider-rating"}:
                continue
            seen.add(identity)
            row["contribution_track"] = track
        # Persist retry inputs: a source/API failure must not lose an import job.
        genres.write_json(state_path, state)
        for identity, row in state["songs"].items():
            track = row.get("contribution_track")
            if not track or identity not in songs or stars(songs[identity]) or row["source"] not in {"unverified", "no-provider-rating"}:
                continue
            try:
                vote = rating_source.rating(track)
                if vote is not None and 1 <= vote[0] <= 5:
                    row["contribution"] = {"value": vote[0], "source": vote[1]}
                row.pop("contribution_track", None)
            except Exception:
                logging.warning("Rating contribution source failed; retained for retry")
                failures += 1
    state["last_scan"] = now
    genres.write_json(state_path, state)
    if submit:
        ledger_path = root / f"submitted-{hashlib.sha256(account.encode()).hexdigest()[:16]}.json"
        ledger = load_json(ledger_path, {})
        failures += synchronize(state, songs, ledger, mb, lambda: genres.write_json(ledger_path, ledger))
    logging.info("Ratings: observed %d songs, %d failures", len(songs), failures)
    return failures


def subsonic_settings():
    """Prefer generic settings while retaining legacy Navidrome environments."""
    return ((os.getenv("SUBSONIC_URL") or os.getenv("NAVIDROME_URL") or os.getenv("NAVIDROME_BASE_URL") or os.getenv("NAV_BASE_URL", "")).rstrip("/"),
            os.getenv("SUBSONIC_USERNAME") or os.getenv("NAVIDROME_USERNAME") or os.getenv("NAV_USER", ""),
            os.getenv("SUBSONIC_PASSWORD") or os.getenv("NAVIDROME_PASSWORD") or os.getenv("NAV_PASS", ""))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("enqueue", "work", "library"))
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--album-id", type=int)
    parser.add_argument("--rating-source", choices=("subsonic", "lastfm"),
                        default=os.getenv("RATINGS_SOURCE", "subsonic"),
                        help="Existing Subsonic ratings, or Last.fm contributions for unrated songs")
    parser.add_argument("--submit", action="store_true", help="Submit contributed ratings to MusicBrainz")
    args = parser.parse_args()
    if args.rating_source not in {"subsonic", "lastfm"}:
        parser.error("RATINGS_SOURCE must be subsonic or lastfm")
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    server_url, server_user, server_password = subsonic_settings()
    identity = hashlib.sha256(json.dumps([server_url, server_user]).encode()).hexdigest()[:16]
    root = args.state_dir / identity
    try:
        if args.command == "enqueue":
            if not args.album_id or args.album_id < 1:
                raise ValueError("A positive album ID is required")
            if not (server_url and server_user):
                raise ValueError("Configure subsonicUrl and subsonicUsername before queuing ratings")
            enqueue(root, args.album_id)
            return 0
        rating_source = None
        if args.rating_source == "lastfm":
            key = os.getenv("LASTFM_API_KEY", "")
            if not key:
                raise ValueError("Last.fm contributions require LASTFM_API_KEY")
            rating_source = LastFmRatingSource(key)
        server = Subsonic(server_url, server_user, server_password)
        account, token = os.getenv("MB_USERNAME", ""), (os.getenv("MB_TOKEN") or os.getenv("MB_PASSWORD", ""))
        submit = (args.submit or os.getenv("RATINGS_SUBMIT") == "true") and bool(account and token)
        if args.submit and not (account and token):
            raise ValueError("Submissions require MB_USERNAME and MB_TOKEN")
        provenance_path = os.getenv("RATINGS_GENERATED_RATINGS")
        if not provenance_path and os.getenv("RATINGS_GENERATED_RATINGS_DIR"):
            provenance_path = str(Path(os.environ["RATINGS_GENERATED_RATINGS_DIR"]) / identity / "generated-ratings.json")
        # Share the genre worker lock: no concurrent MusicBrainz calls within this account.
        shared = genres.profile_root(args.state_dir.parent / "musicbrainz-helper", genres.account_profile(account))
        with genres.musicbrainz_lock(shared), genres.worker_lock(root):
            mb = core.MusicBrainzClient(core.JsonCache(root / "mb-cache.json"), account, token)
            failures = run_worker(root, server, mb,
                                  submit=submit, account=account,
                                  interval=interval_seconds(os.getenv("RATINGS_INTERVAL", "15m")),
                                  backfill=args.command == "library", rating_source=rating_source,
                                  generated_ratings=load_json(Path(provenance_path), {}) if provenance_path else None)
        return 1 if failures else 0
    except Exception:
        logging.error("Ratings helper failed; check configuration and service availability. State retained for retry.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
