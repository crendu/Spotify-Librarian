#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SpotifyCore - shared engine behind SpotifySortPlaylist.py and SpotifyLibraryAnalysis.py.

Not meant to be run directly. It owns everything both tools need identically: the CONFIG block (one
place to edit, regardless of which tool you run), the disk cache and quota handling, reading your
playlists/Liked Songs/local files, the BPM cascade (ReccoBeats -> Deezer -> preview measurement), the
genre cascade (Last.fm -> iTunes -> Spotify artist genres, voted into one of your categories), and
gather_real_data() - the single function that reads everything both tools analyse.

Both importing scripts share this module's cache/circuit-breaker state normally (Python only loads a
module once per process) - but each is typically run as its own separate "python ....py" process, so
the real persistence across runs is the disk cache file (CACHE_FILE), exactly as before this file existed.
"""
import csv
import json
import os
import re
import sys
import logging
import time
import webbrowser
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone

import requests
import spotipy
from spotipy.oauth2 import SpotifyOAuth

logging.getLogger("spotipy").setLevel(logging.CRITICAL)

__all__ = [
    "_ensure_package",
    "SPOTIFY_CLIENT_ID",
    "SPOTIFY_CLIENT_SECRET",
    "SOURCE_PLAYLIST_ID",
    "LASTFM_API_KEY",
    "PROXY_URL",
    "USE_SYSTEM_CERTS",
    "INCLUDE_LOCAL_FILES",
    "MATCH_LOCAL_FILES",
    "MAX_LOCAL_MATCH_PER_RUN",
    "LOCAL_MATCH_PACE",
    "INCLUDE_LIKED_SONGS",
    "CHECK_ALL_PLAYLISTS_FOR_DUPLICATES",
    "ANALYZE_YEARLY_PLAYLISTS",
    "ANALYZE_DEEZER_PREVIEWS",
    "AUTO_OPEN_REVIEW",
    "EXPORT_LIBRARY_BACKUP",
    "AUTO_FOLLOW_ARTISTS",
    "TEST_EXTERNES",
    "SPOTIFY_TRACK_FALLBACK",
    "SPOTIFY_REDIRECT_URI",
    "BPM_STEP",
    "SHOW_HALF_DOUBLE_TEMPO",
    "GENRE_RULES",
    "EXPLICIT_GENRE_MAP",
    "GENRE_PLAYLIST_ID_OVERRIDES",
    "NEIGHBOR_GENRES",
    "MISPLACED_GENRE_EXEMPT",
    "MISPLACED_GENRE_TRACK_ONLY",
    "USE_SPOTIFY_ARTIST_GENRES",
    "MAX_ARTIST_LOOKUPS",
    "USE_DEEZER_BPM_FALLBACK",
    "USE_ITUNES_GENRE_FALLBACK",
    "OUTPUT_DIR",
    "CACHE_FILE",
    "DECISIONS_FILE",
    "DATA_DIR",
    "DEEZER_URL",
    "ITUNES_URL",
    "LASTFM_URL",
    "RECCOBEATS_URL",
    "LASTFM_MIN_TAG_COUNT",
    "READ_SCOPES",
    "WRITE_SCOPES",
    "GENRE_PLAYLIST_NAMES",
    "_SPOTIFY_ID_RE",
    "_http",
    "_cache",
    "_cache_dirty",
    "load_cache",
    "save_cache",
    "quota_exit",
    "spotify_call",
    "validate_config",
    "get_client",
    "fetch_all",
    "_split_artist_title",
    "_parse_playlist_items",
    "get_playlist_tracks",
    "_liked_songs_failure",
    "get_liked_tracks",
    "find_year_playlists",
    "get_my_playlists",
    "_reccobeats_failures",
    "_reccobeats_get",
    "_RB_ID_RE",
    "get_tempos",
    "_deezer_failures",
    "_preview_urls",
    "_TITLE_SUFFIX_RE",
    "_clean_title",
    "_deezer_sanity_check",
    "get_deezer_bpm",
    "measure_preview_bpm",
    "bpm_bucket",
    "get_artist_genres",
    "_lastfm_fail_streak",
    "_lastfm_disabled",
    "_lastfm_get",
    "_extract_tags",
    "get_lastfm_track_tags",
    "get_lastfm_artist_tags",
    "_PREFIX_KWS",
    "_itunes_failures",
    "get_itunes_genre",
    "_CAT_RANK",
    "_KW_PATTERNS",
    "_tag_vote",
    "_tag_votes",
    "match_category",
    "_close_vote_alt",
    "_genre_cache",
    "resolve_genre",
    "run_external_test",
    "_norm",
    "match_local_tracks",
    "gather_real_data",
    "load_tag_mappings",
    "open_review_interface",
    "_local_file_uri"
]

def _ensure_package(module, pip_name=None):
    """Imports a package; if missing, installs it with pip first. Returns the module, or None when the install failed.
    The caller then runs without that feature."""
    import importlib
    import subprocess
    try:
        return importlib.import_module(module)
    except ImportError:
        pkg = pip_name or module
        if getattr(sys, "frozen", False):
            # Inside a PyInstaller .exe, sys.executable IS the bundled app, not a real Python interpreter.
            print(f"! '{pkg}' is missing from this build - it should have been bundled in with --hidden-import={module} when the .exe was built. Continuing without it.", file=sys.stderr)
            return None
        print(f"INFO: python package '{pkg}' missing -> installing it now ({sys.executable} -m pip install {pkg})")
        for extra in ([], ["--user"]):  # plain install first, then per-user (machines without admin rights)
            try:
                subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", *extra, pkg])
                return importlib.import_module(module)
            except (subprocess.CalledProcessError, OSError):
                continue
            except ImportError:
                break
        print(f"! automatic install of '{pkg}' failed -> install it manually: {sys.executable} -m pip install {pkg}", file=sys.stderr)
        return None


# ======================================================================================================================
# CONFIG - three zones, from "must edit" to "leave as-is":
#   ZONE 1 - FILL THESE  : the script cannot work without them.
#   ZONE 2 - CHECK THESE : depends on where and how you run (office/home, local files, quota, options).
#   ZONE 3 - FINE AS-IS  : behaviour tuning; only touch to change how the sorting THINKS.
# ======================================================================================================================

# ----------------------------------------------------------------------------------------------------------------------
# ZONE 1 - FILL THESE (Create the app on https://developer.spotify.com/dashboard, Redirect URI below; free Last.fm key
#                      on https://www.last.fm/api/account/create)
# ----------------------------------------------------------------------------------------------------------------------
SPOTIFY_CLIENT_ID     = "XXX" # /!\
SPOTIFY_CLIENT_SECRET = "XXX" # /!\
SOURCE_PLAYLIST_ID    = "XXX" # /!\ - the playlist to sort (22 chars from its link)
LASTFM_API_KEY        = "XXX" # /!\ - main genre source; "" = run without it

# ----------------------------------------------------------------------------------------------------------------------
# ZONE 2 - CHECK THESE (your environment and what this run should do)
# ----------------------------------------------------------------------------------------------------------------------
# Environment
PROXY_URL = ""                              # /!\ - office proxy; set "" when running from home
USE_SYSTEM_CERTS = True                     # Keep True on a corporate Windows machine (SSL-inspecting proxy)

# Local files (imported MP3s)
INCLUDE_LOCAL_FILES = True                  # Analyse the imported MP3s too (sorted by their name tags)
MATCH_LOCAL_FILES = True                    # Find their Spotify catalog equivalent (makes them actionable)
MAX_LOCAL_MATCH_PER_RUN = 700               # Spotify allows ~750 calls/day; 0 = no cap (one big run)
LOCAL_MATCH_PACE = 0.8                      # Seconds between two searches (Spotify punishes bursts)

# How much of your library this run looks at
INCLUDE_LIKED_SONGS = False                 # Also sort your "Liked Songs" library (adds ~1 call per 50 liked)
CHECK_ALL_PLAYLISTS_FOR_DUPLICATES = False  # Also checks every OTHER playlist YOU OWN (never ones you merely follow, like Discover Weekly)
ANALYZE_YEARLY_PLAYLISTS = False            # Detect your own "top songs of YYYY" playlists (any language) and export a genre/BPM trend by year

# Extra BPM effort
ANALYZE_DEEZER_PREVIEWS = False             # Measure missing BPMs from 30 s previews (slow; installs librosa)

# Output, review, and social
AUTO_OPEN_REVIEW = True                     # Open review_interface.html in your browser at the end of a run, already loaded with this run's report
EXPORT_LIBRARY_BACKUP = True                # Export every analysed track (title, artists, album, Spotify link, playlists it's in) to a CSV - your own copy, independent of Spotify
AUTO_FOLLOW_ARTISTS = False                 # Suggest following (via the review interface) every artist in your analysed library you don't follow yet

# TEST MODE - paste track links here (right-click a track > Share > Copy link) to test the external APIs
# without touching Spotify. Non-empty list = run the test only, then stop. Results are cached.
TEST_EXTERNES = [
    # "https://open.spotify.com/intl-fr/track/5KjJYrM3UXmvhqtQntrsJM?si=e7311651acdd4323",
]
SPOTIFY_TRACK_FALLBACK = True   # in test mode: 1 Spotify call to identify a ReccoBeats-unknown track

# ----------------------------------------------------------------------------------------------------------------------
# ZONE 3 - FINE AS-IS (how the sorting thinks; change only to tune the logic)
# ----------------------------------------------------------------------------------------------------------------------
SPOTIFY_REDIRECT_URI = "http://127.0.0.1:8888/callback" # must equal the Redirect URI in the app settings

BPM_STEP = 10                   # "80 bpm" bucket = 80.0 to 89.9 (truncated to the lower ten)
SHOW_HALF_DOUBLE_TEMPO = True   # a 154 bpm track can "feel" like 77: show the alternative bucket

# GENRE RULES - exact playlist names + lowercase keywords matched against the tags. One tag = one vote for the
# category with the LONGEST matching keyword; the most-voted category wins; this order breaks ties.
GENRE_RULES = [
    ("Latina",          ["latin", "reggaeton", "cumbia", "salsa", "bachata", "urbano", "corrido", "mariachi", "baile funk",
                         "funk carioca", "flamenco", "tango", "merengue", "dembow"]),
    ("Classique",       ["classical", "baroque", "romantic era", "opera", "orchestr", "chamber music", "requiem", "symphony"]),
    ("Jazz",            ["jazz", "bebop", "swing", "bossa nova", "big band"]),
    ("Reggae",          ["reggae", "dancehall", "ska", "dub", "roots", "rocksteady", "rock steady"]),
    ("Rap",             ["rap", "hip hop", "hip-hop", "hiphop", "trap", "drill", "grime", "boom bap", "phonk"]),
    ("Soul",            ["soul", "r&b", "rnb", "r&b/soul", "funk", "motown", "gospel", "doo wop"]),
    ("Electro",         ["electro", "edm", "house", "techno", "tekno", "hardtek", "tribe", "trance", "dubstep", "drum and bass",
                         "dnb", "jungle", "bass music", "future bass", "synthwave", "big room", "uk garage", "future garage",
                         "speed garage", "hardcore techno", "happy hardcore", "gabber", "frenchcore", "electro swing",
                         "drill and bass", "lofi", "lo-fi", "ambient", "chillout", "downtempo", "trip hop", "trip-hop",
                         "idm", "psytrance", "hardstyle"]),
    ("Dance",           ["dance pop", "dance", "disco", "eurodance", "hyperpop"]),
    ("Rock",            ["rock", "metal", "punk", "grunge", "emo", "screamo", "hardcore", "shoegaze", "garage", "blues",
                         "new wave", "rock opera", "alternative"]),
    ("SoundTrack",      ["soundtrack", "film score", "game music", "video game music", "video game", "anime", "ost",
                         "bande originale", "film music", "theme song", "composer"]),
    ("Française",       ["french", "chanson", "variete francaise", "variété française", "francoton"]),
    ("Pop",             ["pop", "indie", "singer-songwriter", "singer/songwriter", "synthpop", "britpop", "folk", "country",
                         "americana", "baroque pop", "chamber pop"]),   # catch-all, keep last
    ("Instrumental",    ["instrumental"]),  # performance style, not a musical style - deliberately last
]

# A tag written here goes straight to its category, before any keyword logic (perfect for the report's "unmapped tags" list,
# or to overrule a keyword decision).
EXPLICIT_GENRE_MAP = {
    "spain": "Latina",
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
MISPLACED_GENRE_EXEMPT = ["Pop", "Instrumental"]
# Only flag a track as misplaced when the verdict comes from the TRACK's own tags (artist tags are too weak).
MISPLACED_GENRE_TRACK_ONLY = True

# Spotify artist genres as last genre fallback: one call PER ARTIST = quota killer, off by default.
USE_SPOTIFY_ARTIST_GENRES = False
MAX_ARTIST_LOOKUPS = 200

USE_DEEZER_BPM_FALLBACK = True      # BPM by artist+title when ReccoBeats does not know the exact id
USE_ITUNES_GENRE_FALLBACK = True    # coarse genre by artist+title when Last.fm knows nothing

DATA_DIR = ".spotify_data"          # the cache and login token live here, out of the way of your source files
DECISIONS_FILE = "decisions.json"   # where --apply looks for your reviewed decisions (see below)
OUTPUT_DIR = "rapport_spotify"      # folder for the report (CSVs, report.json, review.html)

# the project's memory: do NOT delete it
CACHE_FILE = os.path.join(DATA_DIR, "cache_spotify_tri.json")

DEEZER_URL = "https://api.deezer.com"
ITUNES_URL = "https://itunes.apple.com/search"
LASTFM_URL = "https://ws.audioscrobbler.com/2.0/"
RECCOBEATS_URL = "https://api.reccobeats.com/v1/audio-features"
LASTFM_MIN_TAG_COUNT = 10   # ignore overly marginal Last.fm tags

READ_SCOPES = ("playlist-read-private playlist-read-collaborative"
            + (" user-library-read" if INCLUDE_LIKED_SONGS else "")
            + (" user-follow-read" if AUTO_FOLLOW_ARTISTS else ""))     # a plain run only ever needs this
WRITE_SCOPES = ("playlist-modify-private playlist-modify-public"
            + (" user-follow-modify" if AUTO_FOLLOW_ARTISTS else ""))   # only requested for --apply

# Proxy: requests reads HTTP_PROXY/HTTPS_PROXY from the environment by default
if PROXY_URL:
    os.environ["HTTP_PROXY"] = PROXY_URL
    os.environ["HTTPS_PROXY"] = PROXY_URL
    os.environ["NO_PROXY"] = "127.0.0.1,localhost"  # the OAuth callback stays local, it must not go through the proxy

# Certificates: corporate proxies with SSL inspection re-sign HTTPS traffic with their own CA, which Python's bundled certifi store does not trust
if USE_SYSTEM_CERTS:
    _ts = _ensure_package("truststore")
    if _ts:
        _ts.inject_into_ssl()   # Python now validates against the OS certificate store
    else:
        print("INFO: running without 'truststore' - needed behind an SSL-inspecting proxy, harmless otherwise.", file=sys.stderr)

# ======================================================================================================================
# END OF CONFIG - nothing to modify below
# ======================================================================================================================


GENRE_PLAYLIST_NAMES = [name for name, _ in GENRE_RULES]

_SPOTIFY_ID_RE = re.compile(r"[A-Za-z0-9]{22}")

_http = requests.Session()
if PROXY_URL:
    _http.proxies = {"http": PROXY_URL, "https": PROXY_URL}

_cache = {"playlists": {}, "tempos": {}, "lastfm": {}, "localmatch": {}, "bpm_corroboration": {}, "quota_first_hit": {}, "mood": {}}

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
        print(f"Cache loaded: {len(_cache['playlists'])} playlists, {len(_cache['tempos'])} BPMs, {len(_cache['lastfm'])} Last.fm lookups ({CACHE_FILE})")
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
        os.makedirs(os.path.dirname(CACHE_FILE) or ".", exist_ok=True)
        tmp = CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_cache, f, ensure_ascii=False)
        os.replace(tmp, CACHE_FILE)
        _cache_dirty = 0
    except OSError as e:
        print(f"! cache not saved: {e}", file=sys.stderr)

def quota_exit(context, retry_after=None):
    """Saves the cache then stops the current step, telling WHEN Spotify will accept requests again (taken from the
    Retry-After header of the 429 answer when Spotify provides it)."""
    save_cache(force=True)
    if retry_after:
        secs = int(retry_after)
        h, mn = divmod(secs // 60, 60)
        resume = time.localtime(time.time() + secs)
        day = " tomorrow" if resume.tm_mday != time.localtime().tm_mday else ""
        when = f"retry in {h}h{mn:02d} (around {time.strftime('%H:%M', resume)}{day})"
    else:
        first_hit = _cache.setdefault("quota_first_hit", {})
        now = time.time()
        if context not in first_hit:
            first_hit[context] = now
            when = "no retry time given - usually clears ~24h from now"
        else:
            elapsed_h = (now - first_hit[context]) / 3600
            resume = time.localtime(first_hit[context] + 24 * 3600)
            day = " tomorrow" if resume.tm_mday != time.localtime().tm_mday else ""
            when = f"first hit {elapsed_h:.0f}h ago, usually clears ~24h after that (around {time.strftime('%H:%M', resume)}{day})"
    print(f"Spotify quota hit ({context}): {when}. Saved - this step stops, the rest of the run continues.", file=sys.stderr)
    sys.exit(1)

def spotify_call(fn, context, attempts=4):
    """Runs one Spotify call, retrying on network hiccups AND on a SHORT-lived 429 (Retry-After <= 30s - Spotify's burst
    throttle, easy to mistake for the daily quota). A long or absent Retry-After means the daily quota is genuinely
    exhausted: save the cache and stop cleanly right away, no point waiting."""
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except spotipy.exceptions.SpotifyException as e:
            if e.http_status == 429:
                headers = getattr(e, "headers", None) or {}
                retry_after = headers.get("Retry-After")
                short = int(retry_after) if (retry_after or "").isdigit() else None
                if short is not None and short <= 30 and attempt < attempts:
                    print(f"  ! Spotify rate-limit on {context} -> pausing {short}s (attempt {attempt}/{attempts - 1}, looks like a short burst, not the daily quota)", file=sys.stderr)
                    time.sleep(short)
                    continue
                quota_exit(context, retry_after, headers)
            raise
        except requests.exceptions.RequestException as e:
            if attempt == attempts:
                save_cache(force=True)
                sys.exit(f"! could not reach Spotify during {context} after {attempts - 1} retries ({type(e).__name__}: {e}).\n"
                        f"  Check your internet connection, and PROXY_URL/USE_SYSTEM_CERTS in the CONFIG block if\n"
                        f"  you're on a restricted network (e.g. a corporate proxy) - progress so far is saved,\n"
                        f"  just run again once connectivity is back.")
            print(f"  ! flaky network on {context} ({type(e).__name__}) -> retry {attempt}/{attempts - 1} in 5 s", file=sys.stderr)
            time.sleep(5)

def validate_config(require_spotify=True):
    """Checks every credential/ID in the CONFIG block and returns ALL the problems at once, each with a plain-language fix.
    Secret values are never displayed."""
    problems = []
    if require_spotify:
        if not re.fullmatch(r"[0-9a-f]{32}", SPOTIFY_CLIENT_ID or ""):
            problems.append(
                f"SPOTIFY_CLIENT_ID looks wrong: the script found {SPOTIFY_CLIENT_ID!r},\n"
                f"    but a real Client ID is 32 letters/digits, like a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4.\n"
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

def get_client(apply_mode=False) -> spotipy.Spotify:
    problems = validate_config(require_spotify=True)
    if problems:
        sys.exit("The script cannot start: something in the CONFIG block needs fixing.\n\n  * "
                + "\n\n  * ".join(problems)
                + "\n\nFix the line(s) above in the CONFIG block at the top of the file, save, and run again.")
    scopes = READ_SCOPES + (" " + WRITE_SCOPES if apply_mode else "")
    proxies = {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else None

    # spotipy writes the token cache itself - the folder has to exist first
    os.makedirs(DATA_DIR, exist_ok=True)

    # status_retries=0: on a 429 (quota), spotipy raises immediately instead of sleeping for hours
    auth = SpotifyOAuth(client_id=SPOTIFY_CLIENT_ID, client_secret=SPOTIFY_CLIENT_SECRET, redirect_uri=SPOTIFY_REDIRECT_URI,
                        scope=scopes, open_browser=True, cache_path=os.path.join(DATA_DIR, ".spotify_token_cache"), proxies=proxies)

    # A saved login that doesn't cover what THIS run needs (typically: you logged in for a plain analysis, then ran --apply for the first time)
    # would otherwise be reused as-is and fail deep into the run with a raw "Insufficient client scope" 403.
    cached = auth.cache_handler.get_cached_token()
    if cached:
        missing = set(scopes.split()) - set((cached.get("scope") or "").split())
        if missing:
            print(f"! Your saved Spotify login is missing permission(s) this run needs: {', '.join(sorted(missing))}\n"
                f"  Removing the old login so Spotify asks for them (your browser will reopen) ...")
            try:
                os.remove(auth.cache_handler.cache_path)
            except OSError as e:
                print(f"  ! could not remove {auth.cache_handler.cache_path} ({e}) - delete it by hand if the browser does not reopen.", file=sys.stderr)
    return spotipy.Spotify(auth_manager=auth, proxies=proxies, retries=3, status_retries=0, requests_timeout=15)

def fetch_all(sp, first_page, context="pagination"):
    """Follows the "next page" links of a paginated Spotify answer and returns all items."""
    items, page = list(first_page["items"]), first_page
    while page["next"]:
        page = spotify_call(lambda p=page: sp.next(p), context)
        items.extend(page["items"])
    return items

def _split_artist_title(name):
    """Loosely-tagged local files often cram "Artist - Title" into just the title field, artist tag left blank.
    Splits on a dash SURROUNDED BY SPACES specifically so this only fires on the naming convention it's meant for.."""
    m = re.match(r"^\s*(.+?)\s+[-\u2013\u2014]\s+(.+?)\s*$", name)
    if m and len(m.group(1)) >= 2 and len(m.group(2)) >= 2:
        return m.group(1), m.group(2)
    return None, name

