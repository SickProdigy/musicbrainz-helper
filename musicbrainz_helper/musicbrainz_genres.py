#!/usr/bin/env python3
"""Queue genres embedded in imported Lidarr files and contribute them to MusicBrainz."""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import time
import uuid

import mutagen
import requests

from . import musicbrainz as core


def mbid(value: object) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return ""


def values(tags: dict, key: str) -> list[str]:
    value = tags.get(key, [])
    return [value] if isinstance(value, str) else list(value)


def snapshot(album: dict, files: list[dict], source: str, read_tags=None) -> dict:
    """Use embedded IDs; reject files belonging to a different release group."""
    read_tags = read_tags or (lambda path: mutagen.File(path, easy=True))
    group = mbid(album.get("foreignAlbumId"))
    if not group:
        raise ValueError("Lidarr album has no valid MusicBrainz release-group ID")
    tracks = []
    for item in files:
        audio = read_tags(item["path"])
        tags = getattr(audio, "tags", None)
        if not tags:
            continue
        groups = values(tags, "musicbrainz_releasegroupid")
        if groups and {mbid(value) for value in groups} != {group}:
            logging.warning("Skipping file with conflicting release-group ID")
            continue
        genres = sorted({part.strip() for value in values(tags, "genre")
                         for part in value.split(";") if part.strip()})
        if not genres:
            continue
        tracks.append({
            "title": next(iter(values(tags, "title")), ""),
            "recording": mbid(next(iter(values(tags, "musicbrainz_trackid")), "")),
            "release": mbid(next(iter(values(tags, "musicbrainz_albumid")), "")),
            "genres": genres,
        })
    # Repeated files and reordered API responses must not create new jobs.
    tracks = sorted({json.dumps(track, sort_keys=True) for track in tracks})
    return {"version": 1, "album_id": str(album["id"]), "release_group": group,
            "artist": mbid((album.get("artist") or {}).get("foreignArtistId")),
            "name": album.get("title", ""), "source": source,
            "tracks": [json.loads(track) for track in tracks]}


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def enqueue(root: Path, job: dict) -> str:
    digest = hashlib.sha256(json.dumps(job, sort_keys=True).encode()).hexdigest()
    write_json(root / "jobs" / f"{digest}.json", job)
    return digest


def profile_root(root: Path, profile: str | None) -> Path:
    """An explicit local profile separates snapshots without changing the MB account."""
    if not profile:
        return root  # Preserve existing queues and ledgers.
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", profile):
        raise ValueError("Profile must be 1-64 letters/digits/dots/dashes/underscores, starting with a letter or digit")
    return root / "profiles" / profile


def account_profile(username: str) -> str:
    if not username:
        return "local"
    label = re.sub(r"[^A-Za-z0-9_-]", "_", username)[:40].strip("_") or "user"
    return f"{label}-{hashlib.sha256(username.encode()).hexdigest()[:12]}"


def scan_library(fetch, root: Path, source: str, read_tags=None) -> dict:
    """Snapshot present albums locally, then queue their genres without remote MB calls."""
    read_tags = read_tags or (lambda path: mutagen.File(path, easy=True))
    export = {"version": 1, "created_at": datetime.now(timezone.utc).isoformat(),
              "albums": [], "failures": [], "queued": 0, "missing": 0}
    tag_keys = ("title", "album", "artist", "albumartist", "genre", "musicbrainz_trackid",
                "musicbrainz_albumid", "musicbrainz_releasegroupid", "musicbrainz_artistid",
                "musicbrainz_albumartistid")
    for album in fetch("album"):
        try:
            files = fetch(f"trackfile?albumId={int(album['id'])}")
            if not files:
                export["missing"] += 1
                continue
            audio = {item["path"]: read_tags(item["path"]) for item in files}
            job = snapshot(album, files, source, audio.__getitem__)
            # Preserve unfiltered genres and file identities, including empty values.
            # These are a local export, not metadata uploaded to MusicBrainz.
            record = {"snapshot": job, "files": [
                {"path": path, "tags": {key: values(getattr(item, "tags", None) or {}, key)
                                        for key in tag_keys}}
                for path, item in audio.items()
            ]}
            export["albums"].append(record)
            if job["tracks"]:
                enqueue(root, job)
                export["queued"] += 1
        except Exception as exc:
            export["failures"].append({"album_id": str(album.get("id", "")), "error": str(exc)})
            logging.exception("Library album %s could not be exported; continuing", album.get("id"))
    destination = root / "library-snapshots" / f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex}.json"
    write_json(destination, export)
    logging.info("Library export: %d present albums, %d jobs, %d failures: %s",
                 len(export["albums"]), export["queued"], len(export["failures"]), destination)
    return export


