#!/usr/bin/env python3
"""Build reviewable MusicBrainz editor seeds from an Apple Music artist catalog."""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import time
import unicodedata
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import requests


SCRIPT_VERSION = "0.1.0"
APPLE_LOOKUP_URL = "https://itunes.apple.com/lookup"
MUSICBRAINZ_API_URL = "https://musicbrainz.org/ws/2"
MUSICBRAINZ_RELEASE_EDITOR = "https://musicbrainz.org/release/add"
MUSICBRAINZ_ARTIST_EDITOR = "https://musicbrainz.org/artist/create"
MUSICBRAINZ_RETRY_DELAYS = (3, 10, 30, 60)


@dataclass(frozen=True)
class AppleTrack:
    number: int
    title: str
    artist: str
    duration_ms: int
    apple_id: int


@dataclass(frozen=True)
class AppleRelease:
    apple_id: int
    title: str
    store_title: str
    artist: str
    artist_id: int
    release_date: str
    country: str
    genre: str
    copyright: str
    explicit: bool
    url: str
    primary_type: str
    tracks: tuple[AppleTrack, ...]


@dataclass(frozen=True)
class Match:
    mbid: str
    name: str
    disambiguation: str
    score: int
    exact: bool
    reasons: tuple[str, ...] = ()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Find Apple Music releases missing from MusicBrainz and create a "
            "local review page with prefilled MusicBrainz editor forms."
        )
    )
    parser.add_argument(
        "apple_artist",
        help="Apple Music artist URL/ID, or an Apple Music album URL to resolve its artist.",
    )
    parser.add_argument("--country", default="us", help="Apple storefront country code (default: us).")
    parser.add_argument("--artist-mbid", help="Use this known MusicBrainz artist MBID in release seeds.")
    parser.add_argument("--label-mbid", help="Use this known MusicBrainz label MBID in release seeds.")
    parser.add_argument("--language", help="ISO 639-3 release language, such as eng. Omitted if unknown.")
    parser.add_argument("--script", default="Latn", help="ISO 15924 title script (default: Latn).")
    parser.add_argument("--max-releases", type=int, help="Limit Apple releases for a short test run.")
    parser.add_argument(
        "--include-existing",
        action="store_true",
        help="Create seed forms even for releases with an exact MusicBrainz match.",
    )
    parser.add_argument(
        "--keep-store-suffixes",
        action="store_true",
        help="Keep Apple display suffixes such as ' - Single' and ' - EP' in release titles.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Output HTML path (default: new-artist-reports/<artist>-apple-music-import.html).",
    )
    parser.add_argument("--open", action="store_true", help="Open the generated report in a browser.")
    parser.add_argument("--log-level", choices=("DEBUG", "INFO", "WARNING", "ERROR"), default="INFO")
    return parser.parse_args()


def load_dotenv(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)


def normalize(value: str) -> str:
    folded = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "", folded.casefold())


def without_trailing_parenthetical(value: str) -> str:
    return re.sub(r"\s*[\[(][^\])]+[\])]\s*$", "", value).strip()


def compatible_release_dates(apple_date: str, musicbrainz_date: str) -> bool:
    """Match known date parts while tolerating storefront-level day differences."""
    apple_match = re.fullmatch(r"(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?", apple_date)
    musicbrainz_match = re.fullmatch(r"(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?", musicbrainz_date)
    if not apple_match or not musicbrainz_match:
        return False

    apple_year, apple_month, _ = apple_match.groups()
    musicbrainz_year, musicbrainz_month, _ = musicbrainz_match.groups()
    if apple_year != musicbrainz_year:
        return False
    return not (apple_month and musicbrainz_month) or apple_month == musicbrainz_month