def _parse_playlist_items(items, skipped=None):
    """Turns raw playlist items into simple track dicts. Local files are kept (with a stable synthetic id) when
    INCLUDE_LOCAL_FILES is on; skipped items are counted with their reason."""
    tracks = []
    for it in items:
        t = it.get("track") or it.get("item") or {}
        if not t:
            if skipped is not None: skipped["empty item"] = skipped.get("empty item", 0) + 1
            continue
        if t.get("is_local"):
            if INCLUDE_LOCAL_FILES and t.get("name"):
                artists = ", ".join(a.get("name", "") for a in t.get("artists", []) if a.get("name"))
                name = t["name"]
                if not artists:
                    # Worth trying to recover the artist before giving up on it entirely.
                    guessed_artist, name = _split_artist_title(name)
                    if guessed_artist:
                        artists = guessed_artist
                artists = artists or "?"
                tracks.append({"id": f"local::{artists.lower()}::{name.lower()}", "name": name, "artists": artists, "artist_ids": [], "local": True, "album": ""})
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
                        "artist_ids": [a["id"] for a in t.get("artists", []) if a.get("id")],
                        "artist_pairs": [(a["id"], a["name"]) for a in t.get("artists", []) if a.get("id") and a.get("name")],
                        "album": (t.get("album") or {}).get("name", "")})
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
            page = spotify_call(lambda o=offset: sp._get(f"playlists/{playlist_id}/items", limit=100, offset=o), f"reading playlist {playlist_id}")
        except spotipy.exceptions.SpotifyException:
            new_ok = offset > 0 # failure on the very first page -> we will try the old endpoint
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
        print(f"  i {playlist_id}: API total={api_total}, items received={received}, kept={len(tracks)} (skipped: {detail}){note}")

    # 2) fallback: old /tracks endpoint via spotipy (pre-migration apps)
    if not new_ok and not tracks:
        try:
            page = spotify_call(lambda: sp.playlist_items(playlist_id, additional_types=("track",)), f"reading playlist {playlist_id} (old endpoint)")
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

