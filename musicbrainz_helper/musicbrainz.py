"""MusicBrainz API, genre votes and reports maintained by musicbrainz-helper.

Adapted from the standalone helper utilities; this module contains the installed
workflow's shared API and submission utilities.
"""
from __future__ import annotations
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
MB_NS = "http://musicbrainz.org/ns/mmd-2.0#"
CLIENT = "musicbrainz-helper/0.3.0"
RETRY_DELAYS = (1, 3, 10, 30)
ENTITY_LISTS = {"artist": "artist-list", "release-group": "release-group-list", "release": "release-list", "recording": "recording-list"}


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
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.data, indent=2, sort_keys=True), encoding="utf-8")
        temporary.replace(self.path)


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

    def submit(self, body: bytes, endpoint: str = "tag") -> None:
        if endpoint not in {"tag", "rating"}:
            raise ValueError("Unsupported MusicBrainz submission endpoint")
        for attempt, delay in enumerate(RETRY_DELAYS, start=1):
            wait = self.next_request - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self.next_request = time.monotonic() + 1.1
            try:
                response = self.session.post(
                    f"{MB_BASE}/{endpoint}", params={"client": CLIENT}, data=body,
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