def lucene_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def safe_filename(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-.").lower()
    return cleaned or "artist"


def apple_country_to_mb(value: str) -> str:
    return {"USA": "US", "United States": "US"}.get(value, value[:2].upper())


def strip_store_suffix(title: str) -> str:
    return re.sub(r"\s+-\s+(?:single|ep)$", "", title, flags=re.IGNORECASE).strip()


def infer_primary_type(store_title: str, track_count: int) -> str:
    if re.search(r"\s+-\s+ep$", store_title, flags=re.IGNORECASE):
        return "EP"
    if re.search(r"\s+-\s+single$", store_title, flags=re.IGNORECASE) or track_count <= 3:
        return "Single"
    return "Album"


def infer_label(copyright_text: str) -> str:
    value = re.sub(r"^\s*[℗©]\s*", "", copyright_text)
    value = re.sub(r"^\d{4}\s+", "", value)
    return value.strip()


def normalize_track_credit(title: str, artist: str) -> tuple[str, str]:
    match = re.match(
        r"^(?P<title>.+?)\s*[\[(](?:feat(?:uring)?\.?|ft\.?)\s+(?P<artists>.+?)[\])]$",
        title.strip(),
        flags=re.IGNORECASE,
    )
    if not match:
        return title.strip(), artist.strip()

    featured = match.group("artists").strip()
    credited_names = {normalize(name) for name, _ in split_artist_credit(artist)}
    featured_names = [name for name, _ in split_artist_credit(featured)]
    if all(normalize(name) in credited_names for name in featured_names):
        return match.group("title").strip(), artist.strip()
    return match.group("title").strip(), f"{artist.strip()} feat. {featured}"


def extract_apple_id(value: str) -> tuple[int, str]:
    if value.isdigit():
        return int(value), "unknown"
    parsed = urlparse(value)
    if "music.apple.com" not in parsed.netloc.casefold():
        raise ValueError("Expected an Apple Music URL or numeric Apple ID.")
    query_id = parse_qs(parsed.query).get("i", [None])[0]
    path_match = re.search(r"/(artist|album)/[^/]+/(\d+)", parsed.path)
    if not path_match:
        raise ValueError("Could not find an artist or album ID in the Apple Music URL.")
    kind, path_id = path_match.groups()
    return int(query_id or path_id), "track" if query_id else kind


class AppleClient:
    def __init__(self, country: str) -> None:
        self.country = country.lower()
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": f"musicbrainz-helper/{SCRIPT_VERSION}"})

    def lookup(self, apple_id: int, entity: str | None = None, limit: int = 200) -> list[dict]:
        params: dict[str, object] = {"id": apple_id, "country": self.country, "limit": limit}
        if entity:
            params["entity"] = entity
        response = self.session.get(APPLE_LOOKUP_URL, params=params, timeout=30)
        response.raise_for_status()
        payload = response.json()
        return payload.get("results", [])

    def resolve_artist(self, source: str) -> tuple[int, str]:
        apple_id, kind = extract_apple_id(source)
        if kind == "artist":
            rows = self.lookup(apple_id)
        else:
            rows = self.lookup(apple_id, "song")
        for row in rows:
            if row.get("artistId") and row.get("artistName"):
                return int(row["artistId"]), str(row["artistName"])
        raise RuntimeError(f"Apple did not return an artist for ID {apple_id}.")

    def releases(
        self, artist_id: int, artist_name: str, keep_store_suffixes: bool, limit: int | None
    ) -> list[AppleRelease]:
        rows = self.lookup(artist_id, "album")
        collections = [
            row
            for row in rows
            if row.get("wrapperType") == "collection"
            and int(row.get("artistId", 0)) == artist_id
            or row.get("wrapperType") == "collection"
            and any(
                normalize(name) == normalize(artist_name)
                for name, _ in split_artist_credit(str(row.get("artistName", "")))
            )
        ]
        unique = {int(row["collectionId"]): row for row in collections}
        selected = sorted(
            unique.values(),
            key=lambda row: (row.get("releaseDate", ""), row.get("collectionName", "")),
            reverse=True,
        )
        if limit is not None:
            selected = selected[:limit]

        releases: list[AppleRelease] = []
        for index, collection in enumerate(selected, start=1):
            collection_id = int(collection["collectionId"])
            logging.info("Apple release %d/%d: %s", index, len(selected), collection.get("collectionName"))
            detail_rows = self.lookup(collection_id, "song")
            track_rows = [row for row in detail_rows if row.get("wrapperType") == "track"]
            track_rows.sort(key=lambda row: (int(row.get("discNumber", 1)), int(row.get("trackNumber", 0))))
            if not track_rows:
                logging.warning("Skipping Apple collection %s because it has no tracks.", collection_id)
                continue

            store_title = str(collection.get("collectionName", "")).strip()
            title = store_title if keep_store_suffixes else strip_store_suffix(store_title)
            tracks_list = []
            for position, row in enumerate(track_rows, start=1):
                track_title, track_artist = normalize_track_credit(
                    str(row.get("trackName", "")),
                    str(row.get("artistName", collection.get("artistName", ""))),
                )
                tracks_list.append(
                    AppleTrack(
                        number=int(row.get("trackNumber", position)),
                        title=track_title,
                        artist=track_artist,
                        duration_ms=int(row.get("trackTimeMillis", 0)),
                        apple_id=int(row.get("trackId", 0)),
                    )
                )
            tracks = tuple(tracks_list)
            releases.append(
                AppleRelease(
                    apple_id=collection_id,
                    title=title,
                    store_title=store_title,
                    artist=str(collection.get("artistName", "")).strip(),
                    artist_id=int(collection.get("artistId", artist_id)),
                    release_date=str(collection.get("releaseDate", ""))[:10],
                    country=apple_country_to_mb(str(collection.get("country", self.country))),
                    genre=str(collection.get("primaryGenreName", "")),
                    copyright=str(collection.get("copyright", "")),
                    explicit=str(collection.get("collectionExplicitness", "")).casefold() == "explicit",
                    url=str(collection.get("collectionViewUrl", "")).replace("?uo=4", ""),
                    primary_type=infer_primary_type(store_title, len(tracks)),
                    tracks=tracks,
                )
            )
        return releases


class MusicBrainzClient:
    def __init__(self) -> None:
        self.session = requests.Session()
        contact = os.getenv("MB_CONTACT", "https://github.com/SickProdigy/musicbrainz-helper")
        self.session.headers.update(
            {"User-Agent": f"musicbrainz-helper/{SCRIPT_VERSION} ({contact})", "Accept": "application/json"}
        )
        self.next_request_at = 0.0

    def search(self, entity: str, query: str, limit: int = 10) -> dict:
        for attempt, delay in enumerate((*MUSICBRAINZ_RETRY_DELAYS, 0), start=1):
            wait = self.next_request_at - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self.next_request_at = time.monotonic() + 1.1
            try:
                response = self.session.get(
                    f"{MUSICBRAINZ_API_URL}/{entity}",
                    params={"query": query, "fmt": "json", "limit": limit},
                    timeout=30,
                )
                if response.status_code in {429, 500, 502, 503, 504} and delay:
                    retry_after = int(response.headers.get("Retry-After", delay))
                    logging.warning("MusicBrainz returned %s; retrying in %ss.", response.status_code, retry_after)
                    time.sleep(retry_after)
                    continue
                response.raise_for_status()
                return response.json()
            except (requests.ConnectionError, requests.Timeout):
                if not delay:
                    raise
                logging.warning("MusicBrainz request %d failed; retrying in %ss.", attempt, delay)
                time.sleep(delay)
        raise RuntimeError("MusicBrainz search retries were exhausted.")

    def artist_matches(self, name: str) -> list[Match]:
        payload = self.search("artist", f"artist:{lucene_quote(name)}", 10)
        return [
            Match(
                mbid=str(row.get("id", "")),
                name=str(row.get("name", "")),
                disambiguation=str(row.get("disambiguation", "")),
                score=int(row.get("score", 0)),
                exact=normalize(str(row.get("name", ""))) == normalize(name),
            )
            for row in payload.get("artists", [])
        ]

    def artist_name(self, mbid: str) -> str | None:
        wait = self.next_request_at - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self.next_request_at = time.monotonic() + 1.1
        response = self.session.get(
            f"{MUSICBRAINZ_API_URL}/artist/{mbid}", params={"fmt": "json"}, timeout=30
        )
        response.raise_for_status()
        return str(response.json().get("name", "")) or None

    def release_date(self, mbid: str) -> str:
        wait = self.next_request_at - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self.next_request_at = time.monotonic() + 1.1
        response = self.session.get(
            f"{MUSICBRAINZ_API_URL}/release/{mbid}", params={"fmt": "json"}, timeout=30
        )
        response.raise_for_status()
        return str(response.json().get("date", ""))

    def release_matches(self, release: AppleRelease, artist_mbid: str | None) -> list[Match]:
        artist_query = f"arid:{artist_mbid}" if artist_mbid else f"artist:{lucene_quote(release.artist)}"
        query = f"release:{lucene_quote(release.title)} AND {artist_query}"
        payload = self.search("release", query, 10)
        rows = payload.get("releases", [])
        simpler_title = without_trailing_parenthetical(release.title)
        if not rows and simpler_title != release.title:
            query = f"release:{lucene_quote(simpler_title)} AND {artist_query}"
            rows = self.search("release", query, 10).get("releases", [])
        matches: list[Match] = []
        for row in rows:
            mbid = str(row.get("id", ""))
            candidate_title = str(row.get("title", ""))
            credit = "".join(
                str(part.get("name", "")) + str(part.get("joinphrase", ""))
                for part in row.get("artist-credit", [])
            )
            title_matches = normalize(candidate_title) == normalize(release.title)
            artist_matches = bool(artist_mbid) or normalize(credit) == normalize(release.artist)
            candidate_date = str(row.get("date", ""))
            if title_matches and artist_matches and release.release_date and not candidate_date and mbid:
                candidate_date = self.release_date(mbid)
            date_matches = (
                not release.release_date
                or compatible_release_dates(release.release_date, candidate_date)
            )
            exact = title_matches and artist_matches and date_matches
            reasons = []
            if not title_matches:
                reasons.append("title differs")
            if not artist_matches:
                reasons.append("artist differs")
            if release.release_date and candidate_date and candidate_date != release.release_date:
                reasons.append("date differs")
            elif release.release_date and not candidate_date:
                reasons.append("date unavailable")
            matches.append(
                Match(
                    mbid=mbid,
                    name=candidate_title,
                    disambiguation=f"{credit} | {candidate_date} | {row.get('country', '')}",
                    score=int(row.get("score", 0)),
                    exact=exact,
                    reasons=tuple(reasons),
                )
            )
        return matches


def choose_exact_artist(matches: list[Match]) -> str | None:
    exact = [match for match in matches if match.exact]
    return exact[0].mbid if len(exact) == 1 else None


def split_artist_credit(credit: str) -> list[tuple[str, str]]:
    parts = re.split(r"(\s+feat\.\s+|\s*,\s*|\s+&\s+)", credit.strip(), flags=re.IGNORECASE)
    credits: list[tuple[str, str]] = []
    for index in range(0, len(parts), 2):
        name = parts[index].strip()
        if not name:
            continue
        join_phrase = parts[index + 1] if index + 1 < len(parts) else ""
        credits.append((name, join_phrase))
    return credits


def add_artist_credit_fields(
    fields: list[tuple[str, str]],
    prefix: str,
    credit: str,
    target_artist_name: str,
    target_artist_mbid: str | None,
) -> None:
    for index, (name, join_phrase) in enumerate(split_artist_credit(credit)):
        field = f"{prefix}.{index}"
        fields.extend(((f"{field}.name", name), (f"{field}.artist.name", name)))
        if join_phrase:
            fields.append((f"{field}.join_phrase", join_phrase))
        if target_artist_mbid and normalize(name) == normalize(target_artist_name):
            fields.append((f"{field}.mbid", target_artist_mbid))


def release_seed(
    release: AppleRelease,
    artist_mbid: str | None,
    label_mbid: str | None,
    language: str | None,
    script: str,
    target_artist_name: str | None = None,
) -> list[tuple[str, str]]:
    target_artist_name = target_artist_name or release.artist
    fields: list[tuple[str, str]] = [
        ("name", release.title),
        ("type", release.primary_type),
        ("status", "official"),
        ("packaging", "None"),
        ("script", script),
        ("barcode", "none"),
        ("events.0.country", release.country),
        ("mediums.0.format", "Digital Media"),
        ("urls.0.url", release.url),
        (
            "edit_note",
            f"Seeded from the official Apple Music listing:\n{release.url}\n\n"
            f"Apple Music lists {len(release.tracks)} track(s), released {release.release_date or 'on an unknown date'}, "
            f"credited to {release.artist}.",
        ),
    ]
    add_artist_credit_fields(fields, "artist_credit.names", release.artist, target_artist_name, artist_mbid)
    if language:
        fields.append(("language", language))
    if release.release_date:
        year, month, day = release.release_date.split("-")
        fields.extend(
            (("events.0.date.year", year), ("events.0.date.month", month), ("events.0.date.day", day))
        )
    label_name = infer_label(release.copyright)
    if label_mbid:
        fields.append(("labels.0.mbid", label_mbid))
    elif label_name:
        fields.append(("labels.0.name", label_name))
    fields.append(("labels.0.catalog_number", "[none]"))
    for index, track in enumerate(release.tracks):
        prefix = f"mediums.0.track.{index}"
        fields.extend(
            (
                (f"{prefix}.name", track.title),
                (f"{prefix}.number", str(track.number)),
                (f"{prefix}.length", str(track.duration_ms)),
            )
        )
        add_artist_credit_fields(
            fields, f"{prefix}.artist_credit.names", track.artist, target_artist_name, artist_mbid
        )
    return fields


def artist_create_url(name: str, apple_artist_id: int, country: str) -> str:
    apple_url = f"https://music.apple.com/{country.lower()}/artist/{safe_filename(name)}/{apple_artist_id}"
    params = {
        "edit-artist.name": name,
        "edit-artist.sort_name": name,
        "edit-artist.comment": f"Apple Music artist ID {apple_artist_id}",
        "edit-artist.edit_note": (
            f"Artist catalog found on Apple Music: {apple_url}\n"
            "Please verify artist type, area, relationships, and possible duplicates before submission."
        ),
    }
    return f"{MUSICBRAINZ_ARTIST_EDITOR}?{urlencode(params)}"


def hidden_fields(fields: list[tuple[str, str]]) -> str:
    return "\n".join(
        f'<input type="hidden" name="{html.escape(name, quote=True)}" value="{html.escape(value, quote=True)}">'
        for name, value in fields
    )


def match_list(matches: list[Match]) -> str:
    if not matches:
        return "<p>No MusicBrainz candidates found.</p>"
    rows = []
    for match in matches[:5]:
        marker = "exact" if match.exact else "possible"
        notes = " ".join(
            f'<strong class="reason">{html.escape(reason)}</strong>' for reason in match.reasons
        )
        rows.append(
            f'<li><a href="https://musicbrainz.org/release/{html.escape(match.mbid)}" target="_blank">'
            f"{html.escape(match.name)}</a> ({match.score}%, {marker}) {html.escape(match.disambiguation)} "
            f"{notes}</li>"
        )
    return "<ul>" + "".join(rows) + "</ul>"


def render_report(
    artist_name: str,
    artist_id: int,
    artist_matches: list[Match],
    artist_mbid: str | None,
    selected_artist_name: str | None,
    release_rows: list[tuple[AppleRelease, list[Match], bool]],
    args: argparse.Namespace,
) -> str:
    release_sections = []
    for release, matches, should_seed in release_rows:
        duration = sum(track.duration_ms for track in release.tracks) // 1000
        fields = release_seed(
            release, artist_mbid, args.label_mbid, args.language, args.script, artist_name
        )
        if should_seed:
            action = (
                f'<form method="post" enctype="multipart/form-data" action="{MUSICBRAINZ_RELEASE_EDITOR}" target="_blank">'
                f"{hidden_fields(fields)}"
                '<button type="submit">Open prefilled release editor</button></form>'
            )
        else:
            action = (
                '<p class="skip">Compatible MusicBrainz match found; seed suppressed.</p>'
                f'<form method="post" enctype="multipart/form-data" action="{MUSICBRAINZ_RELEASE_EDITOR}" target="_blank">'
                f"{hidden_fields(fields)}"
                '<button class="force" type="submit">Force open prefilled release editor</button></form>'
            )
        tracks = "".join(
            f"<li>{track.number}. {html.escape(track.title)} "
            f"({track.duration_ms // 60000}:{(track.duration_ms // 1000) % 60:02d})</li>"
            for track in release.tracks
        )
        release_sections.append(
            f"<section><h2>{html.escape(release.title)}</h2>"
            f"<p>{html.escape(release.primary_type)} | {html.escape(release.release_date)} | "
            f"{len(release.tracks)} track(s) | {duration // 60}:{duration % 60:02d} | "
            f'<a href="{html.escape(release.url, quote=True)}" target="_blank">Apple Music</a></p>'
            f"<ol>{tracks}</ol><h3>MusicBrainz candidates</h3>{match_list(matches)}{action}</section>"
        )

    if artist_mbid:
        selected_name = selected_artist_name or artist_mbid
        alternate_matches = [match for match in artist_matches if match.mbid != artist_mbid]
        alternatives = ""
        if alternate_matches:
            alternatives = (
                '<details><summary>Other name matches (not selected)</summary><ul>'
                + "".join(
                    f'<li><a href="https://musicbrainz.org/artist/{html.escape(match.mbid)}" target="_blank">'
                    f"{html.escape(match.name)}</a> ({match.score}% name similarity) "
                    f"{html.escape(match.disambiguation)}</li>"
                    for match in alternate_matches[:10]
                )
                + "</ul></details>"
            )
        artist_section = (
            "<h2>Selected MusicBrainz artist</h2>"
            f'<p><a href="https://musicbrainz.org/artist/{html.escape(artist_mbid)}" target="_blank">'
            f"{html.escape(selected_name)}</a><br>Apple credit: {html.escape(artist_name)}</p>{alternatives}"
        )
    else:
        artist_candidates = "<p>No artist candidates found.</p>"
        if artist_matches:
            artist_candidates = "<ul>" + "".join(
                f'<li><a href="https://musicbrainz.org/artist/{html.escape(match.mbid)}" target="_blank">'
                f"{html.escape(match.name)}</a> ({match.score}%, {'name match' if match.exact else 'possible'}) "
                f"{html.escape(match.disambiguation)}</li>"
                for match in artist_matches[:10]
            ) + "</ul>"
        artist_section = (
            f"<h2>Artist candidates</h2>{artist_candidates}"
            f'<p><a href="{html.escape(artist_create_url(artist_name, artist_id, args.country), quote=True)}" '
            'target="_blank">Open prefilled new-artist editor</a></p>'
        )
    resolved = artist_mbid or "not resolved; create/select the artist, then rerun with --artist-mbid"
    created_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(artist_name)} MusicBrainz import review</title>