def _liked_songs_failure(e, read_so_far):
    note = "quota exhausted" if getattr(e, "http_status", None) == 429 else type(e).__name__
    extra = f" -> keeping the {read_so_far} already read" if read_so_far else " -> continuing without them"
    print(f"  ! Liked Songs unavailable ({note}){extra}. This is an optional extra, not the main source -\n"
        f"    the rest of the analysis (your playlists, the source playlist) is unaffected.", file=sys.stderr)

def get_liked_tracks(sp):
    """Reads the user's "Liked Songs" library (50 per call). Cached, invalidated when the liked count changes.
    Deliberately NEVER stops the whole run (no spotify_call/quota_exit here): Liked Songs is an optional extra, so any failure
    just skips or truncates it and moves on, keeping everything else (already fetched from cache) intact."""
    global _cache_dirty
    try:
        first = sp.current_user_saved_tracks(limit=50)
    except (spotipy.exceptions.SpotifyException, requests.exceptions.RequestException) as e:
        _liked_songs_failure(e, 0)
        return []
    total = first.get("total", 0)
    cached = _cache["playlists"].get("__liked__")
    if cached and cached.get("snapshot_id") == str(total):
        return [dict(t) for t in cached["tracks"]]
    items, page = list(first.get("items", [])), first
    try:
        while page.get("next"):
            page = sp.next(page)
            items.extend(page.get("items", []))
    except (spotipy.exceptions.SpotifyException, requests.exceptions.RequestException) as e:
        tracks = _parse_playlist_items(items)
        _liked_songs_failure(e, len(tracks))
        return tracks   # partial read: not cached as "complete", so a later run finishes the job
    tracks = _parse_playlist_items(items)
    _cache["playlists"]["__liked__"] = {"snapshot_id": str(total), "tracks": [dict(t) for t in tracks]}
    _cache_dirty += 1
    save_cache(force=True)
    return tracks

