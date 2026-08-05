# Spotify Playlist Sorter

A Python script that analyses your Spotify playlists and tells you, for every track, which BPM playlist
and which genre playlist it should go to — without ever changing anything on your account.

It's a **dry-run analysis tool**: it only ever reads your library (`playlist-read-private`,
`playlist-read-collaborative`, and optionally `user-library-read` for Liked Songs) and produces a report.
No track is added, removed, or moved anywhere.

## What it produces

A console report plus four CSV files in `rapport_spotify/`:

| # | Report | Content |
|---|--------|---------|
| 1 | `1_suggested_additions.csv` | For each track in your source playlist: which "XX bpm" playlist and which genre playlist it belongs to, and whether it's already there |
| 2 | `2_misplaced.csv` | Tracks already in a playlist whose measured BPM or detected genre disagrees with it |
| 3 | `3_duplicates.csv` | Same track appearing more than once in a playlist (exact match or same name+artist) |
| 4 | `4_playlists_to_create.csv` | BPM or genre playlists that don't exist yet, with the tracks waiting for them |

Every genre verdict states its source (`Last.fm (track)`, `Last.fm (artist)`, `iTunes (track)`, `Spotify
(artist)`) so you can judge how much to trust it.

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
  `http://127.0.0.1:8888/callback`
- A free Last.fm API key (optional but recommended): <https://www.last.fm/api/account/create>
- Third-party Python packages (`spotipy`, `requests`, and optionally `truststore` / `librosa`) install
  **automatically** on first run — nothing to install by hand.

## Setup

Open the script and fill in the **CONFIG** block at the top. It's organised in three zones, from
must-edit to leave-alone:

### Zone 1 — Fill these (the script won't start without them)

```python
SPOTIFY_CLIENT_ID     = "..."   # from your app's dashboard
SPOTIFY_CLIENT_SECRET = "..."   # from your app's dashboard ("View client secret")
SOURCE_PLAYLIST_ID    = "..."   # the playlist to sort: right-click it > Share > Copy link,
                                 # keep the 22 characters between /playlist/ and ?si=
LASTFM_API_KEY        = "..."   # "" disables Last.fm (genre quality drops noticeably)
```

If a value is missing or the wrong shape, the script tells you exactly which field is wrong and how to fix
it — it won't send you into a confusing Spotify login error.

### Zone 2 — Check these (depends on your situation)

| Setting | What it's for |
|---|---|
| `PROXY_URL` | Set to your corporate proxy at the office, `""` at home |
| `USE_SYSTEM_CERTS` | Keep `True` behind a corporate SSL-inspecting proxy |
| `INCLUDE_LOCAL_FILES` | Also analyse imported MP3s (sorted by their name/artist tags) |
| `MATCH_LOCAL_FILES` | Search the Spotify catalog for each local file's equivalent, so it becomes fully actionable |
| `MAX_LOCAL_MATCH_PER_RUN` | Spotify allows roughly 750 calls/day; `0` = no cap, do it all in one run |
| `INCLUDE_LIKED_SONGS` | Also sort your Liked Songs library (adds the `user-library-read` scope) |
| `ANALYZE_DEEZER_PREVIEWS` | Last-resort BPM: download 30 s previews and measure the tempo locally |
| `TEST_EXTERNES` | Paste track links to test ReccoBeats/Last.fm without touching your Spotify quota |

### Zone 3 — Fine as-is

Everything else: the genre keyword rules, exact-tag pins (`EXPLICIT_GENRE_MAP`), sibling-genre pairs that
shouldn't flag each other (`NEIGHBOR_GENRES`), playlists exempt from the misplaced-genre audit, API URLs,
cache/output paths. Only touch these to change how the classifier *thinks*.

## Running it

```bash
python spotify_tri_playlists.py
```

The first run authenticates through your browser (one-time), then reads your playlists and starts fetching
BPM/genre data for everything that isn't cached yet. On a library of a few thousand tracks, expect the
**first full run to take up to an hour** (mostly spent politely pacing calls to Last.fm/Deezer/iTunes, plus
the local-file matching if enabled). Every later run is fast — usually seconds to a few minutes — because
almost everything comes from the cache.

If the daily Spotify quota runs out mid-run, the script tells you exactly when Spotify says to retry, saves
its progress, and exits cleanly. Just run it again after that time; it picks up where it left off.

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
it takes effect on every future run at no extra API cost.

## Local files (imported MP3s)

Local files have no Spotify ID, so the official BPM/audio endpoints can't see them — but their name and
artist tags let the whole name-based cascade (Last.fm, Deezer, iTunes) work anyway. With `MATCH_LOCAL_FILES`
on, the script also searches the Spotify catalog for each one's equivalent, trying up to three query shapes
per file (normal fields, swapped fields for inverted MP3 tags, then free text) — a match makes the track
fully actionable for a future "apply changes" step. Matches and misses are both cached, so this search is
only ever paid once per file.

## Applying the changes (phase 2)

A plain run is always read-only. To actually change your library, there are three steps:

1. **Analyse** — `python spotify_tri_playlists.py` as usual. Besides the CSVs, it now also writes
   `rapport_spotify/report.json`: every proposed change, split into **confident** (safe measurements/matches,
   no call needed) and **needs your call**, grouped by *why* it's uncertain (genre guessed from the artist
   only, a fuzzy local-file match, a close genre vote, a possible real duplicate, etc.).
2. **Review** — open `review_interface.html` (double-click, no server, no install) and click "Load
   report.json". Approve or skip each group in bulk, with the option to except a few individual tracks.
   Decide which not-yet-existing playlists (140 bpm, SoundTrack...) are worth creating. Click "Export
   decisions" — this downloads `decisions.json` to your Downloads folder.
3. **Apply** — `python spotify_tri_playlists.py --apply`. It re-checks your library fresh (fast, thanks to
   the cache), applies your decisions, and shows a **full preview** of every playlist to create, track to
   add, move, or remove — grouped and counted. Nothing is written until you type `yes` at the single
   confirmation prompt that follows.

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
- The review interface only ships a handful of sample tracks per group (to stay lightweight); a group
  decision applies to *every* track in it, with exceptions limited to the tracks you can see and un-tick.
- This write path is thoroughly unit-tested against a simulated Spotify client, but — unlike the read-only
  analysis, exercised for real over many runs — it has not been exercised against the live API. For your
  first `--apply`, consider approving something small first (the exact-ID duplicates are the lowest-risk
  starting point) before doing a full run.

## Troubleshooting

- **`client_id: Invalid` in the browser** — check your app still exists on the dashboard (Spotify prunes
  inactive/quota-exhausted apps); compare the ID in the browser URL with the dashboard, character for
  character.
- **Config errors on startup** — the script lists every problem with the exact field and a step-by-step fix;
  read it before checking anything else.
- **`BadStatusLine` / flaky network errors** — usually a corporate proxy hiccup; the script retries
  automatically. If it persists, check `PROXY_URL` and try refreshing a page in your browser first (some
  proxies need an active session).
- **Quota exhausted** — the exit message tells you exactly when to retry (from Spotify's own `Retry-After`
  value when available). Just wait and re-run; nothing is lost.