<style>
body{{font:16px/1.5 system-ui,sans-serif;max-width:1000px;margin:2rem auto;padding:0 1rem;color:#202124}}
header,section{{border-bottom:1px solid #d8d8d8;padding:0 0 1.5rem;margin-bottom:1.5rem}}
button{{background:#ba478f;color:white;border:0;padding:.7rem 1rem;font-weight:700;cursor:pointer}}
.force{{background:#5f6368;padding:.3rem .5rem;font-size:.75rem;font-weight:600}}
.reason{{display:inline-block;background:#fff1df;color:#8a4300;padding:.1rem .35rem;font-size:.75rem;margin-left:.25rem}}
.warning{{background:#fff4ce;border-left:4px solid #c58b00;padding:1rem}} .skip{{color:#26734d;font-weight:700}}
code{{overflow-wrap:anywhere}} a{{color:#8f3575}}
summary{{cursor:pointer;font-weight:700}}
</style></head><body>
<header><h1>{html.escape(artist_name)}</h1>
<p>Apple artist ID: <code>{artist_id}</code><br>MusicBrainz artist MBID: <code>{html.escape(resolved)}</code><br>Generated {created_at}</p>
<p class="warning"><strong>Review every form.</strong> Apple storefront metadata can be incomplete, styled differently,
or duplicate an existing MusicBrainz entity. These buttons only prefill the official editor; they do not submit edits.</p>
{artist_section}
</header>{''.join(release_sections)}</body></html>"""


def main() -> int:
    args = parse_args()
    load_dotenv()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(levelname)s: %(message)s")
    if args.max_releases is not None and args.max_releases < 1:
        raise SystemExit("--max-releases must be at least 1.")

    apple = AppleClient(args.country)
    musicbrainz = MusicBrainzClient()
    artist_id, artist_name = apple.resolve_artist(args.apple_artist)
    logging.info("Apple artist: %s (%s)", artist_name, artist_id)

    artist_matches = musicbrainz.artist_matches(artist_name)
    artist_mbid = args.artist_mbid or choose_exact_artist(artist_matches)
    if artist_mbid:
        logging.info("MusicBrainz artist selected: %s", artist_mbid)
    else:
        logging.warning("No unique exact MusicBrainz artist selected; release forms will use an artist name search.")

    selected_artist_name = musicbrainz.artist_name(artist_mbid) if artist_mbid else None
    releases = apple.releases(artist_id, artist_name, args.keep_store_suffixes, args.max_releases)
    release_rows: list[tuple[AppleRelease, list[Match], bool]] = []
    exact_count = 0
    for index, release in enumerate(releases, start=1):
        logging.info("MusicBrainz check %d/%d: %s", index, len(releases), release.title)
        matches = musicbrainz.release_matches(release, artist_mbid)
        has_exact = any(match.exact for match in matches)
        exact_count += int(has_exact)
        release_rows.append((release, matches, args.include_existing or not has_exact))

    output = args.output or Path("new-artist-reports") / f"{safe_filename(artist_name)}-apple-music-import.html"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        render_report(
            artist_name,
            artist_id,
            artist_matches,
            artist_mbid,
            selected_artist_name,
            release_rows,
            args,
        ),
        encoding="utf-8",
    )
    summary = {
        "artist": artist_name,
        "apple_artist_id": artist_id,
        "musicbrainz_artist_mbid": artist_mbid,
        "apple_releases": len(releases),
        "exact_musicbrainz_releases": exact_count,
        "release_seeds": sum(1 for _, _, should_seed in release_rows if should_seed),
        "report": str(output.resolve()),
    }
    print(json.dumps(summary, indent=2))
    if args.open:
        webbrowser.open(output.resolve().as_uri())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