def find_year_playlists(all_playlists, user_id, display_name):
    """Finds Spotify's own personalised yearly recap playlists in any language - a 4-digit year in the name, PLUS a description that personally addresses you."""
    if not display_name:
        return {}
    by_year = defaultdict(list)
    current_year = datetime.now().year
    for p in all_playlists:
        owned_by_you = p.get("owner", {}).get("id") == user_id
        made_for_you = display_name.lower() in (p.get("description") or "").lower()
        if not (owned_by_you or made_for_you):
            continue
        for m in re.finditer(r"(19|20)\d{2}", p["name"]):
            year = int(m.group())
            if 1990 <= year <= current_year:
                by_year[year].append({"id": p["id"], "name": p["name"]})
    return dict(sorted(by_year.items()))

def get_my_playlists(sp):
    first = spotify_call(lambda: sp.current_user_playlists(limit=50), "playlist listing")
    return fetch_all(sp, first, "playlist listing")

_reccobeats_failures = 0

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

_RB_ID_RE = re.compile(r"track/([A-Za-z0-9]+)")

def get_tempos(sp, tracks_by_id):
    """Finds the BPM of every track: cache, then Spotify, then ReccoBeats (by id), then Deezer (by artist+title), then
    optional local measurement of Deezer previews. One summary line per stage.
    Returns (tempos, measured_locally) - the second is the set of ids whose BPM came from measuring a preview ourselves
    rather than looking it up, which apply mode treats as lower-confidence."""
    global _cache_dirty
    measured_locally = set()

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
    skipped_local = len(missing) - len(rb_ids)  # local files: no Spotify ID, ReccoBeats can't look them up
    found, next_print = 0, 100
    if rb_ids:
        note = f" - {skipped_local} local file(s) skipped here (no Spotify ID), still counted in 'to fetch' below" if skipped_local else ""
        print(f"  * ReccoBeats : {len(rb_ids)} to try (batches of 40, ~0.5 s each){note}")
    for i in range(0, len(rb_ids), 40):
        content = _reccobeats_get("audio-features", rb_ids[i:i + 40])
        if content is None:
            if _reccobeats_failures >= 3:
                break   # circuit breaker: no point insisting on the remaining batches
            continue
        for item in content:
            m = _RB_ID_RE.search(item.get("href", ""))
            sid = m.group(1) if m else None
            if sid in tempos and item.get("tempo"):
                tempos[sid] = round(float(item["tempo"]), 1)
                _cache["tempos"][sid] = tempos[sid]
                _cache_dirty += 1
                found += 1
            if sid:
                mood = {k: item[k] for k in ("energy", "valence", "danceability", "acousticness") if k in item}
                if mood:
                    _cache.setdefault("mood", {})[sid] = mood

        checked = min(i + 40, len(rb_ids))  # A threshold, not "checked % 100 == 0": batches step by 40, so a plain modulo would silently print every 200 instead of every ~100.
        if checked >= next_print or checked == len(rb_ids):
            print(f"      ... {checked}/{len(rb_ids)} checked, {found} found")
            save_cache(force=True)
            next_print += 100
        else:
            save_cache()
        time.sleep(0.5) # politeness
    save_cache(force=True)
    missing = [tid for tid, v in tempos.items() if v is None]
    stage("ReccoBeats", found, len(missing), "stopped by circuit breaker" if _reccobeats_failures >= 3 else "")

    # --- Deezer (by artist+title: catches remaster/edit versions and local files)
    found = 0
    if missing and USE_DEEZER_BPM_FALLBACK:
        if not _deezer_sanity_check():
            print(f"  * Deezer     : skipped - even a known-good test query ('Bohemian Rhapsody') came back")
            print(f"      empty, so Deezer is unreachable or misbehaving right now rather than genuinely")
            print(f"      not knowing {len(missing)} tracks. Re-run later, or check your network/proxy.")
        else:
            print(f" -> Deezer     : {len(missing)} to try (artist+title search, ~0.6 s each)")
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
        print(f" -> previews   : {len(still2)} to try (30 s downloads + local tempo analysis, ~3 s each)")
        measured = 0
        for n, tid in enumerate(still2, 1):
            url = _preview_urls.get(tid)    # collected by the Deezer stage, keyed by this track's ID
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
                measured_locally.add(tid)
            if n % 50 == 0:
                print(f"      ... {n}/{len(still2)} analysed, {measured} measured")
                save_cache(force=True)
        stage("previews", measured, sum(1 for v in tempos.values() if v is None))
    save_cache(force=True)
    left = sum(1 for v in tempos.values() if v is None)
    print(f"=> BPM coverage: {len(tempos) - left}/{len(tempos)} ({left} will show as unknown BPM)")
    return tempos, measured_locally