def targets(job: dict, client, maximum: int, entities: list[str]) -> tuple[list[dict], list[str]]:
    vocabulary = client.genres()
    expanded = client.release_group(job["release_group"])
    releases = {mbid(release.get("id")) for release in expanded["releases"]}
    recordings = {mbid((track.get("recording") or {}).get("id"))
                  for release in expanded["releases"] for medium in release.get("media", [])
                  for track in medium.get("tracks", [])}
    result = {}
    unmatched = set()
    album_counts = Counter()
    recording_counts = {}
    for track in job["tracks"]:
        # A tagged release must belong to this group, even when Lidarr supplied its ID.
        if track["release"] and track["release"] not in releases:
            logging.warning("Skipping tagged release outside the imported release group")
            continue
        accepted = []
        for value in track["genres"]:
            canonical = vocabulary.get(core.normalized(value))
            if canonical:
                if canonical not in accepted:
                    accepted.append(canonical)
            else:
                unmatched.add(value)
        album_counts.update(accepted)
        recording = track["recording"]
        if recording and recording in recordings:
            recording_counts.setdefault(recording, (track["title"], Counter()))[1].update(accepted)
    album_genres = [genre for genre, _ in album_counts.most_common(maximum)]
    if "artist" in entities:
        core.add_target(result, "artist", mbid(job.get("artist")), job["name"], album_genres, job["source"])
    if "release-group" in entities:
        core.add_target(result, "release-group", job["release_group"], job["name"], album_genres, job["source"])
    if "release" in entities:
        for release in expanded["releases"]:
            core.add_target(result, "release", mbid(release.get("id")), release.get("title", job["name"]),
                            album_genres, job["source"])
    if "recording" in entities:
        for recording, (title, counts) in recording_counts.items():
            core.add_target(result, "recording", recording, title,
                            [genre for genre, _ in counts.most_common(maximum)], job["source"])
    return core.finalized_rows(result), sorted(unmatched)


