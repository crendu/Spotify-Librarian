#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WHAT THIS SCRIPT DOES (read-only: it never modifies your Spotify account)
  Reads your playlists, then prints + exports 4 CSV reports:
    1. where each track of the source playlist should go (one "XX bpm" playlist + one genre playlist)
    2. tracks that look misplaced in your existing playlists
    3. duplicates inside each playlist
    4. playlists worth creating (with the tracks waiting for them)

HOW TO RUN
    1. Have Python 3. Missing libraries install themselves on first run.
    2. Fill the CONFIG block below (the lines marked with an alert comment).
    3. python spotify_tri_playlists.py   (first full run takes ~1 h; later runs are fast thanks to the cache)

GOOD TO KNOW
  - BPM comes from ReccoBeats, then Deezer (Spotify closed its own endpoint to new apps).
  - Genre comes from Last.fm tags (track, then artist), then iTunes. Every verdict states its source.
  - Everything fetched is saved in a local cache file: an interrupted run resumes where it stopped,
    and nothing is ever downloaded twice. Do not delete the cache file.
"""

import csv
import json
import os
import re
import sys
import logging
import time
from collections import defaultdict

import requests
import spotipy
from spotipy.oauth2 import SpotifyOAuth

logging.getLogger("spotipy").setLevel(logging.CRITICAL)

def _ensure_package(module, pip_name=None):
    """Imports a package; if missing, installs it with pip first. Returns the module, or None when the install failed - the caller then runs without that feature."""
    import importlib
    import subprocess
    try:
        return importlib.import_module(module)
    except ImportError:
        pkg = pip_name or module
        print(f"INFO: python package '{pkg}' missing -> installing it now ({sys.executable} -m pip install {pkg})")
        for extra in ([], ["--user"]):  # plain install first, then per-user (machines without admin rights)
            try:
                subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", *extra, pkg])
                return importlib.import_module(module)
            except (subprocess.CalledProcessError, OSError):
                continue
            except ImportError:
                break
        print(f"! automatic install of '{pkg}' failed -> install it manually: "
              f"{sys.executable} -m pip install {pkg}", file=sys.stderr)
        return None

# ======================================================================================================================
# CONFIG - three zones, from "must edit" to "leave as-is":
#   ZONE 1 - FILL THESE  : the script cannot work without them.
#   ZONE 2 - CHECK THESE : depends on where and how you run (office/home, local files, quota, options).
#   ZONE 3 - FINE AS-IS  : behaviour tuning; only touch to change how the sorting THINKS.
# ======================================================================================================================

# ----------------------------------------------------------------------------------------------------------------------
# ZONE 1 - FILL THESE (Create the app on https://developer.spotify.com/dashboard, Redirect URI below; free Last.fm key on https://www.last.fm/api/account/create)
# ----------------------------------------------------------------------------------------------------------------------
SPOTIFY_CLIENT_ID     = "XXX" # /!\
SPOTIFY_CLIENT_SECRET = "XXX" # /!\
SOURCE_PLAYLIST_ID    = "XXX" # /!\ - the playlist to sort (22 chars from its link)
LASTFM_API_KEY        = "XXX" # /!\ - main genre source; "" = run without it

# ----------------------------------------------------------------------------------------------------------------------
# ZONE 2 - CHECK THESE (your environment and what this run should do)
# ----------------------------------------------------------------------------------------------------------------------
PROXY_URL = ""                  # /!\ - office proxy; set "" when running from home
USE_SYSTEM_CERTS = True         # keep True on a corporate Windows machine (SSL-inspecting proxy)

INCLUDE_LOCAL_FILES = True      # analyse the imported MP3s too (sorted by their name tags)
MATCH_LOCAL_FILES = True        # find their Spotify catalog equivalent (makes them actionable)
MAX_LOCAL_MATCH_PER_RUN = 700   # Spotify allows ~750 calls/day; 0 = no cap (one big run)
LOCAL_MATCH_PACE = 0.8          # seconds between two searches (Spotify punishes bursts)

INCLUDE_LIKED_SONGS = False     # also sort your "Liked Songs" library (adds ~1 call per 50 liked)

ANALYZE_DEEZER_PREVIEWS = False # measure missing BPMs from 30 s previews (slow; installs librosa)

# TEST MODE - paste track links here (right-click a track > Share > Copy link) to test the external APIs
# without touching Spotify. Non-empty list = run the test only, then stop. Results are cached.
TEST_EXTERNES = [
    # "https://open.spotify.com/intl-fr/track/5KjJYrM3UXmvhqtQntrsJM?si=e7311651acdd4323",
]
SPOTIFY_TRACK_FALLBACK = True               # in test mode: 1 Spotify call to identify a ReccoBeats-unknown track

# ----------------------------------------------------------------------------------------------------------------------
# ZONE 3 - FINE AS-IS (how the sorting thinks; change only to tune the logic)
# ----------------------------------------------------------------------------------------------------------------------
SPOTIFY_REDIRECT_URI = "http://127.0.0.1:8888/callback"     # must equal the Redirect URI in the app settings

BPM_STEP = 10                   # "80 bpm" bucket = 80.0 to 89.9 (truncated to the lower ten)
SHOW_HALF_DOUBLE_TEMPO = True   # a 154 bpm track can "feel" like 77: show the alternative bucket

# GENRE RULES - exact playlist names + lowercase keywords matched against the tags. One tag = one vote for the
# category with the LONGEST matching keyword; the most-voted category wins; this order breaks ties.
GENRE_RULES = [
    ("Latina",     ["latin", "reggaeton", "cumbia", "salsa", "bachata", "urbano", "corrido", "mariachi", "baile funk", "funk carioca", "flamenco", "tango", "merengue", "dembow"]),
    ("Classique",  ["classical", "baroque", "romantic era", "opera", "orchestr", "chamber music", "requiem", "symphony"]),
    ("Jazz",       ["jazz", "bebop", "swing", "bossa nova", "big band"]),
    ("Reggae",     ["reggae", "dancehall", "ska", "dub", "roots", "rocksteady", "rock steady"]),
    ("Rap",        ["rap", "hip hop", "hip-hop", "hiphop", "trap", "drill", "grime", "boom bap", "phonk"]),
    ("Soul",       ["soul", "r&b", "rnb", "r&b/soul", "funk", "motown", "gospel"]),
    ("Electro",    ["electro", "edm", "house", "techno", "tekno", "hardtek", "tribe", "trance", "dubstep", "drum and bass", "dnb", "jungle", "bass music", "synthwave", "big room", "uk garage", "future garage", "speed garage", "hardcore techno", "happy hardcore", "gabber", "frenchcore", "electro swing", "drill and bass", "lofi", "lo-fi", "ambient", "chillout", "downtempo", "trip hop", "trip-hop", "idm", "psytrance", "hardstyle"]),
    ("Dance",      ["dance pop", "dance", "disco", "eurodance", "hyperpop"]),
    ("Rock",       ["rock", "metal", "punk", "grunge", "emo", "screamo", "hardcore", "shoegaze", "garage", "blues", "new wave", "rock opera"]),
    ("SoundTrack", ["soundtrack", "film score", "game music", "video game music", "anime", "ost",
                    "bande originale", "film music", "theme song", "composer"]),
    ("FranÃ§aise",  ["french", "chanson", "variete francaise", "variÃ©tÃ© franÃ§aise", "francoton"]),
    ("Pop",        ["pop", "indie", "singer-songwriter", "synthpop", "britpop", "folk", "country", "americana", "baroque pop", "chamber pop"]),  # catch-all, keep last
]

# A tag written here goes straight to its category, before any keyword logic (perfect for the report's "unmapped tags" list, or to overrule a keyword decision).
EXPLICIT_GENRE_MAP = {
    # "ska punk": "Reggae",
}

# Only needed when a genre playlist's NAME differs from its category (Share > Copy link, keep the 22 chars).
GENRE_PLAYLIST_ID_OVERRIDES = {
    # "MyCategory": "0D8j9PayIN5ZSvDiwHkZiC",
}

# Sibling playlists that never flag each other as "misplaced" (their border is your taste, not an error).
NEIGHBOR_GENRES = [
    {"Dance", "Electro"},
    {"Pop", "Dance"},
    {"Pop", "Electro"},
    {"Soul", "Dance"},
]
# Playlists never audited for misplaced genres ("Pop" is your all-era hits catch-all: auditing it is noise).
MISPLACED_GENRE_EXEMPT = ["Pop"]
# Only flag a track as misplaced when the verdict comes from the TRACK's own tags (artist tags are too weak).
MISPLACED_GENRE_TRACK_ONLY = True

# Spotify artist genres as last genre fallback: one call PER ARTIST = quota killer, off by default.
USE_SPOTIFY_ARTIST_GENRES = False
MAX_ARTIST_LOOKUPS = 200

USE_DEEZER_BPM_FALLBACK = True      # BPM by artist+title when ReccoBeats does not know the exact id
USE_ITUNES_GENRE_FALLBACK = True    # coarse genre by artist+title when Last.fm knows nothing

OUTPUT_DIR = "rapport_spotify"          # folder for the report CSVs
CACHE_FILE = "cache_spotify_tri.json"   # the project's memory: do NOT delete it

DEEZER_URL = "https://api.deezer.com"
ITUNES_URL = "https://itunes.apple.com/search"
LASTFM_URL = "https://ws.audioscrobbler.com/2.0/"
RECCOBEATS_URL = "https://api.reccobeats.com/v1/audio-features"
LASTFM_MIN_TAG_COUNT = 10   # ignore overly marginal Last.fm tags
SCOPES = "playlist-read-private playlist-read-collaborative" + (" user-library-read" if INCLUDE_LIKED_SONGS else "")   # read-only in every case

# ======================================================================================================================
# END OF CONFIG - nothing to modify below
# ======================================================================================================================

GENRE_PLAYLIST_NAMES = [name for name, _ in GENRE_RULES]
_SPOTIFY_ID_RE = re.compile(r"[A-Za-z0-9]{22}")   # a Spotify track/playlist id is 22 base62 characters

# Env vars as fallback when the fields above are empty (avoids committing secrets).
SPOTIFY_CLIENT_ID     = SPOTIFY_CLIENT_ID or os.environ.get("SPOTIPY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = SPOTIFY_CLIENT_SECRET or os.environ.get("SPOTIPY_CLIENT_SECRET", "")
SPOTIFY_REDIRECT_URI  = SPOTIFY_REDIRECT_URI or os.environ.get("SPOTIPY_REDIRECT_URI", "")
LASTFM_API_KEY        = LASTFM_API_KEY or os.environ.get("LASTFM_API_KEY", "")

# Proxy: requests (used by spotipy, Last.fm and ReccoBeats) reads HTTP_PROXY/HTTPS_PROXY from the environment.
if PROXY_URL:
    os.environ["HTTP_PROXY"] = PROXY_URL
    os.environ["HTTPS_PROXY"] = PROXY_URL
    os.environ["NO_PROXY"] = "127.0.0.1,localhost"  # the OAuth callback stays local, it must not go through the proxy

# Certificates: corporate proxies with SSL inspection.
if USE_SYSTEM_CERTS:
    _ts = _ensure_package("truststore")
    if _ts:
        _ts.inject_into_ssl()  # Python now validates against the OS certificate store
    else:
        print("INFO: running without 'truststore' - needed behind an SSL-inspecting proxy, harmless otherwise.",
              file=sys.stderr)

# Shared HTTP session (ReccoBeats, Last.fm): connection pooling skips a TLS handshake per call.
_http = requests.Session()

# ======================================================================================================================
# DISK CACHE - protects the Spotify quota and speeds up re-runs
# ======================================================================================================================
_cache = {"playlists": {}, "tempos": {}, "lastfm": {}, "localmatch": {}}
_cache_dirty = 0

def load_cache():
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        for k in _cache:
            _cache[k].update(data.get(k, {}))
        healed = 0
        for entry in _cache["playlists"].values():  # heal caches polluted by a pre-v40 in-place mutation bug
            for t in entry.get("tracks", []):
                if "local_id" in t:
                    t["id"] = t.pop("local_id")
                    t.pop("matched", None)
                    healed += 1
        if healed:
            print(f"! cache healed: {healed} local track(s) restored to their stable key (pre-v40 bug)")
            save_cache(force=True)
        print(f"Cache loaded: {len(_cache['playlists'])} playlists, {len(_cache['tempos'])} BPMs, "
              f"{len(_cache['lastfm'])} Last.fm lookups ({CACHE_FILE})")
    except FileNotFoundError:
        pass
    except (json.JSONDecodeError, OSError) as e:
        print(f"! unreadable cache ({e}) -> starting from scratch", file=sys.stderr)

def save_cache(force=False):
    """Writes the cache to disk atomically (temp file + rename: a kill mid-write cannot corrupt it)."""
    global _cache_dirty
    if not force and _cache_dirty < 50:
        return
    try:
        tmp = CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_cache, f, ensure_ascii=False)
        os.replace(tmp, CACHE_FILE)
        _cache_dirty = 0
    except OSError as e:
        print(f"! cache not saved: {e}", file=sys.stderr)

def quota_exit(context, retry_after=None):
    """Saves the cache then stops the script, telling WHEN Spotify will accept requests again (taken from the Retry-After header of the 429 answer when Spotify provides it)."""
    save_cache(force=True)
    if retry_after:
        secs = int(retry_after)
        h, mn = divmod(secs // 60, 60)
        resume = time.localtime(time.time() + secs)
        day = " tomorrow" if resume.tm_mday != time.localtime().tm_mday else ""
        when = f"Spotify says: retry in {h} h {mn:02d} min (around {time.strftime('%H:%M', resume)}{day})."
    else:
        when = "Spotify did not say when; usually ~24 h after the first refusal."
    sys.exit(f"\nERROR: Spotify quota exhausted ({context}). {when}\n"
             f"Cache saved: the next run only fetches what is missing.")

def spotify_call(fn, context, attempts=4):
    """Runs one Spotify call, retrying a few times on network hiccups (proxy glitches). Quota exhausted (429) = save the cache and stop cleanly."""
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except spotipy.exceptions.SpotifyException as e:
            if e.http_status == 429:
                quota_exit(context, (getattr(e, "headers", None) or {}).get("Retry-After"))
            raise
        except requests.exceptions.RequestException as e:
            if attempt == attempts:
                raise
            print(f"  ! flaky network on {context} ({type(e).__name__}) -> retry {attempt}/{attempts - 1} in 5 s",
                  file=sys.stderr)
            time.sleep(5)

# ======================================================================================================================
# SPOTIFY HELPERS
# ======================================================================================================================
def validate_config(require_spotify=True):
    """Checks every credential/ID in the CONFIG block and returns ALL the problems at once, each with a plain-language fix. Secret values are never displayed."""
    problems = []
    if require_spotify:
        if not re.fullmatch(r"[0-9a-f]{32}", SPOTIFY_CLIENT_ID or ""):
            problems.append(
                f"SPOTIFY_CLIENT_ID looks wrong: the script found {SPOTIFY_CLIENT_ID!r},\n"
                f"    but a real Client ID is 32 letters/digits, like 6a905271bd724376a51cae10e31dd4da.\n"
                f"    FIX: open https://developer.spotify.com/dashboard > your app > Settings,\n"
                f"         copy the Client ID and paste it in the CONFIG block at the top of this file.")
        if not re.fullmatch(r"[0-9a-f]{32}", SPOTIFY_CLIENT_SECRET or ""):
            problems.append(
                f"SPOTIFY_CLIENT_SECRET looks wrong: the script found {len(SPOTIFY_CLIENT_SECRET or '')} character(s)\n"
                f"    (value hidden for safety), but a real secret is 32 letters/digits.\n"
                f"    FIX: open https://developer.spotify.com/dashboard > your app > Settings,\n"
                f"         click 'View client secret', copy it and paste it in the CONFIG block.")
        if not _SPOTIFY_ID_RE.fullmatch(SOURCE_PLAYLIST_ID or ""):
            problems.append(
                f"SOURCE_PLAYLIST_ID looks wrong: the script found {SOURCE_PLAYLIST_ID!r}.\n"
                f"    FIX: in Spotify, right-click your source playlist > Share > Copy link, then keep only\n"
                f"         the 22 characters between /playlist/ and ?si= and paste them in the CONFIG block.")
    if LASTFM_API_KEY and not re.fullmatch(r"[0-9a-f]{32}", LASTFM_API_KEY):
        problems.append(
            f"LASTFM_API_KEY looks wrong: the script found {len(LASTFM_API_KEY)} character(s)\n"
            f"    (value hidden for safety), but a real key is 32 letters/digits.\n"
            f"    FIX: copy your key from https://www.last.fm/api/accounts and paste it in the CONFIG block,\n"
            f"         or leave it empty (\"\") to run without Last.fm.")
    return problems

def get_client() -> spotipy.Spotify:
    problems = validate_config(require_spotify=True)
    if problems:
        sys.exit("The script cannot start: something in the CONFIG block needs fixing.\n\n  * "
                 + "\n\n  * ".join(problems)
                 + "\n\nFix the line(s) above in the CONFIG block at the top of the file, save, and run again.")
    # status_retries=0: on a 429 (quota), spotipy raises immediately instead of sleeping for hours
    auth = SpotifyOAuth(client_id=SPOTIFY_CLIENT_ID, client_secret=SPOTIFY_CLIENT_SECRET, redirect_uri=SPOTIFY_REDIRECT_URI,
                        scope=SCOPES, open_browser=True, cache_path=".spotify_token_cache")
    return spotipy.Spotify(auth_manager=auth, retries=3, status_retries=0, requests_timeout=15)

def fetch_all(sp, first_page, context="pagination"):
    """Follows the "next page" links of a paginated Spotify answer and returns all items."""
    items, page = list(first_page["items"]), first_page
    while page["next"]:
        page = spotify_call(lambda p=page: sp.next(p), context)
        items.extend(page["items"])
    return items

def _parse_playlist_items(items, skipped=None):
    """Turns raw playlist items into simple track dicts. Local files are kept (with a stable synthetic id) when INCLUDE_LOCAL_FILES is on; skipped items are counted with their reason."""
    tracks = []
    for it in items:
        t = it.get("track") or it.get("item") or {}
        if not t:
            if skipped is not None: skipped["empty item"] = skipped.get("empty item", 0) + 1
            continue
        if t.get("is_local"):
            if INCLUDE_LOCAL_FILES and t.get("name"):
                artists = ", ".join(a.get("name", "") for a in t.get("artists", []) if a.get("name")) or "?"
                tracks.append({"id": f"local::{artists.lower()}::{t['name'].lower()}", "name": t["name"], "artists": artists, "artist_ids": [], "local": True})
            elif skipped is not None:
                skipped["local file"] = skipped.get("local file", 0) + 1
            continue
        if not t.get("id"):
            if skipped is not None: skipped["no ID (unavailable)"] = skipped.get("no ID (unavailable)", 0) + 1
            continue
        if t.get("type") and t["type"] != "track":
            if skipped is not None: skipped["non-track (episode...)"] = skipped.get("non-track (episode...)", 0) + 1
            continue
        tracks.append({"id": t["id"], "name": t["name"],
                       "artists": ", ".join(a["name"] for a in t.get("artists", [])),
                       "artist_ids": [a["id"] for a in t.get("artists", []) if a.get("id")]})
    return tracks

def get_playlist_tracks(sp, playlist_id, snapshot_id=None):
    """Returns the tracks of one playlist. Served from the disk cache when the playlist has not changed (snapshot_id).
    Reads the new /items endpoint first, the old /tracks one as fallback; a partially-read playlist is never cached."""
    global _cache_dirty
    cached = _cache["playlists"].get(playlist_id)
    if cached and snapshot_id and cached.get("snapshot_id") == snapshot_id:
        return [dict(t) for t in cached["tracks"]]  # copies: runtime mutations must never leak into the cache

    # 1) new /items endpoint (post-migration) - pages parsed on the fly; network retries are BOUNDED (spotify_call)
    #    and a partially-read playlist is never cached (complete flag).
    tracks, offset, new_ok, complete = [], 0, True, False
    api_total, received, skipped = None, 0, {}
    while True:
        try:
            page = spotify_call(lambda o=offset: sp._get(f"playlists/{playlist_id}/items", limit=100, offset=o),
                                f"reading playlist {playlist_id}")
        except spotipy.exceptions.SpotifyException:
            new_ok = offset > 0  # failure on the very first page -> we will try the old endpoint
            break
        if api_total is None:
            api_total = page.get("total")
        items = page.get("items", [])
        received += len(items)
        tracks.extend(_parse_playlist_items(items, skipped))
        if not items or not page.get("next"):
            complete = True
            break
        offset += len(items)

    # --- read accounting: explain any gap between what Spotify reports and what we keep
    if api_total is not None and len(tracks) < api_total:
        detail = ", ".join(f"{v} {k}" for k, v in sorted(skipped.items(), key=lambda kv: -kv[1])) or "none"
        note = "" if received >= api_total else f" | PAGINATION STOPPED EARLY at {received} (next={bool(page.get('next'))})"
        print(f"  i {playlist_id}: API total={api_total}, items received={received}, kept={len(tracks)} "
              f"(skipped: {detail}){note}")

    # 2) fallback: old /tracks endpoint via spotipy (pre-migration apps)
    if not new_ok and not tracks:
        try:
            page = spotify_call(lambda: sp.playlist_items(playlist_id, additional_types=("track",)),
                                f"reading playlist {playlist_id} (old endpoint)")
            tracks = _parse_playlist_items(fetch_all(sp, page, f"playlist {playlist_id}"))
            complete = True
        except spotipy.exceptions.SpotifyException as e:
            print(f"  ! playlist {playlist_id} unreadable ({e.http_status})", file=sys.stderr)
            tracks = []

    if snapshot_id and complete:
        _cache["playlists"][playlist_id] = {"snapshot_id": snapshot_id, "tracks": [dict(t) for t in tracks]}
        _cache_dirty += 1
        save_cache(force=True)  # every fully-read playlist is immediately secured on disk
    return tracks

def get_liked_tracks(sp):
    """Reads the user's "Liked Songs" library (50 per call). Cached, invalidated when the liked count
    changes. Coded defensively: if the endpoint is unavailable, the run continues without liked songs."""
    global _cache_dirty
    try:
        first = spotify_call(lambda: sp.current_user_saved_tracks(limit=50), "liked songs")
    except spotipy.exceptions.SpotifyException as e:
        print(f"  ! Liked Songs unavailable ({e.http_status}) -> continuing without them", file=sys.stderr)
        return []
    total = first.get("total", 0)
    cached = _cache["playlists"].get("__liked__")
    if cached and cached.get("snapshot_id") == str(total):
        return [dict(t) for t in cached["tracks"]]
    items = fetch_all(sp, first, "liked songs")
    tracks = _parse_playlist_items(items)
    _cache["playlists"]["__liked__"] = {"snapshot_id": str(total), "tracks": [dict(t) for t in tracks]}
    _cache_dirty += 1
    save_cache(force=True)
    return tracks

def get_my_playlists(sp):
    first = spotify_call(lambda: sp.current_user_playlists(limit=50), "playlist listing")
    return fetch_all(sp, first, "playlist listing")

# ======================================================================================================================
# BPM: Spotify -> ReccoBeats fallback (with disk cache, backoff and circuit breaker)
# ======================================================================================================================
_reccobeats_failures = 0  # consecutive failures -> circuit breaker

def _reccobeats_get(endpoint, ids):
    """One ReccoBeats request with retries; honours "slow down" (429) answers.
    After 3 failed batches in a row the script stops calling ReccoBeats for this run (the cache keeps what was fetched)."""
    global _reccobeats_failures
    if _reccobeats_failures >= 3:
        return None
    url = RECCOBEATS_URL.rsplit("/", 1)[0] + "/" + endpoint
    for attempt in range(1, 5):
        try:
            r = _http.get(url, params={"ids": ",".join(ids)}, timeout=20)
            if r.status_code == 429:
                wait = min(int(r.headers.get("Retry-After") or 5), 60)
                print(f"     ! ReccoBeats rate-limit (429) -> pausing {wait}s (attempt {attempt}/4)", file=sys.stderr)
                time.sleep(wait)
                continue
            if r.status_code >= 500:
                time.sleep(2 * attempt)
                continue
            r.raise_for_status()
            _reccobeats_failures = 0
            return r.json().get("content", [])
        except (requests.RequestException, ValueError) as e:
            print(f"     ! ReccoBeats {endpoint}: {type(e).__name__} (attempt {attempt}/4)", file=sys.stderr)
            time.sleep(2 * attempt)
    _reccobeats_failures += 1
    if _reccobeats_failures >= 3:
        print("     ! ReccoBeats: 3 failed batches -> stopping for this run (cache keeps the rest)", file=sys.stderr)
    return None

_RB_ID_RE = re.compile(r"track/([A-Za-z0-9]+)")  # compiled once: href = .../track/{spotify_id}

def get_tempos(sp, tracks_by_id):
    """Finds the BPM of every track: cache, then Spotify, then ReccoBeats (by id), then Deezer (by artist+title),
    then optional local measurement of Deezer previews. One summary line per stage."""
    global _cache_dirty

    def stage(name, found, left, note=""):
        print(f"  * {name:<11}: {found} found, {left} still to fetch{(' - ' + note) if note else ''}")

    tempos = {tid: _cache["tempos"].get(tid) for tid in tracks_by_id}
    missing = [tid for tid, v in tempos.items() if v is None]
    stage("cache", len(tempos) - len(missing), len(missing))
    if not missing:
        return tempos

    # --- Spotify audio-features (pre-2024 apps only); synthetic local IDs excluded
    spotify_ids = [tid for tid in missing if _SPOTIFY_ID_RE.fullmatch(tid)]
    spotify_ok, found = bool(spotify_ids), 0
    try:
        feats = sp.audio_features(spotify_ids[:1]) if spotify_ids else None
        if not feats or feats[0] is None:
            spotify_ok = False
    except spotipy.exceptions.SpotifyException:
        spotify_ok = False
    if spotify_ok:
        for i in range(0, len(spotify_ids), 100):
            for f in sp.audio_features(spotify_ids[i:i + 100]) or []:
                if f and f.get("tempo"):
                    tempos[f["id"]] = round(float(f["tempo"]), 1)
                    _cache["tempos"][f["id"]] = tempos[f["id"]]
                    _cache_dirty += 1
                    found += 1
        save_cache(force=True)
        missing = [tid for tid, v in tempos.items() if v is None]
        stage("Spotify", found, len(missing), "endpoint reached")
    else:
        stage("Spotify", 0, len(missing), "endpoint unavailable (deprecated for this app)")

    # --- ReccoBeats (by Spotify ID, ~40 per batch); local files have no ID
    rb_ids = [tid for tid in missing if _SPOTIFY_ID_RE.fullmatch(tid)]
    found = 0
    for i in range(0, len(rb_ids), 40):
        content = _reccobeats_get("audio-features", rb_ids[i:i + 40])
        if content is None:
            if _reccobeats_failures >= 3:
                break  # circuit breaker: no point insisting on the remaining batches
            continue
        for item in content:
            m = _RB_ID_RE.search(item.get("href", ""))
            sid = m.group(1) if m else None
            if sid in tempos and item.get("tempo"):
                tempos[sid] = round(float(item["tempo"]), 1)
                _cache["tempos"][sid] = tempos[sid]
                _cache_dirty += 1
                found += 1
        save_cache()
        time.sleep(0.5)  # politeness
    save_cache(force=True)
    missing = [tid for tid, v in tempos.items() if v is None]
    stage("ReccoBeats", found, len(missing),
          "stopped by circuit breaker" if _reccobeats_failures >= 3 else "")

    # --- Deezer (by artist+title: catches remaster/edit versions and local files)
    found = 0
    if missing and USE_DEEZER_BPM_FALLBACK:
        print(f"  * Deezer     : {len(missing)} to try (artist+title search, ~0.6 s each)")
        for n, tid in enumerate(missing, 1):
            t = tracks_by_id[tid]
            bpm = get_deezer_bpm(t["artists"], t["name"], tid)
            if bpm:
                tempos[tid] = bpm
                _cache["tempos"][tid] = bpm
                _cache_dirty += 1
                found += 1
            if _deezer_failures >= 5:
                break
            if n % 50 == 0:
                print(f"      ... {n}/{len(missing)} checked, {found} found")
                save_cache(force=True)
        left = sum(1 for v in tempos.values() if v is None)
        stage("Deezer", found, left, "stopped by circuit breaker" if _deezer_failures >= 5 else "")

    # --- optional: measure the tempo locally from the 30 s Deezer previews collected above
    still2 = [tid for tid, v in tempos.items() if v is None]
    if still2 and ANALYZE_DEEZER_PREVIEWS and _preview_urls:
        print(f"  * previews   : {len(still2)} to try (30 s downloads + local tempo analysis, ~3 s each)")
        measured = 0
        for n, tid in enumerate(still2, 1):
            url = _preview_urls.get(tid)  # collected by the Deezer stage, keyed by this track's ID
            if not url:
                continue
            bpm = measure_preview_bpm(url)
            if bpm == "no-librosa":
                print("      ! librosa unavailable (automatic install failed) -> preview analysis skipped")
                break
            if bpm:
                tempos[tid] = bpm
                _cache["tempos"][tid] = bpm
                _cache_dirty += 1
                measured += 1
            if n % 50 == 0:
                print(f"      ... {n}/{len(still2)} analysed, {measured} measured")
                save_cache(force=True)
        stage("previews", measured, sum(1 for v in tempos.values() if v is None))
    save_cache(force=True)
    left = sum(1 for v in tempos.values() if v is None)
    print(f"  = BPM coverage: {len(tempos) - left}/{len(tempos)} ({left} will show as unknown BPM)")
    return tempos

_deezer_failures = 0
_preview_urls = {}

_TITLE_SUFFIX_RE = re.compile(r"\s+-\s+")

def _clean_title(title):
    """Drops version suffixes (" - Radio Edit", " - 2011 Remastered Version"...) before a name-based search."""
    return _TITLE_SUFFIX_RE.split(title)[0].strip()

def get_deezer_bpm(artist, title, tid=None):
    """BPM via the public Deezer API: search the track by name, read its bpm. Works for local files and remastered versions.
    Returns None when Deezer does not know (or has bpm=0)."""
    global _deezer_failures
    if _deezer_failures >= 5:
        return None
    main_artist = artist.split(",")[0].strip()
    try:
        q = f'artist:"{main_artist}" track:"{_clean_title(title)}"'
        r = _http.get(f"{DEEZER_URL}/search", params={"q": q, "limit": 1}, timeout=15)
        r.raise_for_status()
        hits = r.json().get("data", [])
        if not hits:
            _deezer_failures = 0
            return None  # genuinely unknown, not an API failure
        r2 = _http.get(f"{DEEZER_URL}/track/{hits[0]['id']}", timeout=15)
        r2.raise_for_status()
        d = r2.json()
        bpm = d.get("bpm") or 0
        _deezer_failures = 0
        if bpm > 0:
            return round(float(bpm), 1)
        if tid and d.get("preview"):
            _preview_urls[tid] = d["preview"]  # kept for the optional local analysis stage, keyed by Spotify ID
        return None
    except (requests.RequestException, ValueError, KeyError) as e:
        _deezer_failures += 1
        if _deezer_failures >= 5:
            print("     ! Deezer: 5 straight failures -> stopping for this run", file=sys.stderr)
        return None
    finally:
        time.sleep(0.3)  # Deezer allows ~50 req / 5 s; two calls per track -> stay well under

def measure_preview_bpm(preview_url):
    """Downloads a 30 s Deezer preview and measures its tempo with librosa (installed automatically on first use).
    Beat trackers sometimes lock on half/double tempo: treat as an estimate."""
    import io
    librosa = _ensure_package("librosa")  # heavy: pulls numpy/scipy/soundfile, one-time ~1-2 min install
    if librosa is None:
        return "no-librosa"
    try:
        audio = _http.get(preview_url, timeout=20).content
        y, sr = librosa.load(io.BytesIO(audio), sr=22050, mono=True, duration=30)
        tempo = float(librosa.beat.tempo(y=y, sr=sr)[0])
        return round(tempo, 1) if tempo > 0 else None
    except Exception:
        return None

_itunes_failures = 0

def get_itunes_genre(artist, title):
    """Last-resort genre from the keyless iTunes Search API (coarse names like "Hip-Hop/Rap").
    Slow on purpose: Apple tolerates about 20 requests per minute. Misses are cached too."""
    global _itunes_failures, _cache_dirty
    key = f"itunes::{artist.lower()}||{title.lower()}"
    if key in _cache["lastfm"]:
        return _cache["lastfm"][key] or None
    if _itunes_failures >= 5:
        return None
    try:
        r = _http.get(ITUNES_URL, params={"term": f"{artist.split(',')[0].strip()} {_clean_title(title)}",
                                          "media": "music", "limit": 1}, timeout=15)
        time.sleep(3)  # stay under Apple's unofficial ~20 req/min tolerance
        if r.status_code == 403:  # throttled: back off once, then count as failure
            time.sleep(30)
            _itunes_failures += 1
            return None
        r.raise_for_status()
        hits = r.json().get("results", [])
        genre = (hits[0].get("primaryGenreName") or "").lower() if hits else ""
        _itunes_failures = 0
        _cache["lastfm"][key] = genre  # "" cached too: a real miss should not be re-asked
        _cache_dirty += 1
        save_cache()
        return genre or None
    except (requests.RequestException, ValueError, KeyError):
        _itunes_failures += 1
        if _itunes_failures >= 5:
            print("     ! iTunes: 5 straight failures -> stopping for this run", file=sys.stderr)
        return None

def bpm_bucket(tempo):
    """X0-X9 bucket: 87.3 -> 80, 154 -> 150 (truncated to the ten)."""
    return None if tempo is None else int(tempo // BPM_STEP) * BPM_STEP

# ======================================================================================================================
# GENRE - cascade: Last.fm track tags -> Last.fm main-artist tags -> Spotify artist genres
# ======================================================================================================================
def get_artist_genres(sp, artist_ids):
    """{artist_id: [genres]} - genre fallback, off by default (quota killer since 02/2026: one call per artist).
    When enabled: capped at MAX_ARTIST_LOOKUPS, immediate stop on 429."""
    if not USE_SPOTIFY_ARTIST_GENRES:
        print("  (Spotify artist-genre fallback disabled - Last.fm only, see USE_SPOTIFY_ARTIST_GENRES)")
        return {}
    out, ids = {}, list(set(artist_ids))
    try:  # 1) batch (50 per call) - pre-migration apps
        for i in range(0, len(ids), 50):
            for a in sp.artists(ids[i:i + 50])["artists"]:
                if a:
                    out[a["id"]] = [g.lower() for g in a.get("genres", [])]
        return out
    except spotipy.exceptions.SpotifyException:
        print("  ! batch /artists endpoint removed (Spotify 02/2026 migration) -> unitary calls", file=sys.stderr)
    if len(ids) > MAX_ARTIST_LOOKUPS:
        print(f"  ! {len(ids)} artists > cap: only the first {MAX_ARTIST_LOOKUPS} queried (quota protection)", file=sys.stderr)
    try:  # 2) unitary - capped
        for i, aid in enumerate(ids[:MAX_ARTIST_LOOKUPS]):
            a = sp.artist(aid)
            if a:
                out[aid] = [g.lower() for g in a.get("genres", [])]
            if i % 50 == 49:
                time.sleep(1)
    except spotipy.exceptions.SpotifyException as e:
        if e.http_status == 429:
            print("  ! Spotify quota reached (429) -> genre fallback abandoned", file=sys.stderr)
        else:
            print(f"  ! Spotify artist genres unavailable ({e.http_status}) -> fallback abandoned", file=sys.stderr)
    return out

_lastfm_fail_streak = 0  # consecutive failures -> circuit breaker (avoids 2000 calls doomed to fail)
_lastfm_disabled = False

def _lastfm_get(params, verbose=False):
    """One Last.fm request with retries; honours rate limits. After 10 straight network failures Last.fm is cut off for this run (already-fetched tags stay cached)."""
    global _lastfm_fail_streak, _lastfm_disabled
    if _lastfm_disabled or not LASTFM_API_KEY:
        return None
    for attempt in range(1, 4):
        try:
            r = _http.get(LASTFM_URL, params={**params, "api_key": LASTFM_API_KEY, "format": "json"}, timeout=15)
            data = r.json()
            err_code = data.get("error") if isinstance(data, dict) else None
            err = data.get("message") if err_code else None
            if r.status_code == 429 or err_code == 29:  # 29 = Last.fm-side rate limit exceeded
                wait = min(int(r.headers.get("Retry-After") or 5), 30) * attempt
                print(f"     ! Last.fm rate-limit -> pausing {wait}s (attempt {attempt}/3)", file=sys.stderr)
                time.sleep(wait)
                continue
            if verbose:
                print(f"     [Last.fm HTTP {r.status_code}{' - error: ' + err if err else ''}]")
            _lastfm_fail_streak = 0
            if r.status_code != 200 or err:
                return None
            return data
        except (requests.RequestException, ValueError) as e:
            if verbose:
                print(f"     [Last.fm NETWORK FAILURE: {type(e).__name__} - proxy?]")
            time.sleep(2 * attempt)
    _lastfm_fail_streak += 1
    if _lastfm_fail_streak >= 10 and not _lastfm_disabled:
        _lastfm_disabled = True
        print("     ! Last.fm: 10 consecutive failures -> stopping for this run (cache keeps the rest)", file=sys.stderr)
    return None

def _extract_tags(data, root):
    raw = (data or {}).get(root, {}).get("tag", [])
    if isinstance(raw, dict):  # Last.fm returns a dict (not a list) when there is a single tag
        raw = [raw]
    return [t["name"].lower() for t in raw if int(t.get("count", 0)) >= LASTFM_MIN_TAG_COUNT][:10]

def get_lastfm_track_tags(artist, title, verbose=False):
    """Last.fm tags of one specific TRACK. Empty list when unknown. Cached on disk; network failures are never cached, so they are retried next run."""
    global _cache_dirty
    key = f"{artist.lower()}||{title.lower()}"
    if key in _cache["lastfm"]:
        return _cache["lastfm"][key]
    if not LASTFM_API_KEY:
        return []
    data = _lastfm_get({"method": "track.gettoptags", "artist": artist, "track": title, "autocorrect": 1}, verbose=verbose)
    time.sleep(0.25)  # honour the Last.fm rate limit
    if data is None:
        return []
    tags = _extract_tags(data, "toptags")
    _cache["lastfm"][key] = tags
    _cache_dirty += 1
    save_cache()  # periodic save (every ~50 new lookups)
    return tags

def get_lastfm_artist_tags(artist, verbose=False):
    """Last.fm tags of an ARTIST - the safety net when the track itself has no tags."""
    global _cache_dirty
    key = f"artist::{artist.lower()}"
    if key in _cache["lastfm"]:
        return _cache["lastfm"][key]
    if not LASTFM_API_KEY:
        return []
    data = _lastfm_get({"method": "artist.gettoptags", "artist": artist, "autocorrect": 1}, verbose=verbose)
    time.sleep(0.25)
    if data is None:
        return []
    tags = _extract_tags(data, "toptags")
    _cache["lastfm"][key] = tags
    _cache_dirty += 1
    save_cache()
    return tags

# One compiled pattern per keyword. Keywords match on WORD BOUNDARIES ("dub" no longer fires inside "dubstep");
# the few prefix keywords below intentionally match their derivatives (electro -> electronic(a)).
_PREFIX_KWS = {"orchestr", "electro"}
_CAT_RANK = {cat: rank for rank, (cat, _) in enumerate(GENRE_RULES)}
_KW_PATTERNS = [(rank, cat, kw, re.compile(r"\b" + re.escape(kw) + ("" if kw in _PREFIX_KWS else r"\b")))
                for rank, (cat, kws) in enumerate(GENRE_RULES) for kw in kws]

def _tag_vote(tag):
    """Gives ONE vote for one tag: an EXPLICIT_GENRE_MAP pin wins outright; otherwise the category with the LONGEST matching keyword ("electro swing" goes to Electro, not Jazz)."""
    pinned = EXPLICIT_GENRE_MAP.get(tag.strip().lower())
    if pinned:
        return pinned
    best_key, best_cat = None, None
    for rank, cat, kw, pat in _KW_PATTERNS:
        if pat.search(tag):
            key = (len(kw), -rank)
            if best_key is None or key > best_key:
                best_key, best_cat = key, cat
    return best_cat

def match_category(genre_list):
    """Turns a list of raw tags into one of your categories by majority vote (one vote per tag, see _tag_vote).
    Ties are settled by the GENRE_RULES order. None when nothing matches."""
    votes = {}
    for g in genre_list:
        cat = _tag_vote(g)
        if cat:
            votes[cat] = votes.get(cat, 0) + 1
    if not votes:
        return None
    return min(votes.items(), key=lambda kv: (-kv[1], _CAT_RANK[kv[0]]))[0]

_genre_cache = {}

def resolve_genre(track, artist_genres):
    """Finds a track's genre, most precise source first: Last.fm tags of the track, then of the main artist, then iTunes, then Spotify artist genres.
    Returns (category, raw tags, source)."""
    if track["id"] in _genre_cache:
        return _genre_cache[track["id"]]

    # 1) Last.fm: per-TRACK tags (the "right info", per song)
    main_artist = track["artists"].split(",")[0].strip()
    tags = get_lastfm_track_tags(main_artist, track["name"])
    cat = match_category(tags) if tags else None
    if cat:
        result = (cat, tags, "Last.fm (track)")
    else:
        # 2) Last.fm: MAIN ARTIST tags (obscure tracks rarely have track tags)
        atags = get_lastfm_artist_tags(main_artist)
        cat2 = match_category(atags) if atags else None
        if cat2:
            result = (cat2, atags, "Last.fm (artist)")
        else:
            # 3) iTunes Search: coarse primaryGenreName by artist+title (keyless, slow, last real lookup)
            ig = get_itunes_genre(main_artist, track["name"]) if USE_ITUNES_GENRE_FALLBACK else None
            cat3 = match_category([ig]) if ig else None
            if cat3:
                result = (cat3, [ig], "iTunes (track)")
            else:
                # 4) Fallback: Spotify genres of the track's artists
                sp_genres = [g for aid in track["artist_ids"] for g in artist_genres.get(aid, [])]
                cat4 = match_category(sp_genres) if sp_genres else None
                if cat4:
                    result = (cat4, sp_genres, "Spotify (artist)")
                else:
                    result = (None, tags or atags or ([ig] if ig else []) or sp_genres, "none")

    _genre_cache[track["id"]] = result
    return result

# ======================================================================================================================
# EXTERNAL-API TEST MODE (no Spotify OAuth, quota-free except the optional per-unknown-track fallback)
# ======================================================================================================================
def run_external_test():
    """TEST_EXTERNES mode: checks ReccoBeats and Last.fm on a few pasted links, without touching the Spotify quota
    (except the optional 1-call-per-unknown-track lookup)."""
    print(f"EXTERNAL TEST: {len(TEST_EXTERNES)} track(s) - ReccoBeats + Last.fm, zero Spotify calls\n")
    entries = []
    for line in TEST_EXTERNES:
        parts = [p.strip() for p in line.split("|")]
        m = re.search(r"track/([A-Za-z0-9]{22})", parts[0]) or re.fullmatch(r"([A-Za-z0-9]{22})", parts[0])
        if not m:
            print(f"  ! no Spotify ID found in: {parts[0]!r}")
            continue
        artist = parts[1] if len(parts) >= 3 else ""
        title = parts[2] if len(parts) >= 3 else ""
        entries.append({"id": m.group(1), "artist": artist, "title": title})
    if not entries:
        sys.exit("No valid entry in TEST_EXTERNES.")

    # --- 0) ReccoBeats /track: fetch title + artist for "link only" entries (needed for Last.fm)
    need = [e for e in entries if not e["title"]]
    if need:
        content = _reccobeats_get("track", [e["id"] for e in need])
        if content is not None:
            print("0) ReccoBeats (track info): OK")
            by_id = {}
            for item in content:
                m = _RB_ID_RE.search(item.get("href", ""))
                if m:
                    by_id[m.group(1)] = item
            for e in need:
                item = by_id.get(e["id"], {})
                e["title"] = item.get("trackTitle") or item.get("title") or ""
                arts = item.get("artists") or []
                e["artist"] = ", ".join(a.get("name", "") for a in arts if isinstance(a, dict)) if arts else ""
                if e["title"]:
                    print(f"   {e['id']} -> {e['title']} - {e['artist'] or '?'}")
                else:
                    print(f"   ! {e['id']} unknown to ReccoBeats (force it with 'link | Artist | Title')")
        else:
            print("0) ReccoBeats (track info): FAILED after retries - see messages above")
        print()

    # --- 0bis) Spotify fallback for tracks unknown to ReccoBeats (1 API call per track, no more)
    still = [e for e in entries if not e["title"]]
    if still and SPOTIFY_TRACK_FALLBACK and validate_config(require_spotify=True):
        print(f"0bis) Spotify fallback skipped (invalid Spotify credentials in CONFIG): "
              f"{len(still)} track(s) will remain unidentified\n")
    elif still and SPOTIFY_TRACK_FALLBACK:
        print(f"0bis) Spotify (track-info fallback: only {len(still)} API call(s)):")
        try:
            sp_fallback = get_client()
            for e in still:
                try:
                    t = sp_fallback.track(e["id"])
                except spotipy.exceptions.SpotifyException as ex:
                    if ex.http_status == 429:
                        print("   ! Spotify quota exhausted -> fallback abandoned for the remaining tracks")
                        break
                    print(f"   ! {e['id']}: Spotify error {ex.http_status}")
                    continue
                e["title"] = t.get("name", "")
                e["artist"] = ", ".join(a.get("name", "") for a in t.get("artists", []))
                print(f"   {e['id']} -> {e['title']} - {e['artist']}  [source: Spotify]")
        except Exception as ex:
            print(f"   ! Spotify connection impossible ({type(ex).__name__}) -> fallback abandoned")
        print()
    elif still:
        print("0bis) Spotify fallback disabled (SPOTIFY_TRACK_FALLBACK=False): "
              f"{len(still)} track(s) will remain unidentified\n")

    # --- 1) ReccoBeats: BPM in a single batch
    print("1) ReccoBeats (BPM):")
    bpm = {}
    content = _reccobeats_get("audio-features", [e["id"] for e in entries])
    if content is not None:
        print("   OK")
        for item in content:
            m = _RB_ID_RE.search(item.get("href", ""))
            if m and item.get("tempo"):
                bpm[m.group(1)] = round(float(item["tempo"]), 1)
                _cache["tempos"][m.group(1)] = bpm[m.group(1)]
    else:
        print("   ! ReccoBeats FAILED after retries (proxy 401/403/timeout = corporate filtering)")

    # --- 2) Last.fm: per-track tags, then artist tags when the track has none
    print("\n2) Last.fm (track tags, then artist tags when the track has none):")
    first = True
    for e in entries:
        e["bpm"] = bpm.get(e["id"])
        if not e["title"] or not e["artist"]:
            e["tags"], e["tag_src"] = [], "none"
            print(f"   {e['id']}: skipped (title/artist unknown)")
            continue
        main_artist = e["artist"].split(",")[0].strip()  # Last.fm only knows the main artist
        e["tags"] = get_lastfm_track_tags(main_artist, e["title"], verbose=first)
        e["tag_src"] = "track"
        if not e["tags"]:
            e["tags"] = get_lastfm_artist_tags(main_artist, verbose=first)
            e["tag_src"] = "artist"
        first = False
        print(f"   {e['title']} - {main_artist}: {len(e['tags'])} tag(s) via {e['tag_src']} {e['tags'][:5]}")
    if all(not e.get("tags") for e in entries):
        print("   ! no tags at all - see [Last.fm HTTP ...] above: bad key / proxy block / unknown to Last.fm")

    # --- 3) Summary: what the real report would do with it
    print("\n3) Summary (what the real report would make of it):")
    for e in entries:
        bucket = bpm_bucket(e["bpm"])
        genre = match_category(e["tags"]) if e["tags"] else None
        print(f"   - {e['title'] or e['id']} - {e['artist'] or '?'}")
        print(f"       BPM {e['bpm'] if e['bpm'] else '?'} -> {'playlist ' + str(bucket) + ' bpm' if bucket is not None else 'unknown BPM'}")
        print(f"       Genre -> {genre or 'unidentified'} (tags {e.get('tag_src', '?')}: {'; '.join(e['tags'][:4]) or 'none'})")

    save_cache(force=True)
    print(f"\nBPMs and tags saved to {CACHE_FILE}: they will be reused by the real run.")
    print("Test finished - no Spotify API call was made." if not (still and SPOTIFY_TRACK_FALLBACK)
          else "Test finished - only the track-info fallback touched the Spotify API.")

def _norm(s):
    """Loose text normalisation (case, punctuation, version suffixes) used to compare titles/artists."""
    return re.sub(r"[^a-z0-9]+", " ", _clean_title(s).lower()).strip()

def match_local_tracks(sp, tracks):
    """Finds the Spotify catalog equivalent of each local file so it becomes sortable.
    Up to 3 searches per file, each only if the previous failed: normal fields, swapped fields (rescues inverted MP3 tags), free text.
    Hits AND misses are cached; the per-run cap counts search calls."""
    global _cache_dirty
    locals_ = [t for t in tracks if t.get("local")]
    if not locals_ or not MATCH_LOCAL_FILES:
        return

    def eligible(hit):  # never searched, or an old miss that predates the extended modes
        return hit is None or (not hit.get("id") and hit.get("v", 1) < 2)

    def acceptable(cand, name, artist):
        """A candidate is valid if its title/artist correspond to ours in EITHER orientation."""
        cn, ca = _norm(cand.get("name", "")), _norm(", ".join(a["name"] for a in cand.get("artists", [])))
        tn, ta = _norm(name), _norm(artist)
        pair_ok = lambda a, b: a and b and (a in b or b in a)
        return (pair_ok(tn, cn) and (pair_ok(ta, ca) or not ta)) or (pair_ok(tn, ca) and pair_ok(ta, cn))

    to_do = sum(1 for t in locals_ if eligible(_cache["localmatch"].get(t.get("local_id") or t["id"])))
    if to_do:
        cap = f", capped at {MAX_LOCAL_MATCH_PER_RUN} search calls this run" if MAX_LOCAL_MATCH_PER_RUN else ""
        print(f"  * local match: {to_do} local file(s) to search (1-3 calls each{cap})")
    calls = matched = 0
    for t in locals_:
        key = t.get("local_id") or t["id"]  # synthetic local::artist::title, stable across runs
        hit = _cache["localmatch"].get(key)
        if eligible(hit):
            if MAX_LOCAL_MATCH_PER_RUN and calls >= MAX_LOCAL_MATCH_PER_RUN:
                continue
            main = t["artists"].split(",")[0].strip()
            title = _clean_title(t["name"])
            attempts = [f'track:"{title}" artist:"{main}"',
                        f'track:"{main}" artist:"{title}"',      # swapped tags rescue
                        f"{title} {main}"]                        # free-text rescue
            hit = {"id": "", "name": "", "artists": "", "v": 2}
            aborted = False
            for q in attempts:
                if MAX_LOCAL_MATCH_PER_RUN and calls >= MAX_LOCAL_MATCH_PER_RUN:
                    aborted = True  # cap hit mid-track: not cached, cleanly redone next run
                    break
                calls += 1
                time.sleep(LOCAL_MATCH_PACE)  # paced: Spotify rate-limits bursts on a rolling window
                if calls % 100 == 0:
                    print(f"      ... {calls} search calls, {matched} matched")
                try:
                    res = spotify_call(lambda q=q: sp.search(q=q, type="track", limit=3), "local file matching")
                except spotipy.exceptions.SpotifyException:
                    aborted = True
                    break
                for cand in (res.get("tracks", {}) or {}).get("items", []):
                    if acceptable(cand, t["name"], main):
                        hit = {"id": cand["id"], "name": cand["name"],
                               "artists": ", ".join(a["name"] for a in cand.get("artists", [])), "v": 2}
                        break
                if hit["id"]:
                    break
            if not aborted:
                _cache["localmatch"][key] = hit
                _cache_dirty += 1
                save_cache()
        if hit and hit.get("id"):
            t["local_id"], t["id"], t["matched"] = key, hit["id"], True
            t["artist_ids"] = []  # unknown, and unused (genre goes through the name-based cascade anyway)
            matched += 1
    save_cache(force=True)
    print(f"  * local match: {matched}/{len(locals_)} matched on the Spotify catalog ({calls} search calls this run)")

# ======================================================================================================================
# REAL DATA GATHERING
# ======================================================================================================================
def gather_real_data(sp):
    """Reads everything the analysis needs: your playlists, the source tracks, the local-file matches, the BPMs and the artist genres.
    Returns it all to main()."""
    # ---------------- The user's playlists ----------------
    print("Reading your playlists...")
    all_playlists = get_my_playlists(sp)
    snapshots = {p["id"]: p.get("snapshot_id") for p in all_playlists}  # for cache invalidation
    bpm_playlists, genre_playlists = {}, {}  # {60: {"id","name"}} / {"Pop": {"id","name"}}
    for p in all_playlists:
        name = p["name"].strip()
        m = re.fullmatch(r"(\d{2,3})\s*bpm", name, re.IGNORECASE)
        if m:
            bpm_playlists[int(m.group(1))] = {"id": p["id"], "name": name}
        elif name in GENRE_PLAYLIST_NAMES:
            genre_playlists[name] = {"id": p["id"], "name": name}

    for cat, pid in GENRE_PLAYLIST_ID_OVERRIDES.items():
        genre_playlists[cat] = {"id": pid, "name": cat}  # bound by ID: name and folder are irrelevant

    print(f"  BPM playlists found   : {sorted(bpm_playlists)}")
    print(f"  Genre playlists found : {sorted(genre_playlists)}\n")

    # --- diagnostic: if nothing is recognised, show what the API returns to understand why
    if not bpm_playlists and not genre_playlists:
        print(f"!! DIAGNOSTIC: no BPM/Genre playlist recognised - exact API names below ({len(all_playlists)} total):")
        for p in all_playlists:
            print(f"!!   - {p['name']!r} (owner: {p['owner']['id']})")
        print("!! Common causes: different name (space, case, accent), or a different Spotify account.\n")

    # ---------------- Contents of every relevant playlist ----------------
    contents = {}  # {playlist_name: [tracks]}
    for info in list(bpm_playlists.values()) + list(genre_playlists.values()):
        contents[info["name"]] = get_playlist_tracks(sp, info["id"], snapshots.get(info["id"]))
        print(f"  {info['name']:<12} : {len(contents[info['name']])} tracks")

    source_tracks = get_playlist_tracks(sp, SOURCE_PLAYLIST_ID, snapshots.get(SOURCE_PLAYLIST_ID))
    print(f"\nSource playlist: {len(source_tracks)} tracks")
    if INCLUDE_LIKED_SONGS:
        seen = {t["id"] for t in source_tracks}
        liked = [dict(t, liked=True) for t in get_liked_tracks(sp) if t["id"] not in seen]
        source_tracks.extend(liked)
        print(f"Liked songs added: {len(liked)} (not already in the source)")
    print()
    if not source_tracks:
        print("!! DIAGNOSTIC: the source playlist returns no usable track.")
        try:
            meta = sp.playlist(SOURCE_PLAYLIST_ID)
            owner = meta.get("owner", {}).get("id", "?")
            total = meta.get("tracks", {}).get("total", "?")
            print(f"!!   Name: {meta.get('name')!r} | owner: {owner} | reported total: {total}")
            print(f"!!   Fields returned by the API: {sorted(meta.keys())}")
        except Exception as e:
            print(f"!!   Could not read the playlist metadata: {e}")
        try:
            raw = sp.playlist_items(SOURCE_PLAYLIST_ID, limit=3)  # old endpoint, raw response
            items = raw.get("items", [])
            print(f"!!   Old /tracks endpoint: total={raw.get('total', '?')}, page-1 items={len(items)}")
            for it in items[:2]:
                print(f"!!     item keys : {sorted(it.keys())}")
                print(f"!!     raw excerpt: {str(it)[:250]}")
        except Exception as e:
            print(f"!!   Old /tracks endpoint: error {e}")
        try:
            raw2 = sp._get(f"playlists/{SOURCE_PLAYLIST_ID}/items", limit=3)  # new 02/2026 endpoint
            items2 = raw2.get("items", [])
            print(f"!!   New /items endpoint: total={raw2.get('total', '?')}, page-1 items={len(items2)}")
            for it in items2[:2]:
                print(f"!!     item keys : {sorted(it.keys())}")
                print(f"!!     raw excerpt: {str(it)[:250]}")
        except Exception as e:
            print(f"!!   New /items endpoint: error {e}")
        print("!!   -> Send this diagnostic block as-is for analysis.\n")

    # ---------------- Local files: find their Spotify catalog equivalents ----------------
    every = list(source_tracks)
    for lst in contents.values():
        every.extend(lst)
    match_local_tracks(sp, every)

    # ---------------- BPM + genres for ALL tracks ----------------
    all_tracks = {t["id"]: t for t in source_tracks}
    for lst in contents.values():
        for t in lst:
            all_tracks.setdefault(t["id"], t)

    print("Fetching BPMs...")
    tempos = get_tempos(sp, all_tracks)

    print("Fetching artist genres...")
    artist_genres = get_artist_genres(sp, [aid for t in all_tracks.values() for aid in t["artist_ids"]])
    print()
    return bpm_playlists, genre_playlists, contents, source_tracks, tempos, artist_genres

# ======================================================================================================================
# MAIN
# ======================================================================================================================
def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    problems = validate_config(require_spotify=not TEST_EXTERNES)
    if problems:
        sys.exit("The script cannot start: something in the CONFIG block needs fixing.\n\n  * "
                 + "\n\n  * ".join(problems)
                 + "\n\nFix the line(s) above in the CONFIG block at the top of the file, save, and run again.")
    if TEST_EXTERNES:  # test mode: ReccoBeats + Last.fm only, zero Spotify calls
        load_cache()
        run_external_test()
        return
    load_cache()
    sp = get_client()
    me = spotify_call(sp.current_user, "authentication")
    print(f"Logged in as: {me['display_name']} ({me['id']})")
    if LASTFM_API_KEY:
        print("Per-track genre: Last.fm ACTIVE (primary source), Spotify artist as fallback\n")
    else:
        print("WARNING: LASTFM_API_KEY missing -> genre via Spotify artists only (free key: last.fm/api/account/create)\n")
    bpm_playlists, genre_playlists, contents, source_tracks, tempos, artist_genres = gather_real_data(sp)

    # per-playlist ID sets to test membership
    ids_in = {name: {t["id"] for t in lst} for name, lst in contents.items()}

    # =====================================================================
    # 1) SUGGESTED ADDITIONS for the source playlist
    # =====================================================================
    rows_add = []
    to_create_bpm = defaultdict(list)       # {bucket: [tracks]} -> BPM playlists to create
    to_create_genre = defaultdict(list)     # {genre: [tracks]}  -> Genre playlists to create
    unknown_genre_tags = defaultdict(list)  # {raw genre/tag: [tracks]} unmapped
    print("=" * 100)
    print("1) SOURCE PLAYLIST TRACKS -> SUGGESTED ADDITIONS")
    print("=" * 100)
    for t in source_tracks:
        tempo = tempos.get(t["id"])
        bucket = bpm_bucket(tempo)
        genre, raw_genres, genre_src = resolve_genre(t, artist_genres)

        # --- BPM target (console shows only the measured tempo; the full action lives in the CSV)
        if tempo is None:
            bpm_action = "unknown BPM (check manually)"
        elif bucket in bpm_playlists:
            pname = bpm_playlists[bucket]["name"]
            bpm_action = f"already in '{pname}'" if t["id"] in ids_in.get(pname, set()) else f"ADD to '{pname}'"
        else:
            bpm_action = f"CREATE playlist '{bucket} bpm' then add this track"
            to_create_bpm[bucket].append(f"{t['name']} - {t['artists']}")

        alts = []
        if SHOW_HALF_DOUBLE_TEMPO and tempo:
            alts = [f"{x} bpm" for x in {bpm_bucket(tempo / 2), bpm_bucket(tempo * 2)} - {bucket} if x in bpm_playlists]

        # --- Genre target
        src_short = genre_src.replace("(", "").replace(")", "")
        if genre is None:
            genre_action, g_tok = f"unidentified genre ({'; '.join(raw_genres[:3]) or 'no Spotify or Last.fm info'})", "genre ?"
            for g in raw_genres:
                unknown_genre_tags[g].append(f"{t['name']} - {t['artists']}")
        elif genre in genre_playlists:
            pname = genre_playlists[genre]["name"]
            already = t["id"] in ids_in.get(pname, set())
            base = f"already in '{pname}'" if already else f"ADD to '{pname}'"
            genre_action = f"{base} [source: {genre_src}]"
            g_tok = ("=" if already else "+") + pname
        else:
            genre_action = f"CREATE playlist '{genre}' then add this track [source: {genre_src}]"
            g_tok = f"NEW {genre}"
            to_create_genre[genre].append(f"{t['name']} - {t['artists']}")

        loc = (" [LOCAL~Spotify]" if t.get("matched") else " [LOCAL]") if t.get("local") else (" [LIKED]" if t.get("liked") else "")
        tag = raw_genres[0] if raw_genres else ""
        # console line: only what is known - except "(? bpm)" on an otherwise-complete line, so a missing tempo does not go unnoticed.
        # A fully unresolved track stays bare (details always live in the CSV).
        parts = [f"- {t['name']} - {t['artists']}{loc}"]
        if tempo:
            parts.append(f"({tempo:g} bpm" + (f" or {' or '.join(alts)}" if alts else "") + ")")
        elif genre is not None:
            parts.append("(? bpm)")
        if genre is not None:
            parts.append(f": {tag + ' ' if tag else ''}-> {g_tok} ({src_short})")
        print(" ".join(parts))
        rows_add.append([t["name"] + loc, t["artists"], tempo, bucket, bpm_action, genre or "?", genre_src, genre_action, "; ".join(raw_genres[:5])])

    # =====================================================================
    # 2) POTENTIALLY MISPLACED TRACKS
    # =====================================================================
    rows_misplaced = []
    print("\n" + "=" * 100)
    print("2) POTENTIALLY MISPLACED TRACKS IN YOUR PLAYLISTS")
    print("=" * 100)

    for bucket_val, info in sorted(bpm_playlists.items()):
        for t in contents[info["name"]]:
            tempo = tempos.get(t["id"])
            if tempo is None:
                continue
            real = bpm_bucket(tempo)
            # tolerance: fine if the exact bucket OR the half/double-tempo version matches
            if real == bucket_val or bpm_bucket(tempo / 2) == bucket_val or bpm_bucket(tempo * 2) == bucket_val:
                continue
            dest = bpm_playlists.get(real)
            dest_name = dest["name"] if dest else f"{real} bpm (does not exist)"
            print(f"- [{info['name']}] {t['name']} - {t['artists']}: measured BPM {tempo} -> MOVE to '{dest_name}'")
            rows_misplaced.append([info["name"], t["name"], t["artists"], tempo, dest_name, "BPM"])

    skipped_artist_flags = 0
    for gname, info in sorted(genre_playlists.items()):
        if gname in MISPLACED_GENRE_EXEMPT:
            continue  # deliberate catch-all playlists are not audited
        for t in contents[info["name"]]:
            genre, raw, src = resolve_genre(t, artist_genres)
            if genre and genre != gname:
                if any({gname, genre} <= pair for pair in NEIGHBOR_GENRES):
                    continue  # sibling playlists: the border is a curation choice, not a misplacement
                if MISPLACED_GENRE_TRACK_ONLY and "track" not in src:
                    skipped_artist_flags += 1  # artist-level tag = too weak a signal to flag
                    continue
                print(f"- [{gname}] {t['name']} - {t['artists']}: estimated genre '{genre}' via {src} ({'; '.join(raw[:3])}) -> double-check")
                rows_misplaced.append([gname, t["name"], t["artists"], "; ".join(raw[:5]), genre, f"Genre ({src})"])
    if skipped_artist_flags:
        print(f"({skipped_artist_flags} weaker artist-level genre flags hidden - set MISPLACED_GENRE_TRACK_ONLY=False to see them)")

    if not rows_misplaced:
        print("Nothing to report.")

    # =====================================================================
    # 3) DUPLICATES
    # =====================================================================
    rows_dupes = []
    print("\n" + "=" * 100)
    print("3) DUPLICATES INSIDE EACH PLAYLIST")
    print("=" * 100)
    for pname, lst in contents.items():
        seen_id, seen_name = defaultdict(int), defaultdict(int)
        for t in lst:
            seen_id[t["id"]] += 1
            seen_name[(t["name"].lower(), t["artists"].lower())] += 1
        for t in lst:
            key = (t["name"].lower(), t["artists"].lower())
            if seen_id[t["id"]] > 1 or seen_name[key] > 1:
                reason = f"same track present {seen_id[t['id']]}x" if seen_id[t["id"]] > 1 else "same name+artist (different versions/IDs)"
                print(f"- [{pname}] {t['name']} - {t['artists']}: {reason} -> DELETE the duplicate")
                rows_dupes.append([pname, t["name"], t["artists"], reason])
                seen_id[t["id"]], seen_name[key] = 1, 1  # show only once
    if not rows_dupes:
        print("No duplicates detected.")

    # =====================================================================
    # 4) PLAYLISTS TO CREATE (missing BPMs / missing genres)
    # =====================================================================
    rows_create = []
    print("\n" + "=" * 100)
    print("4) PLAYLISTS TO CREATE (suggestion)")
    print("=" * 100)
    for label, kind, pending in ([(f"{b} bpm", "BPM", to_create_bpm[b]) for b in sorted(to_create_bpm)]
                                 + [(g, "Genre", to_create_genre[g]) for g in sorted(to_create_genre)]):
        print(f"- '{label}': {len(pending)} pending track(s)")
        for x in pending:
            print(f"      . {x}")
            rows_create.append([label, kind, x])
    if not rows_create:
        print("Your existing playlists cover every analysed track.")

    if unknown_genre_tags:
        print("\nGenres/tags encountered but not mapped to your categories (sorted by frequency - useful to decide")
        print("on a possible new coarse-grained playlist, or to enrich GENRE_RULES):")
        for g, titles in sorted(unknown_genre_tags.items(), key=lambda kv: len(kv[1]), reverse=True)[:15]:
            print(f"  - '{g}': {len(titles)} track(s)  e.g. {titles[0]}")

    # =====================================================================
    # CSV EXPORT
    # =====================================================================
    def write_csv(fname, header, rows):
        path = os.path.join(OUTPUT_DIR, fname)
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(header)
            w.writerows(rows)
        print(f"  -> {path}")

    print("\nExporting CSV reports:")
    write_csv("1_suggested_additions.csv", ["Title", "Artists", "BPM", "BPM bucket", "BPM action", "Estimated genre",
                                            "Genre source", "Genre action", "Raw genres/tags"], rows_add)
    write_csv("2_misplaced.csv", ["Current playlist", "Title", "Artists", "Measurement", "Suggested destination", "Type"], rows_misplaced)
    write_csv("3_duplicates.csv", ["Playlist", "Title", "Artists", "Reason"], rows_dupes)
    write_csv("4_playlists_to_create.csv", ["Playlist to create", "Type", "Pending track"], rows_create)

    save_cache(force=True)
    print("\nDone. Nothing was modified on your account (dry-run).")

if __name__ == "__main__":
    main()