_deezer_failures = 0

_preview_urls = {}

_TITLE_SUFFIX_RE = re.compile(r"\s+-\s+")

def _clean_title(title):
    """Drops version suffixes (" - Radio Edit", " - 2011 Remastered Version"...) before a name-based search."""
    return _TITLE_SUFFIX_RE.split(title)[0].strip()

def _deezer_sanity_check():
    """One known-good query before spending time on the whole batch (see get_tempos): if even a universally-known track returns nothing,
    Deezer is unreachable or misbehaving right now, not every track in the batch being genuinely unknown at once.
    Prints the real reason (SSL/cert issue, timeout, proxy refusal...) once, instead of a generic "unreachable" that hides what actually failed."""
    try:
        r = _http.get(f"{DEEZER_URL}/search", params={"q": 'artist:"Queen" track:"Bohemian Rhapsody"', "limit": 1}, timeout=15)
        r.raise_for_status()
        return bool(r.json().get("data"))
    except requests.exceptions.SSLError as e:
        print(f"      (reason: SSL certificate error - {e.__class__.__name__}. Likely your proxy's SSL "
            f"inspection cert isn't trusted; check USE_SYSTEM_CERTS and that 'truststore' installed OK.)")
        return False
    except (requests.RequestException, ValueError, KeyError) as e:
        print(f"      (reason: {e.__class__.__name__}: {e})")
        return False

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
            _preview_urls[tid] = d["preview"]   # kept for the optional local analysis stage, keyed by Spotify ID
        return None
    except (requests.RequestException, ValueError, KeyError) as e:
        _deezer_failures += 1
        if _deezer_failures >= 5:
            print("     ! Deezer: 5 straight failures -> stopping for this run", file=sys.stderr)
        return None
    finally:
        time.sleep(0.3)

def measure_preview_bpm(preview_url):
    """Downloads a 30 s Deezer preview and measures its tempo with librosa (installed automatically on first use).
    Beat trackers sometimes lock on half/double tempo: treat as an estimate."""
    import io
    librosa = _ensure_package("librosa")    # heavy: pulls numpy/scipy/soundfile, one-time ~1-2 min install
    if librosa is None:
        return "no-librosa"
    try:
        audio = _http.get(preview_url, timeout=20).content
        y, sr = librosa.load(io.BytesIO(audio), sr=22050, mono=True, duration=30)
        tempo = float(librosa.beat.tempo(y=y, sr=sr)[0])
        return round(tempo, 1) if tempo > 0 else None
    except Exception:
        return None

