#!/usr/bin/env python3
"""Planning scaffold for a future Lidarr/Discogs genre contribution helper."""

from __future__ import annotations

import argparse
import json


PLAN = {
    "status": "scaffold",
    "source": "Discogs",
    "inputs": ["Lidarr albums", "MusicBrainz IDs", "Discogs release and master metadata"],
    "credentials": ["DISCOGS_TOKEN"],
    "identity_order": [
        "Discogs URL relationship already stored in MusicBrainz",
        "barcode plus artist and title",
        "catalog number plus label, artist, and title",
        "review-only artist/title search",
    ],
    "genre_fields": ["genres", "styles"],
    "musicbrainz_targets": ["artist", "release-group", "release"],
    "recording_policy": "Do not infer recording genres from release-level Discogs metadata.",
    "workflow": [
        "preview matches",
        "review ambiguous and unmatched records",
        "filter against the MusicBrainz genre vocabulary",
        "checkpoint and submit accepted votes incrementally",
    ],
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Show the staged design for importing Discogs genres from a Lidarr catalog."
    )
    parser.add_argument("--json", action="store_true", help="Print the scaffold plan as JSON.")
    return parser.parse_args()


def render_plan(plan: dict) -> str:
    lines = [f"{plan['source']} genre helper: {plan['status']}", "", "Identity order:"]
    lines.extend(f"  {index}. {value}" for index, value in enumerate(plan["identity_order"], start=1))
    lines.extend(("", "MusicBrainz targets: " + ", ".join(plan["musicbrainz_targets"])))
    lines.append("Recording policy: " + plan["recording_policy"])
    lines.append("Required future credential: " + ", ".join(plan["credentials"]))
    lines.append("")
    lines.append("This scaffold performs no API requests and cannot submit data.")
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    print(json.dumps(PLAN, indent=2) if args.json else render_plan(PLAN))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
