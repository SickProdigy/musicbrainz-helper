# MusicBrainz Helper

Tools for contributing ratings, genres, and missing Apple Music catalog data to
MusicBrainz with reviewable, rate-limited workflows.

Install once, then choose the command for your task. Each command links to its usage examples; each script link opens its implementation.

| Command and usage | What it does | Script |
| --- | --- | --- |
| [musicbrainz-ratings](#ratings-helper) | Contributes Navidrome/Subsonic artist, album, and song ratings; optionally derives Last.fm recording ratings. | [ratings.py](musicbrainz_helper/ratings.py) |
| [musicbrainz-genres](#imported-lidarr-genres) | Queues embedded genres from Lidarr imports or a library scan and contributes them to MusicBrainz. | [musicbrainz_genres.py](musicbrainz_helper/musicbrainz_genres.py) |
| [musicbrainz-new-artist](#apple-music-artist-helper) | Finds Apple Music catalog entries missing from MusicBrainz and creates review pages with prefilled editor forms. | [new_artist.py](musicbrainz_helper/new_artist.py) |
| [musicbrainz-itunes-library](#itunes-library-helper) | Reads iTunes exports, writes reports, and optionally contributes matched ratings and genres. | [itunes_library.py](musicbrainz_helper/itunes_library.py) |
| [musicbrainz-lastfm-genres](#lidarr-lastfm-genre-helper) | Looks up Last.fm genres for albums known to Lidarr, including wanted/missing albums. | [lastfm_genres.py](musicbrainz_helper/lastfm_genres.py) |
| [musicbrainz-navidrome-genres](#navidrome-genre-helper) | Contributes genres already present in a Navidrome library. | [navidrome_genres.py](musicbrainz_helper/navidrome_genres.py) |

## Setup and repository layout

Requires Python 3.10 or newer. Run these commands from this repository.

Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
cp .env.example .env
```

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
Copy-Item .env.example .env
```

Copy the example only when first setting up; keep an existing `.env`. Configure the sources you use in [.env.example](.env.example). Activate the environment again in a new terminal. Editable installation makes the commands use your local source changes immediately.

Run manual commands from this repository so they find `.env` and write output under `logs/`. Use `--help` for any tool, such as `musicbrainz-ratings --help` or `musicbrainz-ratings library --help`.

```text
musicbrainz-helper/
  musicbrainz_helper/    tool implementations and shared MusicBrainz utilities
  tests/                tests for the package
  logs/                 generated logs, reports, and standalone checkpoints
  pyproject.toml        installation and command definitions
```

All tools live in `musicbrainz_helper/`. You can also run a module directly from the checkout, for example `python -m musicbrainz_helper.navidrome_genres --help`, after installing its dependencies. There are no root script wrappers.

Choose the command for the job you want. Source-specific matching stays in each tool; shared MusicBrainz API and submission utilities live in [musicbrainz.py](musicbrainz_helper/musicbrainz.py). The iTunes tool keeps ratings and genres together because both come from the same export.

Queue-based tools use **subcommands**: `musicbrainz-genres enqueue`, `scan`, or `work`; `musicbrainz-ratings enqueue`, `library`, or `work`. Options such as `--submit` and `--rating-source lastfm` select behavior within that workflow. Each command's `--help` lists its required arguments.

SonicRatings writes ratings to Navidrome/Subsonic. This repository gathers ratings and genres from supported sources and contributes them to MusicBrainz; it does not write ratings to Subsonic or require SonicRatings to be installed.


## Planned sources

Discogs genre contributions and Spotify enrichment are future work; they are not
available commands. Discogs release genres would apply to albums/releases, not
be assumed to describe every recording.

## Navidrome Genre Helper

Preview genre upvotes from five Navidrome albums:

```bash
musicbrainz-navidrome-genres --max-albums 5
```

The preview writes `genres.csv`, `unmatched-genres.csv`,
`musicbrainz-tags.xml`, and `summary.json` under `logs/navidrome-genre-reports/`.
Nothing is posted without `--submit`. Review the CSV and then submit with:

```bash
musicbrainz-navidrome-genres --submit
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
musicbrainz-lastfm-genres
```

Process every album known to Lidarr instead:

```bash
musicbrainz-lastfm-genres --scope all
```

Process only albums that are not currently wanted/missing:

```bash
musicbrainz-lastfm-genres --scope present
```

After reviewing `logs/lidarr-lastfm-genre-reports/genres.csv` and
`musicbrainz-tags.xml`, submit the upvotes with:

```bash
musicbrainz-lastfm-genres --submit
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
musicbrainz-lastfm-genres --scope missing --submit --resume
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
musicbrainz-itunes-library "/path/to/Library.xml"
```

Tab-separated playlist exports are supported too:

```bash
musicbrainz-itunes-library "/path/to/playlist.txt" \
  --output-dir logs/itunes-library-reports/playlist
```

Reports default to `logs/itunes-library-reports/`. The output includes `tracks.csv`, explicitly rated tracks in `ratings.csv`,
loved/disliked flags in `preferences.csv`, artist genre summaries in
`artists.csv`, genre counts in `genres.csv`, playlist membership in
`playlist-tracks.csv`, and `summary.json`. The original 0-100 iTunes rating is
preserved for MusicBrainz; a 0-5 star column is included only as a readable and
Navidrome-compatible representation. Computed album ratings remain identified
as computed and are not treated as explicit track ratings.

Preview MusicBrainz matches for explicitly rated tracks:

```bash
musicbrainz-itunes-library "/path/to/Library.xml" --musicbrainz
```

The preview writes accepted, ambiguous, and unmatched CSV files plus the exact
rating and genre XML payloads. Matches are cached so repeated runs do not repeat
completed searches. After reviewing accepted matches, submit ratings and genre
upvotes:

```bash
musicbrainz-itunes-library "/path/to/Library.xml" --musicbrainz --submit
```

Use `--match-scope all` to process genres for the full
library; large exports are intentionally rate-limited and can take a long time.

## Apple Music Artist Helper

MusicBrainz does not permit artists, releases, or tracklists to be created
directly through its web-service API. This helper uses the supported seeding
system instead: it creates a local review page whose buttons open the official
MusicBrainz release editor with Apple metadata already filled in. The helper
never submits an edit by itself.

Review pages default to `logs/new-artist-reports/<artist>-apple-music-import.html`; `--output PATH` selects another location.

Preview an Apple Music artist catalog and generate the review page:

```bash
musicbrainz-new-artist \
  "https://music.apple.com/us/artist/youngvynn-q/6776781654"
```

An album URL also works; the helper resolves its credited artist:

```bash
musicbrainz-new-artist \
  "https://music.apple.com/us/album/he-said-i-looked-fine-single/6803960307"
```

Open the generated report automatically:

```bash
musicbrainz-new-artist APPLE_URL --open
```

For a short test, process only the newest release:

```bash
musicbrainz-new-artist APPLE_URL --max-releases 1 --open
```

If the report shows multiple same-name artist candidates, inspect them and
rerun with the correct MBID instead of creating a duplicate:

```bash
musicbrainz-new-artist APPLE_URL \
  --artist-mbid f6879d41-00c4-48c6-90cb-5f0875c973e0 --open
```

If the label already exists in MusicBrainz, its MBID can be pinned too:

```bash
musicbrainz-new-artist APPLE_URL \
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

### Ratings configuration

Use the package installation above.

Create a `.env` or set these environment variables:

```text
NAVIDROME_BASE_URL=https://navidrome.example.com
NAVIDROME_USERNAME=your-navidrome-username
NAVIDROME_PASSWORD=your-navidrome-password

MB_USERNAME=your-musicbrainz-username
MB_TOKEN=your-musicbrainz-application-token
```

The helper automatically reads `.env` from the current project directory.

### Usage

Preview what would be submitted from Navidrome:

```bash
musicbrainz-ratings --dry-run
```

Run for one Navidrome artist:

```bash
musicbrainz-ratings --dry-run --artist-id 5YPCM8WgUTYDxJPS8QUuOO
```

Resume from a Navidrome artist and continue onward in artist order:

```bash
musicbrainz-ratings --start-artist-id 7dB07x8Q2P9jPvGeDHxIFa
```

Resume from a Navidrome album and continue onward in album-title order:

```bash
musicbrainz-ratings --start-album-id 5WYUQdSkSzSHf4714jsHDM
```

Submit Navidrome ratings for real:

```bash
musicbrainz-ratings
```

Preview forced release-group ratings for one MusicBrainz artist:

```bash
musicbrainz-ratings --force-artist-ratings 6b656576-9504-432e-823d-8920139db2f0 --override-rating 1 --max-release-groups 5 --dry-run
```

Submit forced release-group ratings for one MusicBrainz artist:

```bash
musicbrainz-ratings --force-artist-ratings 6b656576-9504-432e-823d-8920139db2f0 --override-rating 1
```

### Useful Flags

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

### Logging

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

### Notes

- Navidrome uses 1-5 star ratings. MusicBrainz user ratings use a 0-100 scale, so the helper submits `rating * 20`.
- Zero or missing ratings are skipped.
- In `--force-artist-ratings` mode, MusicBrainz release groups are taken from the MusicBrainz artist browse results, so the list can be much larger than the albums you have in Navidrome.
- MusicBrainz API requests are throttled to about one request per second.
- Rating submissions are batched into one MusicBrainz POST where possible instead of sending one request per rating.
- Navidrome access uses the Subsonic API only; the helper does not read the Navidrome database directly.
## Imported Lidarr genres

`musicbrainz-genres` accepts exact-album jobs from arr-scripts, reads
embedded provider or Beets genres, and uses the shared MusicBrainz
release expansion, filtering and submission utilities. It supports singles,
EPs and albums without requiring Beets or Navidrome.

```bash
musicbrainz-genres enqueue --album-id 123 --queue-dir logs/imported-genre-jobs --credentials-file .env
musicbrainz-genres work --queue-dir logs/imported-genre-jobs --credentials-file .env
musicbrainz-genres work --queue-dir logs/imported-genre-jobs --credentials-file .env --submit
```

Enqueue uses `LIDARR_URL` and `LIDARR_API_KEY` from the environment (or an explicit
`--credentials-file`). The worker prefers authorized MusicBrainz OAuth and retains
`MB_USERNAME` plus `MB_TOKEN` as a temporary Digest fallback. `MB_CONTACT` is
optional. Preview is the default. Imported genres target the release group,
all its release editions and exact embedded recording IDs verified in those
releases. Different recordings are not expanded by title alone. Repeated
`--entity` flags limit targets; artist contributions require an explicit
`--entity artist`. `--max-tags` defaults to seven per entity.

Jobs are atomic snapshots, coalesced by content. Preview and submission completion
are separate; reports are stored under `reports/<mode>/<policy>/<job>/`.
Submission ledgers are account-specific. One locked worker handles MusicBrainz
requests serially; failed jobs remain available for retry. Run `work` again to
retry or schedule it. The arr-scripts integration polls every minute.

See the [arr-scripts genre setup guide](https://github.com/SickProdigy/arr-scripts/blob/main/lidarr/musicbrainz-genres.md) for installation and settings. Final embedded tags represent the user's selected genres; source
labels describe the processing path and do not imply independent provider votes.

For a whole-library genre export and contribution pass:

```bash
musicbrainz-genres scan --queue-dir logs/imported-genre-jobs --profile sickprodigy --credentials-file .env
musicbrainz-genres work --queue-dir logs/imported-genre-jobs --profile sickprodigy --credentials-file .env
```

`scan` covers present Lidarr albums and saves a timestamped JSON export under
`library-snapshots/`, preserving raw genre values, file paths and selected matching
tags before MusicBrainz vocabulary filtering. It queues suggestions without
making MusicBrainz requests. Unreadable albums are reported while other albums
continue. Repeat a scan after retagging to retain new history and capture new
genres. This exports genre metadata, not audio or every possible tag, and does
not implement restoration.

An optional `--profile` label separates jobs, exports, reports and ledgers under
`<queue-dir>/profiles/<label>/`; use the same label on enqueue, scan and work.
Omitting it preserves the original layout. The authenticated `MB_USERNAME`
selects the actual MusicBrainz account, regardless of the local label. Submission
reports name that account. Retagging adds upvotes for new genres without
automatically withdrawing old votes. MusicBrainz retains user-specific tag votes,
not a complete restorable copy of the library.

The arr-scripts installer installs this package at a pinned release. Configure an
Installed Application client ID and secret in `extended.conf`, then run:

```bash
bash /config/extended/MusicBrainzGenres.bash --authorize
```

The command prints the MusicBrainz approval URL and prompts for the one-time
authorization code. It stores the refresh token privately under
`/config/extended/musicbrainz-helper/`; workers refresh short-lived access tokens
automatically. Enabled and authenticated installations contribute automatically.
The wrapper uses `--account-profile` to derive a safe,
distinct local profile from `MB_USERNAME`; no extra profile or submission setting
is needed. The standalone CLI above retains explicit preview/submission controls.

For standalone use, set `MB_OAUTH_CLIENT_ID`, `MB_OAUTH_CLIENT_SECRET`,
`MB_OAUTH_TOKEN_FILE`, and optionally `MB_OAUTH_REDIRECT_URI`, then run
`musicbrainz-auth authorize`. OAuth requests only `tag`, `rating`, and `profile`
permissions. Tokens are never passed in command arguments or written to reports.
`MB_TOKEN` remains a deprecated Digest-token fallback; `MB_PASSWORD` is its legacy
alias. Do not store the MusicBrainz website password.
## Arr-scripts integration

Arr-scripts installs a pinned release and calls the same `musicbrainz-genres` and `musicbrainz-ratings` commands used manually. Event callbacks enqueue work; background workers make the remote requests. Existing queues and account-specific submission history remain in `/config/extended/`.

See [genre setup](https://github.com/SickProdigy/arr-scripts/blob/main/lidarr/musicbrainz-genres.md) and [ratings setup](https://github.com/SickProdigy/arr-scripts/blob/main/lidarr/readme.md#ratings-automation) for container settings and wrapper commands. Arr-scripts also installs SonicRatings independently to fill unrated Subsonic songs; `enableRatings` controls those writes, while `enableMusicBrainzHelper` controls contributions. The Apple Music, iTunes, Last.fm genre, and Navidrome genre tools remain available as manual commands; they are not automatically scheduled by these hooks.

The [ratings worker](musicbrainz_helper/ratings_worker.py) handles queue and history operations behind `musicbrainz-ratings`. The [shared utilities](musicbrainz_helper/musicbrainz.py) handle MusicBrainz requests, caching, and genre submissions. Command definitions live in [pyproject.toml](pyproject.toml).

## Optional ratings from Last.fm

The normal ratings command reads existing Subsonic ratings and preserves its artist, album, and song options. To contribute ratings for unrated recordings, the background/library adapter also supports an independent Last.fm source. Existing personal ratings take priority. No computed rating is written to Subsonic; the vote is sent to MusicBrainz, where SonicRatings can later find it.

Configure `.env` with your Subsonic/Navidrome credentials, authorized MusicBrainz
OAuth settings, and `LASTFM_API_KEY`. With `RATINGS_SUBMIT` unset or false, preview
a library contribution pass, then explicitly submit:

```bash
musicbrainz-ratings library --state-dir logs/ratings-helper-state --rating-source lastfm
musicbrainz-ratings library --state-dir logs/ratings-helper-state --rating-source lastfm --submit
```

This source derives recording stars from Last.fm artist rank, listeners, and replay engagement, checking artist/title/version and returned recording IDs. It requires an existing matched MusicBrainz recording; creating missing catalog entries remains the new-artist/editor-seed workflow. iTunes rating/genre contributions and the other genre sources retain their existing options above.

Arr-scripts calls the same entry point with `enqueue`, `work`, and `library`. Its default `ratingsSource="subsonic"` observes existing personal stars; `ratingsSource="lastfm"` also contributes Last.fm ratings for unrated recordings. Failed source requests retain retry inputs; successful MusicBrainz votes are tracked per account. Existing state paths and submission history remain compatible, and old pending Subsonic-write jobs are not resumed.

A caller may supply generated-rating provenance with `RATINGS_GENERATED_RATINGS=/path/to/generated-ratings.json`, or `RATINGS_GENERATED_RATINGS_DIR=/path/to/scoped-state`. The worker checks the song and recording identity before using a source label. MusicBrainz-derived values stay excluded; later personal changes remain eligible. Arr-scripts passes this path automatically without adding a SonicRatings dependency to this package.

Package version is **0.4.0**. Publish its `v0.4.0` tag before deploying the corresponding arr-scripts pin. Standalone helper use requires only this package; the combined arr-scripts installer also pins SonicRatings `v1.4.0`.

Run checks with `python -B -m unittest discover -s tests -p 'test_*.py'`.