def bpm_bucket(tempo):
    """X0-X9 bucket: 87.3 -> 80, 154 -> 150 (truncated to the ten)."""
    return None if tempo is None else int(tempo // BPM_STEP) * BPM_STEP

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

_lastfm_fail_streak = 0

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
    if isinstance(raw, dict):   # Last.fm returns a dict (not a list) when there is a single tag
        raw = [raw]
    return [t["name"].lower() for t in raw if int(t.get("count", 0)) >= LASTFM_MIN_TAG_COUNT][:10]

def get_lastfm_track_tags(artist, title, verbose=False):
    """Last.fm tags of one specific TRACK. Empty list when unknown. Cached on disk; network failures are never cached,
    so they are retried next run."""
    global _cache_dirty
    key = f"{artist.lower()}||{title.lower()}"
    if key in _cache["lastfm"]:
        return _cache["lastfm"][key]
    if not LASTFM_API_KEY:
        return []
    data = _lastfm_get({"method": "track.gettoptags", "artist": artist, "track": title, "autocorrect": 1}, verbose=verbose)
    time.sleep(0.25)    # honour the Last.fm rate limit
    if data is None:
        return []
    tags = _extract_tags(data, "toptags")
    _cache["lastfm"][key] = tags
    _cache_dirty += 1
    save_cache()    # periodic save (every ~50 new lookups)
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

_PREFIX_KWS = {"orchestr", "electro"}

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
        r = _http.get(ITUNES_URL, params={"term": f"{artist.split(',')[0].strip()} {_clean_title(title)}", "media": "music", "limit": 1}, timeout=15)
        time.sleep(3)   # stay under Apple's unofficial ~20 req/min tolerance
        if r.status_code == 403:    # throttled: back off once, then count as failure
            time.sleep(30)
            _itunes_failures += 1
            return None

        r.raise_for_status()
        hits = r.json().get("results", [])
        genre = (hits[0].get("primaryGenreName") or "").lower() if hits else ""
        _itunes_failures = 0
        _cache["lastfm"][key] = genre   # "" cached too: a real miss should not be re-asked
        _cache_dirty += 1
        save_cache()
        return genre or None
    except (requests.RequestException, ValueError, KeyError):
        _itunes_failures += 1
        if _itunes_failures >= 5:
            print("     ! iTunes: 5 straight failures -> stopping for this run", file=sys.stderr)
        return None

_CAT_RANK = {cat: rank for rank, (cat, _) in enumerate(GENRE_RULES)}

_KW_PATTERNS = [(rank, cat, kw, re.compile(r"\b" + re.escape(kw) + ("" if kw in _PREFIX_KWS else r"\b"))) for rank, (cat, kws) in enumerate(GENRE_RULES) for kw in kws]

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

def _tag_votes(genre_list):
    """One vote per tag (see _tag_vote), tallied by category. {} when nothing matches."""
    votes = {}
    for g in genre_list:
        cat = _tag_vote(g)
        if cat:
            votes[cat] = votes.get(cat, 0) + 1
    return votes

def match_category(genre_list):
    """Turns a list of raw tags into one of your categories by majority vote. Ties are settled by the GENRE_RULES order. None when nothing matches."""
    votes = _tag_votes(genre_list)
    if not votes:
        return None
    return min(votes.items(), key=lambda kv: (-kv[1], _CAT_RANK[kv[0]]))[0]

def _close_vote_alt(votes):
    """The runner-up category when the vote was close enough to be worth a second look - lets the
    review interface offer a straight choice between the two instead of silently picking the winner."""
    if len(votes) < 2:
        return None
    ranked = sorted(votes.items(), key=lambda kv: (-kv[1], _CAT_RANK[kv[0]]))
    (_, top_count), (second_cat, second_count) = ranked[0], ranked[1]
    return second_cat if second_count >= top_count - 1 else None

_genre_cache = {}

def resolve_genre(track, artist_genres):
    """Finds a track's genre, most precise source first: Last.fm tags of the track, then of the main artist, then iTunes, then Spotify artist genres.
    Returns (category, raw tags, source, close_vote) - close_vote is only meaningful for "Last.fm (track)": True when
    the winning category only narrowly beat the runner-up."""
    if track["id"] in _genre_cache:
        return _genre_cache[track["id"]]

    # 1) Last.fm: per-TRACK tags (the "right info", per song)
    main_artist = track["artists"].split(",")[0].strip()
    tags = get_lastfm_track_tags(main_artist, track["name"])
    votes = _tag_votes(tags) if tags else {}
    cat = match_category(tags) if tags else None
    if cat:
        result = (cat, tags, "Last.fm (track)", _close_vote_alt(votes))
    else:
        # 2) Last.fm: MAIN ARTIST tags (obscure tracks rarely have track tags)
        atags = get_lastfm_artist_tags(main_artist)
        cat2 = match_category(atags) if atags else None
        if cat2:
            result = (cat2, atags, "Last.fm (artist)", False)
        else:
            # 3) iTunes Search: coarse primaryGenreName by artist+title (keyless, slow, last real lookup)
            ig = get_itunes_genre(main_artist, track["name"]) if USE_ITUNES_GENRE_FALLBACK else None
            cat3 = match_category([ig]) if ig else None
            if cat3:
                result = (cat3, [ig], "iTunes (track)", False)
            else:
                # 4) Fallback: Spotify genres of the track's artists
                sp_genres = [g for aid in track["artist_ids"] for g in artist_genres.get(aid, [])]
                cat4 = match_category(sp_genres) if sp_genres else None
                if cat4:
                    result = (cat4, sp_genres, "Spotify (artist)", False)
                else:
                    result = (None, tags or atags or ([ig] if ig else []) or sp_genres, "none", False)

    _genre_cache[track["id"]] = result
    return result

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
        print(f"0bis) Spotify fallback skipped (invalid Spotify credentials in CONFIG): {len(still)} track(s) will remain unidentified\n")
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
        print(f"0bis) Spotify fallback disabled (SPOTIFY_TRACK_FALLBACK=False): {len(still)} track(s) will remain unidentified\n")

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
    Hits AND misses are cached; the per-run cap counts search calls.
    If the daily quota runs out here, it stops cleanly with whatever was matched so far and lets the rest of the analysis
    (BPM/genre, both non-Spotify) run to completion - see the try/except below."""
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
    quota_hit = False
    try:
        for t in locals_:
            key = t.get("local_id") or t["id"]  # synthetic local::artist::title, stable across runs
            hit = _cache["localmatch"].get(key)
            if eligible(hit):
                if MAX_LOCAL_MATCH_PER_RUN and calls >= MAX_LOCAL_MATCH_PER_RUN:
                    continue
                main = t["artists"].split(",")[0].strip()
                title = _clean_title(t["name"])
                attempts = [f'track:"{title}" artist:"{main}"',
                            f'track:"{main}" artist:"{title}"', # swapped tags rescue
                            f"{title} {main}"]                  # free-text rescue
                hit = {"id": "", "name": "", "artists": "", "v": 2}
                aborted = False
                for mode, q in enumerate(attempts, 1):
                    if MAX_LOCAL_MATCH_PER_RUN and calls >= MAX_LOCAL_MATCH_PER_RUN:
                        aborted = True  # cap hit mid-track: not cached, cleanly redone next run
                        break
                    calls += 1
                    time.sleep(LOCAL_MATCH_PACE)    # paced: Spotify rate-limits bursts on a rolling window
                    if calls % 100 == 0:
                        print(f"      ... {calls} search calls, {matched} matched")
                    try:
                        res = spotify_call(lambda q=q: sp.search(q=q, type="track", limit=3), "local file matching")
                    except spotipy.exceptions.SpotifyException:
                        aborted = True
                        break
                    for cand in (res.get("tracks", {}) or {}).get("items", []):
                        if acceptable(cand, t["name"], main):
                            hit = {"id": cand["id"], "name": cand["name"], "mode": mode, "artists": ", ".join(a["name"] for a in cand.get("artists", [])), "v": 2}
                            break
                    if hit["id"]:
                        break
                if not aborted:
                    _cache["localmatch"][key] = hit
                    _cache_dirty += 1
                    save_cache()
            if hit and hit.get("id"):
                t["local_id"], t["id"], t["matched"] = key, hit["id"], True
                t["match_mode"] = hit.get("mode", 1)    # 1=normal fields, 2=swapped, 3=free text (weaker signal)
                t["artist_ids"] = []                    # unknown, and unused (genre goes through the name-based cascade anyway)
                matched += 1
    except SystemExit:
        # quota_exit() already saved the cache and printed its own message.
        quota_hit = True
        print(f"  ! Local-file matching stopped after {calls} search call(s) (quota) - {matched} matched so far;\n"
            f"    the rest of the analysis still runs on what's available, and the remaining "
            f"{sum(1 for t in locals_ if eligible(_cache['localmatch'].get(t.get('local_id') or t['id'])))} "
            f"file(s) resume next run.", file=sys.stderr)
    save_cache(force=True)
    if not quota_hit:
        print(f"  * local match: {matched}/{len(locals_)} matched on the Spotify catalog ({calls} search calls this run)")

def gather_real_data(sp, user_id, display_name):
    """Reads everything the analysis needs: your playlists, the source tracks, the local-file matches, the BPMs and the artist genres.
    Returns it all to main()."""
    # ---------------- The user's playlists ----------------
    print("Reading your playlists...")
    all_playlists = get_my_playlists(sp)
    snapshots = {p["id"]: p.get("snapshot_id") for p in all_playlists}  # for cache invalidation
    bpm_playlists, genre_playlists = {}, {} # {60: {"id","name"}} / {"Pop": {"id","name"}}
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
    contents = {}   # {playlist_name: [tracks]}
    for info in list(bpm_playlists.values()) + list(genre_playlists.values()):
        contents[info["name"]] = get_playlist_tracks(sp, info["id"], snapshots.get(info["id"]))
        print(f"  {info['name']:<12} : {len(contents[info['name']])} tracks")

    source_tracks = get_playlist_tracks(sp, SOURCE_PLAYLIST_ID, snapshots.get(SOURCE_PLAYLIST_ID))
    print(f"\nSource playlist: {len(source_tracks)} tracks\n")
    src_meta = next((p for p in all_playlists if p["id"] == SOURCE_PLAYLIST_ID), None)
    source_name = src_meta["name"] if src_meta else "Source playlist"
    content_ids = {info["name"]: info["id"] for info in list(bpm_playlists.values()) + list(genre_playlists.values())}
    contents[source_name] = list(source_tracks) # a COPY: duplicate-checking must ignore the liked-songs merge below
    content_ids[source_name] = SOURCE_PLAYLIST_ID

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
            raw = sp.playlist_items(SOURCE_PLAYLIST_ID, limit=3)    # old endpoint, raw response
            items = raw.get("items", [])
            print(f"!!   Old /tracks endpoint: total={raw.get('total', '?')}, page-1 items={len(items)}")
            for it in items[:2]:
                print(f"!!     item keys : {sorted(it.keys())}")
                print(f"!!     raw excerpt: {str(it)[:250]}")
        except Exception as e:
            print(f"!!   Old /tracks endpoint: error {e}")
        try:
            raw2 = sp._get(f"playlists/{SOURCE_PLAYLIST_ID}/items", limit=3)    # new 02/2026 endpoint
            items2 = raw2.get("items", [])
            print(f"!!   New /items endpoint: total={raw2.get('total', '?')}, page-1 items={len(items2)}")
            for it in items2[:2]:
                print(f"!!     item keys : {sorted(it.keys())}")
                print(f"!!     raw excerpt: {str(it)[:250]}")
        except Exception as e:
            print(f"!!   New /items endpoint: error {e}")
        print("!!   -> Send this diagnostic block as-is for analysis.\n")

    # ---------------- Local files: find their Spotify catalog equivalents ----------------
    # Runs BEFORE the optional add-ons below (Liked Songs, extra-playlist duplicate scan): local matching is the biggest
    # and most valuable backlog, so on a quota-limited day it gets first claim on what's left, instead of being starved
    # by add-ons that would otherwise spend the day's budget first.
    every = list(source_tracks)
    for lst in contents.values():
        every.extend(lst)
    match_local_tracks(sp, every)

    # ---------------- Optional add-ons: Liked Songs, then duplicate-scan of every other owned playlist ----------------
    # Both only add value on top of the core sort; neither should cost you progress on the above if the daily quota runs
    # out here - Liked Songs already degrades gracefully, and the loop below is wrapped the same way (a 429 keeps whatever
    # was already read and moves on rather than aborting the whole run).
    if INCLUDE_LIKED_SONGS:
        seen = {t["id"] for t in source_tracks}
        liked = []
        for t in get_liked_tracks(sp):
            # checked against 'seen' as it grows, not just its initial state.
            if t["id"] not in seen:
                seen.add(t["id"])
                liked.append(dict(t, liked=True))
        source_tracks.extend(liked)
        print(f"Liked songs added: {len(liked)} (not already in the source)")

    if CHECK_ALL_PLAYLISTS_FOR_DUPLICATES:
        already = set(content_ids.values())
        extra = [p for p in all_playlists if p["id"] not in already and p.get("owner", {}).get("id") == user_id]
        if extra:
            print(f"Also checking {len(extra)} other playlist(s) you own for duplicates...")
            read = 0
            try:
                for p in extra:
                    name = p["name"].strip() or p["id"]
                    contents[name] = get_playlist_tracks(sp, p["id"], snapshots.get(p["id"]))
                    content_ids[name] = p["id"]
                    read += 1
            except SystemExit:
                # an optional add-on: a 429 here keeps what was already read (still checked for duplicates)
                # and lets the rest of the analysis proceed normally, instead of ending the whole run.
                print(f"  ! Stopped after {read}/{len(extra)} (quota) - the rest will be checked next run.", file=sys.stderr)
    print()

    # ---------------- Yearly recap playlists (opt-in), fetched before the BPM/genre pass so their
    # tracks ride the SAME cascade for free instead of a separate, duplicated fetch ----------------
    year_contents = {}
    if ANALYZE_YEARLY_PLAYLISTS:
        year_playlists = find_year_playlists(all_playlists, user_id, display_name)
        if year_playlists:
            found = sum(len(v) for v in year_playlists.values())
            print(f"Yearly playlists found: {found} across {len(year_playlists)} year(s) -> "
                + ", ".join(f"{y} ({', '.join(p['name'] for p in ps)})" for y, ps in year_playlists.items()))
            for year, plist in year_playlists.items():
                tracks_for_year = []
                for p in plist:
                    tracks_for_year.extend(get_playlist_tracks(sp, p["id"], snapshots.get(p["id"])))
                year_contents[year] = tracks_for_year
            print()

    # ---------------- BPM + genres for ALL tracks (source + BPM/genre playlists + yearly playlists) ----------------
    all_tracks = {t["id"]: t for t in source_tracks}
    for lst in contents.values():
        for t in lst:
            all_tracks.setdefault(t["id"], t)
    for lst in year_contents.values():
        for t in lst:
            all_tracks.setdefault(t["id"], t)

    print("Fetching BPMs...")
    tempos, measured_locally = get_tempos(sp, all_tracks)

    print("Fetching artist genres...")
    artist_genres = get_artist_genres(sp, [aid for t in all_tracks.values() for aid in t["artist_ids"]])
    print()
    return bpm_playlists, genre_playlists, contents, content_ids, source_tracks, tempos, measured_locally, artist_genres, year_contents

def _local_file_uri(path):
    """A file:// URI that works with webbrowser.open() on both Windows and Unix-like systems."""
    abs_path = os.path.abspath(path).replace(os.sep, "/")
    return "file:///" + abs_path if not abs_path.startswith("/") else "file://" + abs_path

def open_review_interface(report_path=None, analysis_path=None):
    """Opens review_interface.html (Review + Analysis tabs) in your browser with whichever of report_path/analysis_path exist already loaded."""
    if not AUTO_OPEN_REVIEW:
        return

    own_path = sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__)
    script_dir = os.path.dirname(own_path)
    template_path = os.path.join(script_dir, "review_interface.html")
    if not os.path.exists(template_path):
        print(f"(review_interface.html not found next to {os.path.basename(own_path)} in {script_dir} - skipping auto-open; place it there to enable this.)")
        return
    try:
        with open(template_path, encoding="utf-8") as f:
            html = f.read()
        marker = "<script>\n"  # the page's single <script> block: inject just before it
        if marker not in html:
            print("(review_interface.html looks different than expected - skipping auto-open.)")
            return
        preload, loaded = "", []
        for var_name, path, label in [("__PRELOADED_REPORT__", report_path, "report"), ("__PRELOADED_ANALYSIS__", analysis_path, "analysis")]:
            if path and os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    text = f.read()
                safe_json = text.replace("</", "<\\/")  # never let a track title/artist close our <script> early
                preload += f"<script>window.{var_name} = {safe_json};</script>\n"
                loaded.append(label)
        rendered_path = os.path.join(OUTPUT_DIR, "review.html")
        with open(rendered_path, "w", encoding="utf-8") as f:
            f.write(html.replace(marker, preload + marker))
        webbrowser.open(_local_file_uri(rendered_path))
        print(f"  -> opened {rendered_path} in your browser (already loaded with this run's {' and '.join(loaded) or 'data'})")
    except OSError as e:
        print(f"(could not auto-open the review interface: {e})", file=sys.stderr)

