# Spotify Librarian

Sorts your Spotify library by BPM and genre, and shows genre/BPM/artist trends across it — a librarian for
your collection, not just a sorter. Split into four files that stay in the same folder:

| File | What it does |
|---|---|
| `SpotifyCore.py` | Shared engine — CONFIG, cache, BPM/genre cascades. Not run directly. |
| `SpotifySortPlaylist.py` | Sorts your library: where each track should go, misplaced tracks, duplicates. |
| `SpotifyLibraryAnalysis.py` | Genre/BPM/artist trends, optionally by year, plus a full library backup CSV. |
| `SpotifyLibrarian.py` | One entry point with a menu — run this if you don't want to remember which script does what. |

A plain run of `SpotifySortPlaylist.py` is **read-only**: it only ever reads your library
(`playlist-read-private`, `playlist-read-collaborative`, and optionally `user-library-read` for Liked
Songs) and produces a report — no track is added, removed, or moved. Actually changing your library is a
deliberate second step (`--apply`, see "Applying the changes" below), gated behind your review and a
single explicit confirmation. `SpotifyLibraryAnalysis.py` is *always* read-only — there is no `--apply`
for it, nothing to approve.

## What it produces

`SpotifySortPlaylist.py` writes a console report, four CSV files, and `report.json` (for the review
interface — see "Applying the changes" below), all in `rapport_spotify/`:

| # | Report | Content |
|---|--------|---------|
| 1 | `1_suggested_additions.csv` | For each track in your source playlist: which "XX bpm" playlist and which genre playlist it belongs to, and whether it's already there |
| 2 | `2_misplaced.csv` | Tracks already in a playlist whose measured BPM or detected genre disagrees with it |
| 3 | `3_duplicates.csv` | Same track appearing more than once in a playlist (exact match or same name+artist) — checked in every BPM/genre playlist, your source playlist, and (optionally) every other playlist you own |
| 4 | `4_playlists_to_create.csv` | BPM or genre playlists that don't exist yet, with the tracks waiting for them |

`SpotifyLibraryAnalysis.py` writes two more, covering your *whole* analysed library rather than just the
sort queue:

| # | Report | Content |
|---|--------|---------|
| 5 | `5_library_backup.csv` | Every analysed track — title, artists, album, a direct Spotify link, measured BPM/genre, and every playlist it's in — your own copy, independent of Spotify (`EXPORT_LIBRARY_BACKUP`, on by default) |
| — | `analysis.json` | For `analysis.html` — genre/BPM/artist trends, see "Library analysis" below |

Every genre verdict states its source (`Last.fm (track)`, `Last.fm (artist)`, `iTunes (track)`, `Spotify
(artist)`) so you can judge how much to trust it.

Artist-follow suggestions (`AUTO_FOLLOW_ARTISTS`) live only in `report.json` and the review interface, not
in any of the CSVs above.

## Why this is more complicated than it sounds

Spotify closed its own BPM endpoint (`/audio-features`) to every app created after November 2024, and a
February 2026 API migration removed several other endpoints (batch artist lookups, recommendations, related
artists). Spotify also has **no per-track genre** — only a rough per-artist one. So the script rebuilds both
from external, keyless sources:

- **BPM**: Spotify (only works for apps created before Nov 2024) → **ReccoBeats** (by Spotify ID) →
  **Deezer** (by artist + title — catches remasters and local files that ReccoBeats' ID lookup misses) →
  optionally, measuring the tempo *ourselves* from 30 s Deezer previews with `librosa`.
- **Genre**: **Last.fm** tags of the track itself (most precise) → Last.fm tags of the main artist (for
  obscure tracks with no track-level tags) → **iTunes Search** (coarse but broad coverage) → Spotify artist
  genres (off by default — see below).

A disk cache (`cache_spotify_tri.json`) remembers everything ever fetched — playlists (invalidated only when
they actually change), BPMs, genre tags, and local-file matches — so **a run that gets interrupted resumes
exactly where it stopped**, and nothing is ever paid for (in API calls or time) twice.

**Do not delete the cache file.** It is the project's memory; deleting it means re-fetching everything from
scratch, which can take an hour or more on a large library.

## Requirements

- Python 3.9+
- A Spotify app: create one at <https://developer.spotify.com/dashboard>, with Redirect URI
  `http://127.0.0.1:8888/callback`. A plain run only ever asks for read access; `--apply` additionally asks
  for permission to create/edit playlists the first time you use it (your browser reopens once for that).
  If `AUTO_FOLLOW_ARTISTS` is on, the same pattern applies to following artists: read access for a plain
  run (to know who you already follow), write access only for `--apply`.
- A free Last.fm API key (optional but recommended): <https://www.last.fm/api/account/create>
- Third-party Python packages (`spotipy`, `requests`, and optionally `truststore` / `librosa`) install
  **automatically** on first run — nothing to install by hand.

## Setup

Open **`SpotifyCore.py`** and fill in the **CONFIG** block at the top — one shared place for both
`SpotifySortPlaylist.py` and `SpotifyLibraryAnalysis.py`, regardless of which you actually run. It's
organised in three zones, from must-edit to leave-alone:

### Zone 1 — Fill these (the script won't start without them)

```python
SPOTIFY_CLIENT_ID     = "..."   # from your app's dashboard
SPOTIFY_CLIENT_SECRET = "..."   # from your app's dashboard ("View client secret")
SOURCE_PLAYLIST_ID    = "..."   # the playlist to sort: right-click it > Share > Copy link,
                                 # keep the 22 characters between /playlist/ and ?si=
LASTFM_API_KEY        = "..."   # "" disables Last.fm (genre quality drops noticeably)
```

If a value is missing or the wrong shape, whichever script you run tells you exactly which field is wrong
and how to fix it — it won't send you into a confusing Spotify login error.

### Zone 2 — Check these (depends on your situation)

| Setting | What it's for |
|---|---|
| `PROXY_URL` | Set to your corporate proxy at the office, `""` at home |
| `USE_SYSTEM_CERTS` | Keep `True` behind a corporate SSL-inspecting proxy |
| `INCLUDE_LOCAL_FILES` | Also analyse imported MP3s (sorted by their name/artist tags) |
| `MATCH_LOCAL_FILES` | Search the Spotify catalog for each local file's equivalent, so it becomes fully actionable |
| `MAX_LOCAL_MATCH_PER_RUN` | Spotify allows roughly 750 calls/day; `0` = no cap, do it all in one run |
| `INCLUDE_LIKED_SONGS` | Also sort your Liked Songs library (adds the `user-library-read` scope) |
| `CHECK_ALL_PLAYLISTS_FOR_DUPLICATES` | Also check every OTHER playlist you own (not just BPM/genre ones) for duplicates |
| `AUTO_OPEN_REVIEW` | Open `review_interface.html` automatically at the end of a run, report already loaded |
| `AUTO_FOLLOW_ARTISTS` | Also suggest (via the review interface) following every artist in your library you don't follow yet |
| `EXPORT_LIBRARY_BACKUP` | Write `5_library_backup.csv` — every analysed track, independent of Spotify (on by default) |
| `ANALYZE_YEARLY_PLAYLISTS` | Detect your own "top songs of the year" playlists and add a by-year trend to `analysis.html` |
| `ANALYZE_DEEZER_PREVIEWS` | Last-resort BPM: download 30 s previews and measure the tempo locally |
| `TEST_EXTERNES` | Paste track links to test ReccoBeats/Last.fm without touching your Spotify quota |

### Zone 3 — Fine as-is

Everything else: the genre keyword rules, exact-tag pins (`EXPLICIT_GENRE_MAP`), sibling-genre pairs that
shouldn't flag each other (`NEIGHBOR_GENRES`), playlists exempt from the misplaced-genre audit, API URLs,
cache/output paths. Only touch these to change how the classifier *thinks*.

## Running it

```bash
python SpotifySortPlaylist.py          # sort: analyse (read-only)
python SpotifyLibraryAnalysis.py       # analysis: trends + backup, always read-only
python SpotifyLibrarian.py                 # menu: pick one interactively, or "sort"/"analysis" as an argument
```

Any of the three works — `SpotifyLibrarian.py` is purely a convenience front door if you don't want to
remember which script does what. All three also work packaged into a single `.exe` if you'd rather
double-click than type a command — see "Running it as a standalone .exe" below.

The first run authenticates through your browser (one-time), then reads your playlists and starts fetching
BPM/genre data for everything that isn't cached yet. On a library of a few thousand tracks, expect the
**first full run to take up to an hour** (mostly spent politely pacing calls to Last.fm/Deezer/iTunes, plus
the local-file matching if enabled). Every later run is fast — usually seconds to a few minutes — because
almost everything comes from the cache.

If the daily Spotify quota runs out mid-run, the script tells you exactly when Spotify says to retry, saves
its progress, and exits cleanly. Just run it again after that time; it picks up where it left off. A short
rate-limit (Spotify asking you to wait a few seconds, not the daily wall) is retried automatically and never
interrupts the run. Liked Songs, the all-playlist duplicate scan, and local-file matching are all resilient
to running out of quota mid-way: they stop cleanly with whatever they got, and the rest of the analysis
(BPM/genre, both non-Spotify) still completes and still produces a full report.

Every BPM-based add or move is cross-checked against a second, independent source before being trusted (see
"Applying the changes" below) — a real cost the first time a track is seen (one extra Deezer lookup), but
free on every run after, since the verdict is cached per track. On a large, never-before-analysed library
expect this to noticeably add to that first-run hour; it has no effect on later runs beyond genuinely new
tracks.

## How genre classification works

Each raw tag (from Last.fm, iTunes, etc.) casts **one vote** for a category:

1. If the tag is listed in `EXPLICIT_GENRE_MAP`, that pin wins outright.
2. Otherwise, among your `GENRE_RULES` keywords that match the tag, the **longest** one wins (so "electro
   swing" votes Electro, not Jazz, despite containing "swing").
3. The category with the most votes across all of a track's tags wins; ties are broken by the order of
   `GENRE_RULES`.

This means a track tagged `["electronic", "house", "trap"]` votes 2-1 for Electro over Rap — a single
ambiguous tag can no longer overrule a clear majority.

If you spot a genre you disagree with, the cheapest fix is almost always an `EXPLICIT_GENRE_MAP` entry —
it takes effect on every future run at no extra API cost. You don't have to edit the CONFIG by hand for
this: tags that matched nothing show up in the review interface, grouped by the track they came from (a
track can carry several unmapped tags at once — they show as one block, not one disconnected row per tag),
each with a dropdown to pin it to a category. Export your decisions as usual; the pin takes effect starting
with your very next analysis, not just the following `--apply`.

Two categories exist specifically as deliberate, un-audited catch-alls (like Pop): **Instrumental** (tag
`instrumental` — a performance style, not a genre, so it only wins when nothing more specific also matched)
and **Inclassable**, which is different from the rest: it's not voted for by any tag. A track lands there
only when Last.fm (track and artist), iTunes, *and* Spotify's own artist genres all came back with nothing
usable — rather than silently vanishing with no actionable trace, it gets a real, reviewable add-to-playlist
action, so you can place it by ear at your leisure instead of it being lost in a CSV footnote.

## Local files (imported MP3s)

Shared by both tools (part of `SpotifyCore.py`'s `gather_real_data`), so a local file is just as sortable
and just as included in the library backup/trends as a catalog track. Local files have no Spotify ID, so
the official BPM/audio endpoints can't see them — but their name and artist tags let the whole name-based
cascade (Last.fm, Deezer, iTunes) work anyway. With `MATCH_LOCAL_FILES` on, the script also searches the
Spotify catalog for each one's equivalent, trying up to three query shapes per file (normal fields, swapped
fields for inverted MP3 tags, then free text) — a match makes the track fully actionable for a future
"apply changes" step. Matches and misses are both cached, so this search is only ever paid once per file.

## Artist auto-follow (optional)

Part of `SpotifySortPlaylist.py`. With `AUTO_FOLLOW_ARTISTS` on, the analysis also looks across your whole
library (source playlist, Liked Songs, and every other playlist checked) for artists you don't already
follow, and adds them to the review interface as a normal group — approve individually or in bulk, same as
everything else. Nothing is followed automatically; it's suggested like any other change and only takes
effect through `--apply`.

This uses the unified library endpoints Spotify introduced in February 2026
(`GET`/`PUT /me/library` with `spotify:artist:{id}` URIs) — the older, entity-specific follow endpoints this
kind of feature would normally use were removed that same month for apps in Development Mode.

## Library analysis (optional)

`python SpotifyLibraryAnalysis.py` writes `analysis.json`. Open `analysis.html` (same folder, no server or
install needed) and load it to see:

- **Overview** — your top artists, genre distribution, and tempo distribution across the analysed library,
  plus a genre/sub-genre map (each category sized by your real track count; the thin branches are the
  keywords the script recognises for that category, not a measured per-keyword count).
- **By year** — with `ANALYZE_YEARLY_PLAYLISTS` on, a genre-mix trend across your own "top songs of the
  year" playlists, plus average tempo per year.

Detecting those yearly playlists isn't as simple as looking for ones you own: Spotify's own "Your Top Songs
2023"-style playlists are algorithmic and typically owned by Spotify itself, not you, even once you've
followed them into your library — and ownership alone can't tell one apart from an unrelated Spotify
editorial playlist that also happens to mention a year (e.g. a generic "Hits 2010" compilation). What does
reliably set them apart: Spotify always writes a description that names you personally ("Conçue pour
\<your name\>", "Made for \<your name\>", whatever the phrasing in your language) — a generic editorial
playlist's description never does. A playlist you made yourself that happens to mention a year is still
picked up if you own it, as a fallback.

## Running it as a standalone .exe (optional)

If double-clicking a `.py` file isn't convenient on your machine, the whole project can be packaged into a
single `SpotifyLibrarian.exe` with PyInstaller — no Python install required to run it afterwards. Must be done
on a Windows machine (PyInstaller builds for whatever OS it runs on). All four `.py` files need to stay in
the same folder, whether running with `python` or compiling.

**Important before compiling**: fill in `SpotifyCore.py`'s CONFIG with your **real** values, not a version
with `XXX` placeholders — the config is baked into the exe at compile time and can't be changed afterwards
(short of recompiling).

### One-time setup

```
python -m pip install pyinstaller
```

If `pip` alone isn't recognised (common on a machine where only `python` is on the PATH), use the
`python -m pip` form above — never bare `pip` in that case. Behind a corporate proxy, append
`--proxy http://your.proxy:3128` if it fails.

### Building (or rebuilding, after updating a script)

From the folder containing the 4 `.py` files:

```
python -m PyInstaller --onefile --name SpotifyLibrarian --console --hidden-import=truststore --hidden-import=librosa --distpath . SpotifyLibrarian.py
```

(`python -m PyInstaller` rather than bare `pyinstaller`, same PATH reason as `pip` above.)

- `--onefile`: a single `.exe`, not a folder with 50 files.
- `--console`: **important** — keeps the console window visible, where the menu and `yes`/`no`
  confirmations show up. Without it, those messages are invisible.
- `--hidden-import=truststore --hidden-import=librosa`: **necessary**. Both packages are loaded on demand
  (`importlib.import_module`) inside `SpotifyCore.py`, not via a normal top-of-file `import` — PyInstaller
  can't detect them on its own and would silently leave them out. Without this flag, `truststore`
  (corporate proxy) and `librosa` (preview-based BPM measurement) would be missing from the exe; the script
  still runs, just without those two capabilities. Drop `--hidden-import=librosa` if you never use
  `ANALYZE_DEEZER_PREVIEWS`.
- `--distpath .`: puts the finished exe directly in the current folder. Without it, PyInstaller leaves it
  in a `dist\` subfolder you'd have to go fetch every time.

Unlike `truststore`/`librosa`, **`SpotifyCore.py`, `SpotifySortPlaylist.py`, and `SpotifyLibraryAnalysis.py`
need no `--hidden-import`**: they're plain imports (`import SpotifySortPlaylist` at the top of
`SpotifyLibrarian.py`) that PyInstaller detects and bundles on its own by analysing the code — as long as all
four files sit in the same folder at compile time.

A `build\` folder and a `.spec` file also appear alongside — build artefacts with no further use, safe to
ignore or delete.

Copy the exe, with `review_interface.html` **and** `analysis.html` next to it, into whichever folder you
want the cache and reports to live in (your usual `Downloads\Spotify` folder, for instance).

### Launching it

Double-clicking `SpotifyLibrarian.exe` on its own shows a menu:
```
What do you want to do?
  1) Sort my library (analyse - read-only)
  2) Sort my library (--apply - writes your approved changes)
  3) Analyse my library (genres, tempo, trends, backup)
  4) Quit