@contextmanager
def worker_lock(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    with (root / "worker.lock").open("a+b") as handle:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            handle.write(b"0")
            handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


@contextmanager
def musicbrainz_lock(root: Path):
    # Share the gate across account profiles and both integrated workflows.
    shared = root.parent.parent if root.parent.name == "profiles" else root
    with worker_lock(shared):
        cooldown = shared / "worker-cooldown.json"
        if cooldown.exists():
            finished = json.loads(cooldown.read_text(encoding="utf-8"))["finished"]
            time.sleep(max(0, min(1.1, finished + 1.1 - time.time())))
        try:
            yield
        finally:
            write_json(cooldown, {"finished": time.time()})


def process(root: Path, submit: bool, maximum: int, entities: list[str]) -> int:
    username = os.getenv("MB_USERNAME", "")
    password = (os.getenv("MB_TOKEN") or os.getenv("MB_PASSWORD", ""))
    if submit and not (username and password):
        raise ValueError("Submission requires MB_USERNAME and MB_TOKEN (MusicBrainz application token)")
    account = hashlib.sha256(username.encode()).hexdigest()[:16] if submit else "preview"
    mode = f"submit-{account}" if submit else "preview"
    with musicbrainz_lock(root):
        client = core.MusicBrainzClient(core.JsonCache(root / "cache.json"), username, password)
        ledger = core.SubmissionLedger(root / f"submitted-votes-{account}.json")
        failures_count = 0
        policy = hashlib.sha256(json.dumps([maximum, sorted(entities)]).encode()).hexdigest()[:12]
        for path in sorted((root / "jobs").glob("*.json")):
            report = root / "reports" / mode / policy / path.stem
            if (report / "complete.json").exists():
                continue
            try:
                job = json.loads(path.read_text(encoding="utf-8"))
                rows, unmatched = targets(job, client, maximum, entities)
                failures = []
                summary = {"mode": mode, "musicbrainz_username": username or None,
                           "entities": len(rows), "unmatched_genres": unmatched}
                core.write_reports(report, rows, summary)
                if submit:
                    votes = [{**row, "genres": [genre]} for row in rows for genre in row["genres"]]
                    for offset in range(0, len(votes), 100):
                        core.submit_resilient(client, votes[offset:offset + 100], ledger, failures)
                core.write_failures(report, failures)
                if failures:
                    raise RuntimeError(f"{len(failures)} genre votes failed; job retained for retry")
                write_json(report / "complete.json", summary)
                logging.info("Processed %s: %d entities (%s)", path.stem[:12], len(rows), mode)
            except Exception:
                failures_count += 1
                logging.exception("Genre job %s failed; retained for retry", path.stem[:12])
        return 1 if failures_count else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("enqueue", "scan", "work"))
    parser.add_argument("--queue-dir", type=Path, required=True)
    parser.add_argument("--album-id", type=int)
    parser.add_argument("--profile", help="Optional local profile name, e.g. your MusicBrainz username.")
    parser.add_argument("--account-profile", action="store_true", help="Select a safe local profile automatically from MB_USERNAME.")
    parser.add_argument("--source", choices=("embedded-provider", "embedded-beets"), default="embedded-provider")
    parser.add_argument("--credentials-file", type=Path)
    parser.add_argument("--submit", action="store_true")
    parser.add_argument("--max-tags", type=int, default=7)
    parser.add_argument("--entity", action="append", choices=("artist", "release-group", "release", "recording"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.max_tags < 1:
        parser.error("--max-tags must be positive")
    if args.credentials_file:
        core.load_dotenv(args.credentials_file)
    if args.account_profile:
        if args.profile:
            parser.error("Use either --profile or --account-profile")
        args.profile = account_profile(os.getenv("MB_USERNAME", ""))
    try:
        root = profile_root(args.queue_dir, args.profile)
    except ValueError as exc:
        parser.error(str(exc))
    if args.command in {"enqueue", "scan"}:
        if args.command == "enqueue" and (not args.album_id or args.album_id < 1):
            parser.error("enqueue requires a positive --album-id")
        base = os.environ["LIDARR_URL"].rstrip("/")
        session = requests.Session()
        session.headers["X-Api-Key"] = os.environ["LIDARR_API_KEY"]
        def fetch(endpoint):
            response = session.get(f"{base}/api/v1/{endpoint}", timeout=(5, 15))
            response.raise_for_status()
            return response.json()
        if args.command == "scan":
            export = scan_library(fetch, root, args.source)
            return 1 if export["failures"] else 0
        album = fetch(f"album/{args.album_id}")
        files = fetch(f"trackfile?albumId={args.album_id}")
        job = snapshot(album, files, args.source)
        if job["tracks"]:
            logging.info("Queued %s", enqueue(root, job)[:12])
        else:
            logging.info("No usable embedded genres; no contribution queued")
        return 0
    return process(root, args.submit, args.max_tags,
                   args.entity or ["release-group", "release", "recording"])


if __name__ == "__main__":
    raise SystemExit(main())