def load_tag_mappings():
    """Merges any tag -> category assignments from a previous decisions.json export straight into EXPLICIT_GENRE_MAP for this run,
    so a tag you pinned in the interface takes effect starting with the very next analysis."""
    candidates = [DECISIONS_FILE, os.path.join(OUTPUT_DIR, DECISIONS_FILE), os.path.join(DATA_DIR, DECISIONS_FILE),
                os.path.join(os.path.expanduser("~"), "Downloads", DECISIONS_FILE)]
    for path in candidates:
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    mappings = json.load(f).get("tag_mappings", {})
            except (OSError, json.JSONDecodeError):
                return
            if mappings:
                EXPLICIT_GENRE_MAP.update(mappings)
                print(f"Loaded {len(mappings)} tag mapping(s) from {path}: " + ", ".join(f"'{t}'->{c}" for t, c in mappings.items()) + "\n")
            return

# ------------------------------------------------------------
#                            ,
#                       ,   /^\     ___
#                      /^\_/   `...'  /`
#                   ,__\    ,'     ~ (
#                ,___\ ,,    .,       \
#                 \___ \\\ .'.'   .-.  )
#                   .'.-\\\`.`.  '.-. (
#                  / (==== ."".  ( o ) \
#                ,/u  `~~~'|  /   `-'   )
#               "")^u ^u^|~| `""".  ~_ /
#                 /^u ^u ^\~\     ".  \\
#         _      /u^  u ^u  ~\      ". \\
#        ( \     )^ ^U ^U ^U\~\      ". \\
#       (_ (\   /^U ^ ^U ^U  ~|       ". `\
#      (_  _ \  )U ^ U^ ^U ^|~|        ". `\.
#     (_  = _(\ \^ U ^U ^ U^ ~|          ".`.;
#    (_ -(    _\_)U ^ ^ U^ ^|~|            ""
#    (_    =   ( ^ U^ U^ ^ U ~|
#    (_ -  ( ~  = ^ U ^U U ^|~/
#     (_  =     (_^U^ ^ U^ U /
#      (_-   ~_(/ \^ U^ ^U^,"
#       (_ =  _/   |^ u^u."
#        (_  (/    |u^ u.(
#         (__/     )^u^ u/
#                 /u^ u^(
#                |^ u^ u/
#                |u^ u^(       ____
#                |^u^ u(    .-'    `-,
#                \^u ^ \  / ' .---.  \
#                 \^ u^u\ |  '  `  ;  |
#                  \^ u^u:` . `-'  ;  |
#                   `-.^ u`._   _.'^'./
#                      "-.^.-```_=~._/
#                         `"------"'
