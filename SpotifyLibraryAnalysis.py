#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SpotifyLibraryAnalysis - genre/BPM/artist trends across your analysed library, optionally by year.

Always READ-ONLY: it never writes anything to Spotify, so it only ever asks for read access (see
SpotifyCore.READ_SCOPES) - there is no --apply here, nothing to approve.

Configuration lives in SpotifyCore.py (one CONFIG block shared with SpotifySortPlaylist.py) - the
settings this tool actually reads are ANALYZE_YEARLY_PLAYLISTS and EXPORT_LIBRARY_BACKUP; the rest
(local file matching, review options, etc.) only matter to SpotifySortPlaylist.py, but live in the same
place so there is one CONFIG to edit regardless of which tool you run.

HOW TO RUN
    python SpotifyLibraryAnalysis.py
Writes rapport_spotify/analysis.json and opens review_interface.html's Analysis tab for you and,
if EXPORT_LIBRARY_BACKUP is on, rapport_spotify/5_library_backup.csv.
"""

import csv
import json
import os
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone

from SpotifyCore import *

def _setup():
    """Everything up to and including gather_real_data(): config, login, the one read of your library.
    Split out from main() so a combined run (SpotifyLibrarian.py's "both" option) can reuse the SAME data SpotifySortPlaylist._setup()
    already fetched, instead of reading your library twice."""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    problems = validate_config(require_spotify=True)
    if problems:
        sys.exit("The script cannot start: something in the CONFIG block needs fixing.\n\n  * "
                 + "\n\n  * ".join(problems)
                 + "\n\nFix the line(s) above in the CONFIG block (SpotifyCore.py), save, and run again.")
    load_tag_mappings()
    load_cache()
    sp = get_client(apply_mode=False)   # this tool never writes anything - always read-only scope
    me = spotify_call(sp.current_user, "authentication")
    print(f"Logged in as: {me['display_name']} ({me['id']})")
    gathered = gather_real_data(sp, me["id"], me["display_name"])
    return sp, me, gathered

def _run_analysis(gathered, auto_open=True):
    """Everything after gather_real_data(), taking the already-fetched data as a parameter.
    So a combined run can save it once, at the end."""
    bpm_playlists, genre_playlists, contents, playlist_id_by_name, source_tracks, tempos, measured_locally, artist_genres, year_contents = gathered

    # The whole point of "analysis" is covering what you actually have, including tracks already correctly filed away long ago.
    all_lib_tracks = {t["id"]: t for t in source_tracks}
    for lst in contents.values():
        for t in lst:
            all_lib_tracks.setdefault(t["id"], t)
    for lst in year_contents.values():
        for t in lst:
            all_lib_tracks.setdefault(t["id"], t)

    if EXPORT_LIBRARY_BACKUP:
        backup_membership_sources = dict(contents)
        for year, lst in year_contents.items():
            backup_membership_sources[f"Top Songs {year}"] = lst

        path = os.path.join(OUTPUT_DIR, "5_library_backup.csv")
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(["Title", "Artists", "Album", "Spotify link", "BPM", "Genre", "Playlists"])
            w.writerows(build_library_backup_rows(all_lib_tracks, backup_membership_sources, tempos, artist_genres))
        print(f"  -> {path}")

    analysis_path = os.path.join(OUTPUT_DIR, "analysis.json")
    export_analysis(list(all_lib_tracks.values()), tempos, artist_genres, year_contents, analysis_path)
    if auto_open:
        # review_interface.html now covers both tabs (Review + Analysis) - reuse the same opener, passing
        # whichever report.json a previous sort run left behind too, so it isn't blank if you switch tabs.
        open_review_interface(os.path.join(OUTPUT_DIR, "report.json"), analysis_path)
    print("\nDone.")

def main():
    sp, me, gathered = _setup()
    _run_analysis(gathered)
    save_cache(force=True)

def _distribution_stats(tracks, tempos, artist_genres):
    """Genre split, BPM average/median/histogram, top artists, and a mood profile."""
    genre_counts = Counter()
    bpms = []
    bpm_bucket_counts = Counter()
    artist_counts = Counter()
    mood_sums = defaultdict(lambda: {"energy": 0.0, "valence": 0.0, "danceability": 0.0, "acousticness": 0.0, "n": 0})
    for t in tracks:
        genre, _, _, _ = resolve_genre(t, artist_genres)
        cat = genre or "Inclassable"
        genre_counts[cat] += 1
        tempo = tempos.get(t["id"])
        if tempo:
            bpms.append(tempo)
            bpm_bucket_counts[bpm_bucket(tempo)] += 1
        for _, aname in t.get("artist_pairs", []):
            artist_counts[aname] += 1
        mood = _cache.get("mood", {}).get(t["id"])
        if mood:
            entry = mood_sums[cat]
            for k in ("energy", "valence", "danceability", "acousticness"):
                entry[k] += mood.get(k, 0)
            entry["n"] += 1

    mood_by_genre = {}
    for cat, s in mood_sums.items():
        n = s["n"]
        mood_by_genre[cat] = {
            "energy": round(s["energy"] / n, 3), "valence": round(s["valence"] / n, 3),
            "danceability": round(s["danceability"] / n, 3), "acousticness": round(s["acousticness"] / n, 3),
            "coverage": n, "of": genre_counts[cat],
        }

    return {
        "track_count": len(tracks),
        "genre_counts": dict(genre_counts),
        "avg_bpm": round(sum(bpms) / len(bpms), 1) if bpms else None,
        "median_bpm": round(statistics.median(bpms), 1) if bpms else None,
        "bpm_histogram": dict(sorted(bpm_bucket_counts.items())),
        "top_artists": [{"name": n, "count": c} for n, c in artist_counts.most_common(8)],
        "mood_by_genre": mood_by_genre,
    }

def build_library_backup_rows(all_tracks, contents, tempos, artist_genres):
    """One row per analysed track: title, artists, album, a direct Spotify link, measured BPM/genre if
    known, and every playlist this run read that the track appears in. Your own copy, independent of
    Spotify - if a track is ever pulled from the catalog or a playlist gets mangled, this survives it.
    Membership only covers playlists this run actually looked at (source/BPM/genre/liked/extra/yearly,
    per your CONFIG) - a track sitting only in some other playlist you don't scan won't list it, since
    the script never read it there."""
    membership = defaultdict(list)
    for pname, lst in contents.items():
        for t in lst:
            membership[t["id"]].append(pname)
    rows = []
    for t in sorted(all_tracks.values(), key=lambda t: t["name"].lower()):
        genre, _, _, _ = resolve_genre(t, artist_genres)
        link = "(local file, no catalog link)" if t.get("local") else f"https://open.spotify.com/track/{t['id']}"
        rows.append([t["name"], t["artists"], t.get("album", ""), link, tempos.get(t["id"]) or "", genre or "", "; ".join(membership.get(t["id"], []))])
    return rows

def export_analysis(source_tracks, tempos, artist_genres, year_contents, path):
    """Writes analysis.json for review_interface.html's Analysis tab: verall genre/BPM/artist distribution across the analysed
    library, plus a per-year breakdown from any detected yearly recap playlists (opt-in via ANALYZE_YEARLY_PLAYLISTS)
    Empty when that option is off, no separate gating needed here."""
    overall = _distribution_stats(source_tracks, tempos, artist_genres)
    yearly = {str(year): _distribution_stats(tracks, tempos, artist_genres) for year, tracks in sorted(year_contents.items())}
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "overall": overall, "yearly": yearly}, f, ensure_ascii=False, indent=2)
    note = f", {len(yearly)} year(s)" if yearly else ""
    print(f"  -> {path} ({overall['track_count']} tracks analysed{note})")

if __name__ == "__main__":
    main()
