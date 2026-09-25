# MusicBrainz Helper

Tools for contributing personal ratings and missing Apple Music catalog data to
MusicBrainz with reviewable, rate-limited workflows.

The project currently keeps each job in its own script:

- `musicbrainz-ratings-helper.py` pushes Navidrome ratings to existing
  MusicBrainz entities.
- `musicbrainz-new-artist-helper.py` discovers an Apple Music artist catalog,
  checks MusicBrainz for possible duplicates, and generates prefilled editor
  forms for missing releases.
- `itunes-library-helper.py` extracts ratings, genres, artists, and playlist
  membership from iTunes library and playlist exports into reviewable reports.
- `lidarr-lastfm-genres-helper.py` maps Last.fm genres onto MusicBrainz artists,
  release groups, every release version, and recordings from a Lidarr catalog.
- `navidrome-genres-helper.py` contributes genres already embedded in a
  Navidrome library to matching MusicBrainz entities.

## Navidrome Genre Helper

Preview genre upvotes from five Navidrome albums:

```bash
python navidrome-genres-helper.py --max-albums 5
```

The preview writes `genres.csv`, `unmatched-genres.csv`,
`musicbrainz-tags.xml`, and `summary.json` under `navidrome-genre-reports/`.
Nothing is posted without `--submit`. Review the CSV and then submit with:

```bash
python navidrome-genres-helper.py --submit
```

The helper reads both the legacy single `genre` field and OpenSubsonic's
multi-value `genres` field. It uses embedded MusicBrainz IDs to target artists,
releases, release groups, and recordings, filters values against MusicBrainz's
recognized genre vocabulary, and limits each entity to seven genres by default.
Use repeated `--entity` options to select target types, `--artist-id` for one
Navidrome artist, and `--max-tags` to change the per-entity limit.

Set `NAVIDROME_BASE_URL`, `NAVIDROME_USERNAME`, and `NAVIDROME_PASSWORD` in
`.env`. MusicBrainz credentials are required only with `--submit`.

## Lidarr Last.fm Genre Helper

Preview genre upvotes for Lidarr's wanted/missing albums (the default):

```bash
python lidarr-lastfm-genres-helper.py
```

Process every album known to Lidarr instead:

```bash
python lidarr-lastfm-genres-helper.py --scope all
```

Process only albums that are not currently wanted/missing:

```bash
python lidarr-lastfm-genres-helper.py --scope present
```

After reviewing `lidarr-lastfm-genre-reports/genres.csv` and
`musicbrainz-tags.xml`, submit the upvotes with:

```bash
python lidarr-lastfm-genres-helper.py --submit
```

Set `LIDARR_URL`, `LIDARR_API_KEY`, and `LASTFM_API_KEY` in `.env`. MusicBrainz
credentials are needed only for `--submit`. Last.fm community tags are filtered
against MusicBrainz's recognized genre vocabulary and limited to seven per
entity by default. Use repeated `--entity` options to limit targets, such as
`--entity artist --entity release-group`, and `--max-albums 5` for a short test.

Missing scope means albums listed by Lidarr as wanted/missing. Their MusicBrainz
release-group IDs are expanded to all known release versions and recordings, so
the contribution does not depend on already having local audio files.

Long runs checkpoint every 25 albums. Reports, `progress-preview.json` or
`progress-submit.json`,
`submitted-votes.json`, `failed-albums.csv`, and `failed-submissions.csv` are
updated in the output directory. Restart an interrupted run with `--resume`;
already completed albums and accepted entity/genre votes are skipped:

```bash
python lidarr-lastfm-genres-helper.py --scope missing --submit --resume
```

Preview and submission progress are intentionally separate, so a reviewed
preview does not make a later `--submit --resume` run skip those albums.

Use `--start-album-id` with either a Lidarr album ID or MusicBrainz
release-group MBID, or `--start-artist-id` with a Lidarr artist ID or
MusicBrainz artist MBID. `--artist-id` limits a run to one artist. Timestamped
logs are written under `logs/`.

Last.fm lookups use MusicBrainz IDs by default to avoid same-name artist, album,
and recording collisions. `--name-fallback` permits broader name-based lookups
when Last.fm has no tags for an MBID; review those results particularly closely.
MusicBrainz submissions are incremental, recorded per genre vote, retried on
transient failures, and split down to individual votes when a batch is rejected.

## iTunes Library Helper

Export an iTunes XML library without modifying iTunes or MusicBrainz:

```bash
python itunes-library-helper.py "/path/to/Library.xml"
```

Tab-separated playlist exports are supported too:

```bash
python itunes-library-helper.py "/path/to/playlist.txt" \
  --output-dir itunes-library-reports/playlist
```

The output includes `tracks.csv`, explicitly rated tracks in `ratings.csv`,
loved/disliked flags in `preferences.csv`, artist genre summaries in
`artists.csv`, genre counts in `genres.csv`, playlist membership in
`playlist-tracks.csv`, and `summary.json`. The original 0-100 iTunes rating is
preserved for MusicBrainz; a 0-5 star column is included only as a readable and
Navidrome-compatible representation. Computed album ratings remain identified
as computed and are not treated as explicit track ratings.

Preview MusicBrainz matches for explicitly rated tracks:

```bash
python itunes-library-helper.py "/path/to/Library.xml" --musicbrainz
```

The preview writes accepted, ambiguous, and unmatched CSV files plus the exact
rating and genre XML payloads. Matches are cached so repeated runs do not repeat
completed searches. After reviewing accepted matches, submit ratings and genre
upvotes with `--submit`. Use `--match-scope all` to process genres for the full
library; large exports are intentionally rate-limited and can take a long time.

## Apple Music Artist Helper

MusicBrainz does not permit artists, releases, or tracklists to be created
directly through its web-service API. This helper uses the supported seeding
system instead: it creates a local review page whose buttons open the official
MusicBrainz release editor with Apple metadata already filled in. The helper
never submits an edit by itself.

Preview an Apple Music artist catalog and generate the review page:

```bash
python musicbrainz-new-artist-helper.py \
  "https://music.apple.com/us/artist/youngvynn-q/6776781654"
```

An album URL also works; the helper resolves its credited artist:

```bash
python musicbrainz-new-artist-helper.py \
  "https://music.apple.com/us/album/he-said-i-looked-fine-single/6803960307"
```

Open the generated report automatically:

```bash
python musicbrainz-new-artist-helper.py APPLE_URL --open
```

For a short test, process only the newest release:

```bash
python musicbrainz-new-artist-helper.py APPLE_URL --max-releases 1 --open
```

If the report shows multiple same-name artist candidates, inspect them and
rerun with the correct MBID instead of creating a duplicate:

```bash
python musicbrainz-new-artist-helper.py APPLE_URL \
  --artist-mbid f6879d41-00c4-48c6-90cb-5f0875c973e0 --open
```

If the label already exists in MusicBrainz, its MBID can be pinned too:

```bash
python musicbrainz-new-artist-helper.py APPLE_URL \
  --artist-mbid ARTIST_MBID --label-mbid LABEL_MBID --open
```

The helper fills release titles, artist credits, dates, country, digital-media
format, release type, track numbers, exact Apple durations, label-name hints,
the Apple source URL, and an edit note. Store-generated ` - Single` and ` - EP`
suffixes are removed by default. Use `--keep-store-suffixes` when the suffix is
actually part of the official title.

By default, a seed is suppressed when MusicBrainz has an exact title and artist
match with a compatible release date. Date matching compares the year and, when
both sources provide it, the month; day differences are tolerated because
storefront availability can vary. Candidate links remain visible in the report
so you can inspect less certain matches. `--include-existing` generates forms
even for compatible matches. Suppressed releases also retain a per-release
force button for cases where you verify that the Apple release is distinct;
use it carefully to avoid creating duplicates. Candidates that do not suppress
a normal seed show the title, artist, or date difference that prevented the
match so you know what to verify or update. When an exact-title search finds
nothing, the helper also retries without a trailing parenthetical to reveal
possible title variants without automatically treating them as duplicates.

Apple does not provide enough information to safely infer every MusicBrainz
field. Review artist identity, artist type, title styling, featured-artist
credits, label identity, barcode, language, release events, relationships, and
duplicates before submitting each edit.

Set `MB_CONTACT` in `.env` to a public project URL or contact address. This is
included in the meaningful User-Agent required by MusicBrainz.

## Ratings Helper

Push Navidrome ratings to MusicBrainz.

The helper reads ratings from Navidrome through the Subsonic API and submits them to MusicBrainz as:

- artist ratings -> MusicBrainz artists
- album ratings -> MusicBrainz release groups
- song ratings -> MusicBrainz recordings

For songs, release-group expansion is enabled by default. When a rated Navidrome song belongs to a MusicBrainz release group, the helper finds matching recordings in that group so alternate releases can receive the same recording rating.

It can also run in a direct MusicBrainz force mode. In that mode, it bypasses Navidrome, walks every release group credited to a MusicBrainz artist MBID, and submits one forced release-group rating for each one.

## Setup

Install dependencies:

```bash
python3 -m pip install -r requirements.txt
```

Using a Linux virtual environment is recommended:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Create a `.env` or set these environment variables:

```text
NAVIDROME_BASE_URL=https://navidrome.example.com
NAVIDROME_USERNAME=your-navidrome-username
NAVIDROME_PASSWORD=your-navidrome-password

MB_USERNAME=your-musicbrainz-username
MB_PASSWORD=your-musicbrainz-password
```

The helper automatically reads `.env` from the current project directory.

## Usage

Preview what would be submitted from Navidrome:

```bash
python musicbrainz-ratings-helper.py --dry-run
```

Run for one Navidrome artist:

```bash
python musicbrainz-ratings-helper.py --dry-run --artist-id 5YPCM8WgUTYDxJPS8QUuOO
```

Resume from a Navidrome artist and continue onward in artist order:

```bash
python musicbrainz-ratings-helper.py --start-artist-id 7dB07x8Q2P9jPvGeDHxIFa
```

Resume from a Navidrome album and continue onward in album-title order:

```bash
python musicbrainz-ratings-helper.py --start-album-id 5WYUQdSkSzSHf4714jsHDM
```

Submit Navidrome ratings for real:

```bash
python musicbrainz-ratings-helper.py
```

Preview forced release-group ratings for one MusicBrainz artist:

```bash
python musicbrainz-ratings-helper.py --force-artist-ratings 6b656576-9504-432e-823d-8920139db2f0 --override-rating 1 --max-release-groups 5 --dry-run
```

Submit forced release-group ratings for one MusicBrainz artist:

```bash
python musicbrainz-ratings-helper.py --force-artist-ratings 6b656576-9504-432e-823d-8920139db2f0 --override-rating 1
```

## Useful Flags

- `--dry-run` previews the same rating batches without posting to MusicBrainz.
- `--artist-id ID` limits album and song processing to one Navidrome artist.
- `--start-artist-id ID` skips artists until the matching Navidrome artist, then continues onward in artist order. Album/song processing also follows artist order when this flag is used.
- `--start-album-id ID` skips album/song processing until the matching Navidrome album, then continues onward in the current album traversal order. When no `--entity` flags are provided, this resumes only `song` and `album` work. Skips all artist ratings.
- `--entity song`, `--entity album`, and `--entity artist` limit exported entity types. Repeat the flag for multiple types.
- `--override-rating N` submits `N` as the source rating instead of each Navidrome rating. Use a 0-5 value; `1` submits a one-star MusicBrainz rating.
- `--include-unrated-albums` includes unrated Navidrome albums when exporting `--entity album` with `--override-rating`.
- `--force-artist-ratings MBID` bypasses Navidrome and forces release-group ratings directly for every release group credited to the MusicBrainz artist MBID. This mode supports album/release-group ratings only and requires `--override-rating`.
- `--max-release-groups N` limits how many MusicBrainz release groups are collected in `--force-artist-ratings` mode.
- `--max-artists N` limits artist rating collection.
- `--max-albums N` limits album/song collection.
- `--no-expand-release-groups` disables recording fan-out within release groups.
- `--log-level DEBUG` shows detailed matching and MusicBrainz resolution logs.

## Logging

Normal logs are grouped by artist and album. Rating lines show the Navidrome source rating and the MusicBrainz rating that will be submitted:

```text
Artist: s:2 -> mb:40 | 3Breezy / 3Breezy: dry-run
Album: s:3 -> mb:60 | Murda She Wrote / 3Breezy: dry-run
Recording: s:2 -> mb:40 | Bacc To Tha Basics / 3Breezy: dry-run
```

Force mode logs one release-group rating per MusicBrainz release group:

```text
Album: s:1 -> mb:20 | Example Album / Example Artist: dry-run
```

The final summary includes scanned counts and previewed/submitted counts.

Detailed recording ID resolution is logged only with `--log-level DEBUG`.

Each run also writes a timestamped log file in `logs/`, for example:

```text
logs/musicbrainz-ratings-helper_1783864011.log
```

## Notes

- Navidrome uses 1-5 star ratings. MusicBrainz user ratings use a 0-100 scale, so the helper submits `rating * 20`.
- Zero or missing ratings are skipped.
- In `--force-artist-ratings` mode, MusicBrainz release groups are taken from the MusicBrainz artist browse results, so the list can be much larger than the albums you have in Navidrome.
- MusicBrainz API requests are throttled to about one request per second.
- Rating submissions are batched into one MusicBrainz POST where possible instead of sending one request per rating.
- Navidrome access uses the Subsonic API only; the helper does not read the Navidrome database directly.