```
Type the number and Enter — no shortcut juggling or opening a command prompt needed to choose between
analysing and applying, or between sorting and analysing.

For scripting/automation without the menu, command-line arguments work too:
```
SpotifyLibrarian.exe sort
SpotifyLibrarian.exe sort --apply
SpotifyLibrarian.exe analysis
```

The cache (`cache_spotify_tri.json`), the login token (`.spotify_token_cache`), and the report folder
(`rapport_spotify\`) are created automatically in that same folder, exactly as before.

### The window doesn't close on its own anymore

Whether the run succeeds, hits a config error, or crashes unexpectedly, the exe now waits for a keypress
before closing its window — Windows otherwise closes a double-clicked exe's console the instant the process
ends, taking the message with it, success or failure alike. A plain `python SpotifyLibrarian.py` (or either
script on its own) from an already-open terminal never has this problem (the terminal owns that window,
not the script) and isn't affected by this pause.

If you'd still rather have a separate launcher (say, to automate several steps), a `.bat` next to the exe
still works:
```bat
@echo off
SpotifyLibrarian.exe sort --apply
pause
```

### The first launch will be slower

PyInstaller bundles Python and every dependency into the exe — the very first startup (extracting to a
temp folder) takes a few seconds longer than `python SpotifyLibrarian.py`. Later launches are the same either
way.

### Antivirus

PyInstaller executables sometimes trigger a false positive on first launch (a known pattern related to how
PyInstaller packages code, not specific to this script). If Windows Defender or a corporate antivirus
blocks the file, you'll likely need to explicitly allow it — worth checking with IT whether company policy
permits that before relying on it day to day.

## Applying the changes (phase 2)

A plain run is always read-only. To actually change your library, there are three steps:

1. **Analyse** — `python SpotifySortPlaylist.py` as usual. Besides the CSVs, it now also writes
   `rapport_spotify/report.json`: every proposed change, split into **confident** (safe measurements/matches,
   no call needed) and **needs your call**, grouped by *why* it's uncertain (genre guessed from the artist
   only, a fuzzy local-file match, a close genre vote, a possible real duplicate, a BPM that a second,
   independent lookup didn't confirm, etc.). No BPM-based add or move — including the very first time a
   track is filed into a bucket — is ever trusted on a single source's word alone: a second, independent
   lookup (Deezer, by name) has to agree, or it goes to "needs your call" instead. That verdict is cached
   per track, so it stays the same between this report and the later `--apply` even if that second lookup
   would answer differently by then — and it's only ever paid once per track, not once per run.
2. **Review** — `review_interface.html` opens automatically with this run's report already loaded (set
   `AUTO_OPEN_REVIEW = False` in the CONFIG to disable this and load it manually instead: double-click
   `review_interface.html`, no server, no install, and click "Load report.json"). Every group shows its
   **full** track list — not just a preview — in a scrollable, filterable, sortable panel (by title, artist,
   or destination). Checkboxes are literal: checked means that track will be included, full stop. "Approve
   all" / "Leave out" just check or uncheck everything in the group at once; you can still flip any
   individual track afterwards. A small VU-meter on each group fills up live as you include its tracks, so
   you can see your progress at a glance. Decide which not-yet-existing playlists (140 bpm, SoundTrack...)
   are worth creating. Click "Export decisions" — this downloads `decisions.json` to your Downloads folder.
3. **Apply** — `python SpotifySortPlaylist.py --apply` (or `python SpotifyLibrarian.py sort --apply`). It
   re-checks your library fresh (fast, thanks to the cache), applies your decisions, and shows a **full
   preview** of every playlist to create, track to add, move, or remove — grouped and counted. Nothing is
   written until you type `yes` at the single confirmation prompt that follows.

Before the preview, `--apply` also tells you how long ago the decisions were exported and, if a newer
`report.json` has been generated since (say you re-ran the analysis after exporting), flags it clearly.
This is a heads-up, not a block — your approvals are matched by group and track id, not a frozen snapshot,
so they still apply correctly either way; it's just a prompt to re-review if enough changed since.

Why re-check fresh instead of trusting the earlier report? So the tool is naturally resilient: if `--apply`
gets interrupted (daily quota, closed terminal), just running it again picks up exactly where it left off —
already-applied changes are recognised as done and skipped, nothing is ever double-applied.

**A few things worth knowing about apply mode:**
- It only ever *adds*, *creates*, or *removes exact duplicate/misplaced entries* — it never deletes a
  playlist, and it never touches a track that isn't part of an action you approved.
- Local files without a matched catalog equivalent still can't be acted on directly (the API has no way to
  add/move a raw MP3) — they stay a manual, drag-and-drop job.
- Exercised against the real API over multiple full runs (playlist creation, adds, moves, duplicate removal,
  artist-follow) — not just simulated. One real quirk turned up and got fixed along the way: the artist-follow
  endpoint (`PUT /me/library`) rejects the identifiers when sent as a JSON body, even though Spotify's own
  migration guide shows exactly that — they have to go in the query string instead (see Troubleshooting).
  For your very first `--apply` on a new setup, approving something small first (the exact-ID duplicates are
  the lowest-risk starting point) is still a reasonable way to build confidence before a full run.

In the review interface, two things that look like duplicated rows at a glance are intentional: a track
needing both a BPM and a genre decision under the same uncertainty reason shows as two lines, each labelled
`(bpm)` / `(genre)`; a track with 3+ copies in one playlist shows one line per extra copy, each saying how
many copies exist in total. Review groups are also sorted by category first (genre, then BPM, then
duplicates, then local-file matches, then artist-follows), by size within each — not just by raw size across
everything, which used to interleave unrelated categories.

## Troubleshooting

- **`client_id: Invalid` in the browser** — check your app still exists on the dashboard (Spotify prunes
  inactive/quota-exhausted apps); compare the ID in the browser URL with the dashboard, character for
  character.
- **Config errors on startup** — the script lists every problem with the exact field and a step-by-step fix;
  read it before checking anything else.
- **`BadStatusLine` / flaky network errors** — usually a corporate proxy hiccup; the script retries
  automatically. If it persists, check `PROXY_URL` and try refreshing a page in your browser first (some
  proxies need an active session).
- **`Insufficient client scope` on `--apply`** — a saved login from before you first used `--apply` doesn't
  cover playlist creation/editing yet. The script now detects this itself and deletes the stale login so
  Spotify re-asks (your browser reopens) — if it still happens, delete `.spotify_token_cache` by hand and
  run again.
- **Deezer (or ReccoBeats) consistently returns 0 results** — usually a proxy/certificate issue rather than
  Deezer itself being down: test a single well-known query (e.g. "Bohemian Rhapsody") outside the main
  script with your `PROXY_URL` and `truststore` set up the same way, to see the raw response or the real
  underlying error.
- **Quota exhausted** — the exit message tells you exactly when to retry (from Spotify's own `Retry-After`
  value when available). Just wait and re-run; nothing is lost.
- **Artist-follow calls fail even though `AUTO_FOLLOW_ARTISTS` is on** — Spotify removed the old
  entity-specific follow endpoints for Development Mode apps in February 2026; this script already targets
  their replacement (`/me/library`) directly rather than through spotipy's now-outdated built-in methods.
  If you see `Missing required field: uris` specifically, that's this endpoint rejecting identifiers sent as
  a JSON body — they need to be a comma-separated query parameter instead (already how this script sends
  them; a long-documented quirk on this endpoint's predecessors, `/me/tracks` and `/me/albums`, too).
- **"ReccoBeats: N to try" is smaller than the "still to fetch" count right after** — expected, not a
  miscount: ReccoBeats can only look up tracks with a real Spotify ID, so local files are excluded from "to
  try" but still counted in "still to fetch" once the stage moves on. The console line says how many were
  skipped for this reason.
