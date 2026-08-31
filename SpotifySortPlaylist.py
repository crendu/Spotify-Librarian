#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SpotifySortPlaylist - sorts your Spotify library by BPM and genre.

A plain run is READ-ONLY: it reads your playlists (via SpotifyCore), classifies every track, and
exports a report (4 CSVs + report.json) plus review_interface.html, already loaded with this run's
report. Only "--apply" (see README) writes anything to Spotify, and only after you have reviewed and
approved via the interface.

Configuration lives in SpotifyCore.py (one CONFIG block shared with SpotifyLibraryAnalysis.py).
The library backup CSV and analysis.json both moved entirely to SpotifyLibraryAnalysis.py - their value
is covering your WHOLE analysed library, not just this tool's sort queue.

HOW TO RUN
    1. Have Python 3. Missing libraries install themselves on first run.
    2. Fill the CONFIG block in SpotifyCore.py.
    3. python SpotifySortPlaylist.py   (first full run takes ~1 h; later runs are fast thanks to the cache)
    4. review_interface.html opens automatically with this run's report - decide, export your decisions,
    then "python SpotifySortPlaylist.py --apply" to write the approved changes.
"""

import csv
import json
import os
import re
import sys
import time
import webbrowser
from collections import Counter, defaultdict
from datetime import datetime, timezone

from SpotifyCore import *

REVIEW_GROUPS = {
    "bpm_disagreement":     ("BPM sources disagree on where this belongs",
                             "A second, independent lookup did not confirm the tempo that suggested moving this track."),
    "bpm_measured":         ("BPM measured from an audio preview, not looked up",
                             "No catalog had this tempo on file, so it was estimated by analysing a short preview."),
    "dup_variant":          ("Same title & artist, different file",
                             "Could be a genuinely different version (live, remaster) worth keeping separately."),
    "genre_artist_tag":     ("Genre guessed from the artist, not the track",
                             "Last.fm had nothing for the track itself, so the verdict comes from the artist's overall style."),
    "genre_close_vote":     ("Genre call was close",
                             "Two categories were nearly tied on the track's own tags."),
    "genre_itunes":         ("Genre from iTunes (coarse match)",
                             "Last.fm knew nothing about this track or its artist; iTunes's broad category is what's left."),
    "genre_unclassifiable": ("Genre truly unknown - every source came up empty",
                             "Last.fm (track and artist), iTunes, and Spotify all had nothing usable - parked in 'Inclassable' for you to place by ear, rather than silently dropped."),
    "local_fuzzy_match":    ("Local file matched by a loose search",
                             "The title/artist tags in the file looked unusual, so the catalog match is less certain."),
    "misplaced_genre":      ("Genre disagrees with where you put it",
                             "A second opinion on your own curation, not a verdict - your placement may well be the right one."),
    "new_artist_follow":    ("Artist in your library you don't follow yet",
                             "Following helps Spotify's recommendations and keeps you notified of new releases."),
}

def _addable_id(t):
    """The catalog id usable to ADD this track via the API, or None when it cannot be added at all
    (an imported MP3 with no matched catalog equivalent)."""
    return None if (t.get("local") and not t.get("matched")) else t["id"]

def _local_override(t):
    """A shaky local-file match makes the WHOLE track uncertain, regardless of how solid its BPM/genre
    verdict looks - we might be tagging the wrong song entirely. Returns (tier, group) or None."""
    if t.get("local") and t.get("matched") and t.get("match_mode", 1) != 1:
        return "review", "local_fuzzy_match"
    return None

def _corroborate_bpm(tid, cached_tempo, artist, title):
    """Before trusting ANY BPM-based action, cross-checks the cached tempo (from whichever source produced it first - ReccoBeats usually)
    against an INDEPENDENT second opinion (Deezer, by name).
    A single bad reading from one source should never be enough to file (or move) a track with silent confidence.

    Returns True when there's no contradiction (agreement, half/double, or no second opinion yet),
    False when a genuine disagreement means this action should go to a human instead of happening automatically."""
    global _cache_dirty
    if tid in _cache["bpm_corroboration"]:
        return _cache["bpm_corroboration"][tid]
    second = get_deezer_bpm(artist, title)
    if not second:
        return True  # nothing to contradict with yet - proceed on the original source's word, retry next run
    b1, b2 = bpm_bucket(cached_tempo), bpm_bucket(second)
    agree = b1 == b2 or bpm_bucket(cached_tempo / 2) == b2 or bpm_bucket(cached_tempo * 2) == b2
    _cache["bpm_corroboration"][tid] = agree
    _cache_dirty += 1
    return agree

def classify_bpm(t, tempo, measured_locally):
    """(tier, group) for a BPM-based add. Confident unless the tempo was measured (not looked up), or a second, independent source doesn't confirm it."""
    if t["id"] in measured_locally:
        return "review", "bpm_measured"
    if not _corroborate_bpm(t["id"], tempo, t["artists"], t["name"]):
        return "review", "bpm_disagreement"
    return "confident", None

def _row_key(a):
    """A key that uniquely identifies ONE ROW in the review interface."""
    if a["type"] == "remove_duplicate":
        return f"{a['track_id']}::{a['playlist_name']}::{a.get('position', '')}"
    if a["type"] == "move_track":
        return f"{a['track_id']}::{a['from_playlist_name']}"
    return a["track_id"]

def classify_genre(genre_src, close_vote):
    """(tier, group) for a genre-based add/move, from the precision of its source."""
    if genre_src == "Last.fm (track)":
        return ("review", "genre_close_vote") if close_vote else ("confident", None)
    if genre_src in ("Last.fm (artist)", "Spotify (artist)"):
        return "review", "genre_artist_tag"
    if genre_src == "iTunes (track)":
        return "review", "genre_itunes"
    return "confident", None

def _slug(label):
    return re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")

def get_unfollowed_artists(sp, artist_ids):
    """Returns the subset of artist_ids not already followed."""
    ids = list(artist_ids)
    unfollowed = []
    for i in range(0, len(ids), 40):
        chunk = ids[i:i + 40]
        uris = ",".join(f"spotify:artist:{aid}" for aid in chunk)
        result = spotify_call(lambda uris=uris: sp._get("me/library/contains", uris=uris), "checking followed artists")
        unfollowed.extend(aid for aid, already in zip(chunk, result) if not already)
    return unfollowed

def follow_artists(sp, artist_ids):
    """Follows the given artists via PUT /me/library (spotify:artist:{id} URIs). Chunked at 40, the endpoint's documented maximum."""
    for i in range(0, len(artist_ids), 40):
        chunk = artist_ids[i:i + 40]
        uris = ",".join(f"spotify:artist:{aid}" for aid in chunk)
        spotify_call(lambda uris=uris: sp._put("me/library", uris=uris), "following artists")

def _track_label(t):
    """A human-readable "Title - Artist" label. Falls back to the raw ID when Spotify has neither. Never printed as a confusing blank line."""
    if t.get("name") or t.get("artists"):
        return f"{t['name']} - {t['artists']}"
    return f"(unnamed track, id {t.get('id', '?')})"

MAX_TRACKS_PER_GROUP_IN_REPORT = 5000

def export_report(actions, to_create_bpm, to_create_genre, path, unknown_genre_tags=None, unmapped_by_track=None):
    """Writes report.json: a human-friendly, grouped view of every pending action, for review_interface.html - the FULL track
    list per review group (not just a sample), so the interface can show, filter, and sort every one of them, not only a preview."""
    confident_gated = defaultdict(int)
    for a in actions:
        if a["tier"] == "confident" and a.get("needs_create") and a.get("unlock_id"):
            confident_gated[a["unlock_id"]] += 1
    unlocks = ([{"id": f"bpm_{b}", "label": f"{b} bpm", "count": len(v), "confident": confident_gated[f"bpm_{b}"]} for b, v in sorted(to_create_bpm.items())]
            + [{"id": f"genre_{_slug(g)}", "label": g, "count": len(v), "confident": confident_gated[f"genre_{_slug(g)}"]} for g, v in sorted(to_create_genre.items())])
    confident_count = sum(1 for a in actions if a["tier"] == "confident" and not a.get("needs_create"))
    by_group = defaultdict(list)
    for a in actions:
        if a["tier"] == "review":
            by_group[a["group"]].append(a)
    review_groups = []
    for gid, acts in by_group.items():
        label, why = REVIEW_GROUPS[gid]
        tracks = []
        for a in acts[:MAX_TRACKS_PER_GROUP_IN_REPORT]:
            if a["type"] == "follow_artist":
                detail = "Follow"
            elif a["type"] == "add_track":
                # kind disambiguates the common case of the SAME track needing both a BPM and a genre action under the same uncertainty reason.
                origin = f"{a['from_playlist_name']}: " if a.get("from_playlist_name") else ""
                detail = f"{origin}-> {a['target']} ({a.get('kind', '?')})"
            else:
                # These actions needs BOTH ends to make sense at a glance.
                if a["type"] == "move_track":
                    detail = f"{a['from_playlist_name']} -> {a['to_playlist_name']}"
                elif a["type"] == "remove_duplicate":
                    detail = f"{a['playlist_name']}: remove"
                    if a.get("total_copies"):
                        detail += f" (extra copy, {a['total_copies']} total)"
                else:
                    detail = f"-> {a.get('to_playlist_name', 'remove')}"
            track = {"id": _row_key(a), "title": a["track_name"], "artist": a["track_artist"], "detail": detail}
            if a["type"] == "follow_artist":
                track["count"] = a.get("count", 0)
            if a["type"] == "add_track":
                track["target"] = a["target"]
            if a.get("alt_target"):
                # Lets the interface offer a straight toggle between the two instead of silently picking one.
                track["alt_target"] = a["alt_target"]
            tracks.append(track)
        review_groups.append({"id": gid, "label": label, "why": why, "total_count": len(acts), "tracks": tracks})

    _CATEGORY = {"genre_artist_tag": 0, "genre_itunes": 0, "genre_close_vote": 0, "genre_unclassifiable": 0, "misplaced_genre": 0,
                "bpm_measured": 1, "bpm_disagreement": 1, "dup_variant": 2, "local_fuzzy_match": 3, "new_artist_follow": 4}

    review_groups.sort(key=lambda g: (_CATEGORY.get(g["id"], 9), -g["total_count"]))

    # One block per real TRACK (not per tag): several unmapped tags on the same track would otherwise show as unrelated rows that just happen to share an example.
    # Each tag still carries how many tracks total have it, so a tag worth pinning (appears widely) is easy to spot.
    tag_counts = {tag: len(titles) for tag, titles in (unknown_genre_tags or {}).items()}
    unmapped_tracks = []
    for entry in sorted((unmapped_by_track or []), key=lambda e: -len(e["tags"]))[:40]:
        unmapped_tracks.append({"track": entry["track"],
                                "tags": [{"tag": g, "count": tag_counts.get(g, 1)} for g in entry["tags"]]})
    categories = [cat for cat, _ in GENRE_RULES]

    with open(path, "w", encoding="utf-8") as f:
        json.dump({"generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "confident_count": confident_count,
                "unlocks": unlocks, "review_groups": review_groups, "unmapped_tracks": unmapped_tracks, "categories": categories},
                f, ensure_ascii=False, indent=2)
    print(f"  -> {path} ({confident_count} confident, {sum(g['total_count'] for g in review_groups)} to review)")

def _parse_iso(ts):
    """Parses the ISO-ish timestamps this project uses - Python's own export, or a browser's
    toISOString() - into a UTC epoch, so ages and before/after comparisons are reliable regardless
    of the exact format (with/without milliseconds, with/without a trailing Z). None if unparseable."""
    if not ts:
        return None
    s = ts.rstrip("Z")
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    return None

def _age_str(epoch):
    hours = (time.time() - epoch) / 3600
    if hours < 1:
        return "less than an hour ago"
    if hours < 24:
        n = int(hours)
        return f"{n} hour{'s' if n != 1 else ''} ago"
    n = int(hours // 24)
    return f"{n} day{'s' if n != 1 else ''} ago"

def warn_if_stale(decisions):
    """Non-blocking heads-up (the apply confirmation remains the real gate): says how old the
    decisions are, and flags it clearly if a newer analysis report exists than the one reviewed."""
    dec_time = _parse_iso(decisions.get("generated_at"))
    if dec_time:
        print(f"Decisions exported: {_age_str(dec_time)}.")
    report_path = os.path.join(OUTPUT_DIR, "report.json")
    if not os.path.exists(report_path):
        return
    try:
        with open(report_path, encoding="utf-8") as f:
            report_time = _parse_iso(json.load(f).get("generated_at"))
    except (json.JSONDecodeError, OSError):
        report_time = None
    if report_time and dec_time and report_time > dec_time:
        print(f"\n! A newer analysis report exists than the one these decisions were reviewed against ({_age_str(report_time)}).")
        print("  Your approvals still apply correctly (matched by group and track, not by a frozen snapshot)")
        print("  - but if your library or the sorting rules changed meaningfully since, it's worth reviewing again:")
        print("  re-run the analysis, reload review_interface.html, and export fresh decisions before continuing.\n")

def load_decisions():
    """Finds decisions.json: next to the script, in the report folder, in .spotify_data, or in your Downloads
    (wherever review_interface.html's "Export decisions" button saved it)."""
    import glob
    base, ext = os.path.splitext(DECISIONS_FILE)   # "decisions", ".json"
    candidate_dirs = [".", OUTPUT_DIR, DATA_DIR, os.path.join(os.path.expanduser("~"), "Downloads")]
    existing = []
    for d in candidate_dirs:
        existing.extend(glob.glob(os.path.join(d, f"{glob.escape(base)}*{ext}")))
    if not existing:
        sys.exit(f"ERROR: no '{DECISIONS_FILE}' (or a browser-renamed copy of it) found next to the script,\n"
                f"in {OUTPUT_DIR}/, in {DATA_DIR}/, or in Downloads.\n"
                f"Open review_interface.html, load {OUTPUT_DIR}/report.json, decide, click 'Export decisions',\n"
                f"and make sure the downloaded file lands in one of those places, then run --apply again.")
    path = max(existing, key=os.path.getmtime)
    if len(existing) > 1:
        others = ", ".join(p for p in existing if p != path)
        print(f"Decisions loaded from: {path} (most recently modified - also found, but older, at: {others})\n")
    else:
        print(f"Decisions loaded from: {path}\n")
    with open(path, encoding="utf-8") as f:
        return json.load(f)

def filter_actions(actions, decisions):
    """Keeps: every CONFIDENT action; REVIEW actions per their group's approve/skip decision and sample-
    level exceptions; and only add actions whose target playlist's creation was actually approved."""
    approved_unlocks = {uid for uid, v in decisions.get("unlocks", {}).items() if v}
    review = decisions.get("review", {})
    genre_choice = decisions.get("genre_choice", {})
    kept = []
    for a in actions:
        # A close genre call redirected to its runner-up: alt_target always points at an EXISTING playlist
        if a.get("alt_target") and genre_choice.get(_row_key(a)) == "alt":
            a = {**a, "target": a["alt_target"], "needs_create": False, "unlock_id": None}
        if a.get("needs_create") and a.get("unlock_id") not in approved_unlocks:
            continue  # that playlist wasn't approved for creation - nothing to add it to (yet)
        if a["tier"] == "confident":
            kept.append(a)
            continue
        grp = review.get(a["group"], {})
        default, exceptions = grp.get("default", "skipped"), set(grp.get("exceptions", []))

        # Matched by the SAME per-row key used to build report.json (see _row_key).
        included = (_row_key(a) not in exceptions) if default == "approved" else (_row_key(a) in exceptions)
        if included:
            kept.append(a)
    return kept

def apply_actions(sp, actions, to_create_bpm, to_create_genre, playlist_id_by_name):
    """Shows exactly what is about to change, asks for ONE confirmation, then writes it to Spotify."""
    decisions = load_decisions()
    warn_if_stale(decisions)
    final = filter_actions(actions, decisions)
    approved_unlocks = {uid for uid, v in decisions.get("unlocks", {}).items() if v}
    to_create = ([(f"bpm_{b}", f"{b} bpm") for b in sorted(to_create_bpm) if f"bpm_{b}" in approved_unlocks]
                + [(f"genre_{_slug(g)}", g) for g in sorted(to_create_genre) if f"genre_{_slug(g)}" in approved_unlocks])

    adds = [a for a in final if a["type"] == "add_track"]
    moves = [a for a in final if a["type"] == "move_track"]
    removes = [a for a in final if a["type"] == "remove_duplicate"]
    follows = [a for a in final if a["type"] == "follow_artist"]

    print("=" * 100)
    print("APPLY PREVIEW - nothing has been sent to Spotify yet")
    print("=" * 100)
    if to_create:
        print(f"\nPlaylists to create ({len(to_create)}):")
        for _, label in to_create:
            print(f"  - {label}")
    if adds:
        print(f"\nTracks to add ({len(adds)}):")
        for target, n in sorted(Counter(a["target"] for a in adds).items(), key=lambda kv: -kv[1]):
            print(f"  - {n:>5}  -> '{target}'")
    if moves:
        print(f"\nTracks to move ({len(moves)}):")
        for (frm, to), n in sorted(Counter((a["from_playlist_name"], a["to_playlist_name"]) for a in moves).items(), key=lambda kv: -kv[1]):
            print(f"  - {n:>5}   '{frm}' -> '{to}'")
    if removes:
        print(f"\nDuplicate tracks to remove ({len(removes)}):")
        for pname, n in sorted(Counter(a["playlist_name"] for a in removes).items(), key=lambda kv: -kv[1]):
            print(f"  - {n:>5}   from '{pname}'")
    if follows:
        print(f"\nArtists to follow ({len(follows)}):")
        for a in follows[:15]:
            print(f"  - {a['track_name']}")
        if len(follows) > 15:
            print(f"  ... and {len(follows) - 15} more")

    total = len(adds) + len(moves) + len(removes) + len(to_create) + len(follows)
    if total == 0:
        print("\nNothing to apply - either everything is already sorted, or decisions.json approved nothing.")
        return
    print(f"\n{len(adds)} add(s), {len(moves)} move(s), {len(removes)} removal(s), {len(follows)} follow(s), {len(to_create)} playlist(s) to create.")
    if input("\nType 'yes' to write these changes to your Spotify account, anything else to cancel: ").strip().lower() != "yes":
        print("Cancelled - nothing was changed.")
        return

    print("\nApplying changes...")
    name_to_id = dict(playlist_id_by_name)
    for uid, label in to_create:
        print(f"  Creating playlist '{label}'...")
        pl = spotify_call(lambda label=label: sp.current_user_playlist_create(label, public=False), f"create playlist {label}")
        name_to_id[label] = pl["id"]

    # additions (a track waiting for a new playlist AND the "to" side of a move both land here)
    add_by_target = defaultdict(list)
    for a in adds:
        add_by_target[a["target"]].append(a["track_id"])
    for a in moves:
        add_by_target[a["to_playlist_name"]].append(a["track_id"])
    for target, ids in add_by_target.items():
        pid = name_to_id.get(target)
        if not pid:
            print(f"  ! cannot add to '{target}': no playlist id (was its creation skipped?)")
            continue
        for i in range(0, len(ids), 100):
            spotify_call(lambda pid=pid, chunk=ids[i:i + 100]: sp.playlist_add_items(pid, chunk), f"add to {target}")
        print(f"  Added {len(ids)} track(s) to '{target}'")

    # removals: duplicates AND the "from" side of a move, merged per playlist and removed by HIGHEST position first,
    # so deleting one entry never shifts the position of another still to be removed.
    removal_by_playlist = defaultdict(list)
    for a in removes:
        removal_by_playlist[a["playlist_id"]].append((a["position"], a["track_id"], a["playlist_name"]))
    for a in moves:
        removal_by_playlist[a["from_playlist_id"]].append((a["from_position"], a["track_id"], a["from_playlist_name"]))
    for pid, entries in removal_by_playlist.items():
        entries.sort(key=lambda e: -e[0])
        pname = entries[0][2]
        for i in range(0, len(entries), 100):
            items = [{"uri": tid, "positions": [pos]} for pos, tid, _ in entries[i:i + 100]]
            spotify_call(lambda pid=pid, items=items: sp.playlist_remove_specific_occurrences_of_items(pid, items), f"remove from {pname}")
        print(f"  Removed {len(entries)} track(s) from '{pname}'")

    if follows:
        follow_artists(sp, [a["track_id"] for a in follows])
        print(f"  Followed {len(follows)} artist(s)")
    print("\nDone. Your Spotify account has been updated.")

def suggest_artists_to_follow(sp, all_lib_tracks, actions, apply_mode):
    """Section 0 (opt-in, AUTO_FOLLOW_ARTISTS): every artist across the analysed library you don't
    already follow becomes a "follow_artist" review action - appended straight into `actions`, exactly
    like every other kind of action, so it flows through the same review/apply pipeline."""
    if not AUTO_FOLLOW_ARTISTS:
        return
    artist_map = {}  # artist_id -> [name, track_count]
    for t in all_lib_tracks.values():
        for aid, aname in t.get("artist_pairs", []):
            entry = artist_map.setdefault(aid, [aname, 0])
            entry[1] += 1
    if artist_map:
        unfollowed = get_unfollowed_artists(sp, list(artist_map))
        for aid in unfollowed:
            name, count = artist_map[aid]

            # Spotify sometimes returns a near-blank name (seen: a bare ".") for an artist whose catalog metadata is largely gone.
            display_name = name if (name and name.strip() not in ("", ".")) else f"(unnamed artist, id {aid})"
            actions.append({"type": "follow_artist", "tier": "review", "group": "new_artist_follow",
                            "track_id": aid, "track_name": display_name,
                            "track_artist": f"{count} track{'s' if count != 1 else ''} in your library",
                            "needs_create": False, "unlock_id": None, "count": count})
        if not apply_mode:
            print(f"Artists you don't follow yet: {len(unfollowed)} (out of {len(artist_map)} in your library)\n")

def suggest_additions(source_tracks, tempos, bpm_playlists, genre_playlists, ids_in, measured_locally, artist_genres, actions,
                    to_create_bpm, to_create_genre, unknown_genre_tags, unmapped_by_track, apply_mode, source_name):
    """Section 1: for every source-playlist track, works out its BPM and genre destination (existing
    playlist, a new one, or - genre only - the "Inclassable" catch-all when nothing matched at all),
    appends the resulting add_track action(s) to `actions`, and returns the CSV rows for
    1_suggested_additions.csv. to_create_bpm/to_create_genre/unknown_genre_tags/unmapped_by_track are
    filled in as a side effect (read by later sections and by the CSV/report export)."""
    rows_add = []
    if not apply_mode:
        print("=" * 100)
        print("1) SOURCE PLAYLIST TRACKS -> SUGGESTED ADDITIONS")
        print("=" * 100)
    for t in source_tracks:
        tempo = tempos.get(t["id"])
        bucket = bpm_bucket(tempo)
        genre, raw_genres, genre_src, close_vote = resolve_genre(t, artist_genres)
        aid = _addable_id(t)            # None only for an unmatched local file: cannot be added via the API
        local_tier = _local_override(t) # a shaky local match makes the WHOLE track uncertain

        # --- BPM target (console shows only the measured tempo; the full action lives in the CSV)
        if tempo is None:
            bpm_action = "unknown BPM (check manually)"
        elif bucket in bpm_playlists:
            pname = bpm_playlists[bucket]["name"]
            already = t["id"] in ids_in.get(pname, set())
            bpm_action = f"already in '{pname}'" if already else f"ADD to '{pname}'"
            if aid and not already:
                tier, group = local_tier or classify_bpm(t, tempo, measured_locally)
                actions.append({"type": "add_track", "kind": "bpm", "tier": tier, "group": group, "track_id": aid, "track_name": t["name"],
                                "track_artist": t["artists"], "target": pname, "needs_create": False, "unlock_id": None, "from_playlist_name": source_name})
        else:
            bpm_action = f"CREATE playlist '{bucket} bpm' then add this track"
            to_create_bpm[bucket].append(_track_label(t))
            if aid:
                tier, group = local_tier or classify_bpm(t, tempo, measured_locally)
                actions.append({"type": "add_track", "kind": "bpm", "tier": tier, "group": group, "track_id": aid, "track_name": t["name"],
                                "track_artist": t["artists"], "target": f"{bucket} bpm", "needs_create": True, "unlock_id": f"bpm_{bucket}", "from_playlist_name": source_name})

        alts = []
        if SHOW_HALF_DOUBLE_TEMPO and tempo:
            alts = [f"{x} bpm" for x in {bpm_bucket(tempo / 2), bpm_bucket(tempo * 2)} - {bucket} if x in bpm_playlists]

        # --- Genre target
        src_short = genre_src.replace("(", "").replace(")", "")
        if genre is None:
            genre_action, g_tok = f"unidentified genre ({'; '.join(raw_genres[:3]) or 'no Spotify or Last.fm info'})", "genre ?"
            for g in raw_genres:
                unknown_genre_tags[g].append(f"{t['name']} - {t['artists']}")
            if raw_genres:
                unmapped_by_track.append({"track": _track_label(t), "tags": list(raw_genres)})

            # Every source in the cascade came up empty - park it in a real "Inclassable" playlist instead of the track vanishing with no action at all.
            if aid:
                tier, group = local_tier or ("review", "genre_unclassifiable")
                if INCLASSABLE in genre_playlists:
                    pname = genre_playlists[INCLASSABLE]["name"]
                    already = t["id"] in ids_in.get(pname, set())
                    if not already:
                        actions.append({"type": "add_track", "kind": "genre", "tier": tier, "group": group, "track_id": aid,
                                        "track_name": t["name"], "track_artist": t["artists"], "target": pname,
                                        "needs_create": False, "unlock_id": None, "from_playlist_name": source_name})
                else:
                    to_create_genre[INCLASSABLE].append(_track_label(t))
                    actions.append({"type": "add_track", "kind": "genre", "tier": tier, "group": group, "track_id": aid,
                                    "track_name": t["name"], "track_artist": t["artists"], "target": INCLASSABLE,
                                    "needs_create": True, "unlock_id": f"genre_{_slug(INCLASSABLE)}", "from_playlist_name": source_name})
        elif genre in genre_playlists:
            pname = genre_playlists[genre]["name"]
            already = t["id"] in ids_in.get(pname, set())
            base = f"already in '{pname}'" if already else f"ADD to '{pname}'"
            genre_action = f"{base} [source: {genre_src}]"
            g_tok = ("=" if already else "+") + pname
            if aid and not already:
                tier, group = local_tier or classify_genre(genre_src, close_vote)

                # The runner-up only matters when it ALREADY has a real playlist.
                alt_target = genre_playlists[close_vote]["name"] if close_vote and close_vote in genre_playlists else None
                actions.append({"type": "add_track", "kind": "genre", "tier": tier, "group": group, "track_id": aid,
                                "track_name": t["name"], "track_artist": t["artists"], "target": pname, "needs_create": False,
                                "unlock_id": None, "alt_target": alt_target, "from_playlist_name": source_name})
        else:
            genre_action = f"CREATE playlist '{genre}' then add this track [source: {genre_src}]"
            g_tok = f"NEW {genre}"
            to_create_genre[genre].append(_track_label(t))
            if aid:
                tier, group = local_tier or classify_genre(genre_src, close_vote)
                alt_target = genre_playlists[close_vote]["name"] if close_vote and close_vote in genre_playlists else None
                actions.append({"type": "add_track", "kind": "genre", "tier": tier, "group": group, "track_id": aid,
                                "track_name": t["name"], "track_artist": t["artists"], "target": genre, "needs_create": True,
                                "unlock_id": f"genre_{_slug(genre)}", "alt_target": alt_target, "from_playlist_name": source_name})

        loc = (" [LOCAL~Spotify]" if t.get("matched") else " [LOCAL]") if t.get("local") else (" [LIKED]" if t.get("liked") else "")
        tag = raw_genres[0] if raw_genres else ""
        if not apply_mode:
            # console line: only what is known, except "(? bpm)" on an otherwise-complete line, so a missing tempo does
            # not go unnoticed. A fully unresolved track stays bare (CSV has the rest).
            parts = [f"- {_track_label(t)}{loc}"]
            if tempo:
                parts.append(f"({tempo:g} bpm" + (f" or {' or '.join(alts)}" if alts else "") + ")")
            elif genre is not None:
                parts.append("(? bpm)")
            if genre is not None:
                parts.append(f": {tag + ' ' if tag else ''}-> {g_tok} ({src_short})")
            print(" ".join(parts))
        rows_add.append([t["name"] + loc, t["artists"], tempo, bucket, bpm_action, genre or "?", genre_src, genre_action, "; ".join(raw_genres[:5])])
    return rows_add

def find_misplaced_tracks(bpm_playlists, genre_playlists, contents, tempos, artist_genres, actions, apply_mode):
    """Section 2: audits every track already IN a BPM or genre playlist against its own measurement -
    prints/collects a "MOVE to" row for anything that disagrees, and appends a move_track action when
    there's a real destination and a real (non-local) id to move. Genre BPM moves go through
    _corroborate_bpm first (see there); genre moves are always "review" (a second opinion on your own
    curation, never auto-applied)."""
    rows_misplaced = []
    if not apply_mode:
        print("\n" + "=" * 100)
        print("2) POTENTIALLY MISPLACED TRACKS IN YOUR PLAYLISTS")
        print("=" * 100)

    for bucket_val, info in sorted(bpm_playlists.items()):
        for pos, t in enumerate(contents[info["name"]]):
            tempo = tempos.get(t["id"])
            if tempo is None:
                continue
            real = bpm_bucket(tempo)
            # tolerance: fine if the exact bucket OR the half/double-tempo version matches
            if real == bucket_val or bpm_bucket(tempo / 2) == bucket_val or bpm_bucket(tempo * 2) == bucket_val:
                continue
            dest = bpm_playlists.get(real)
            dest_name = dest["name"] if dest else f"{real} bpm (does not exist)"
            if not apply_mode:
                print(f"- [{info['name']}] {t['name']} - {t['artists']}: measured BPM {tempo} -> MOVE to '{dest_name}'")
            rows_misplaced.append([info["name"], t["name"], t["artists"], tempo, dest_name, "BPM"])
            # a move needs a real destination, and a real (non-synthetic) uri to remove from the source
            if dest and not t.get("local"):
                corroborated = _corroborate_bpm(t["id"], tempo, t["artists"], t["name"])
                tier, group = ("confident", None) if corroborated else ("review", "bpm_disagreement")
                actions.append({"type": "move_track", "kind": "bpm", "tier": tier, "group": group, "track_id": t["id"],
                                "track_name": t["name"], "track_artist": t["artists"], "from_playlist_id": info["id"],
                                "from_playlist_name": info["name"], "from_position": pos, "to_playlist_name": dest["name"]})

    skipped_artist_flags = 0
    for gname, info in sorted(genre_playlists.items()):
        if gname in MISPLACED_GENRE_EXEMPT:
            continue    # deliberate catch-all playlists are not audited
        for pos, t in enumerate(contents[info["name"]]):
            genre, raw, src, _close = resolve_genre(t, artist_genres)
            if genre and genre != gname:
                if any({gname, genre} <= pair for pair in NEIGHBOR_GENRES):
                    continue    # sibling playlists: the border is a curation choice, not a misplacement
                if MISPLACED_GENRE_TRACK_ONLY and "track" not in src:
                    skipped_artist_flags += 1   # artist-level tag = too weak a signal to flag
                    continue
                if not apply_mode:
                    print(f"- [{gname}] {t['name']} - {t['artists']}: estimated genre '{genre}' via {src} ({'; '.join(raw[:3])}) -> double-check")
                rows_misplaced.append([gname, t["name"], t["artists"], "; ".join(raw[:5]), genre, f"Genre ({src})"])
                dest_info = genre_playlists.get(genre)
                if dest_info and not t.get("local"):
                    actions.append({"type": "move_track", "kind": "genre", "tier": "review", "group": "misplaced_genre",
                                    "track_id": t["id"], "track_name": t["name"], "track_artist": t["artists"],
                                    "from_playlist_id": info["id"], "from_playlist_name": gname, "from_position": pos,
                                    "to_playlist_name": dest_info["name"]})
    if skipped_artist_flags and not apply_mode:
        print(f"({skipped_artist_flags} weaker artist-level genre flags hidden - set MISPLACED_GENRE_TRACK_ONLY=False to see them)")

    if not rows_misplaced and not apply_mode:
        print("Nothing to report.")
    return rows_misplaced

def find_duplicates(contents, playlist_id_by_name, actions, apply_mode):
    """Section 3: within each analysed playlist, flags every track beyond the first occurrence of the
    same exact ID (confident - a true duplicate) or the same name+artist under a different ID (review -
    could be a genuinely different version, e.g. live/remaster). Local files are reported but never
    given a removal action (no reliable API uri to remove one exact entry with)."""
    rows_dupes = []
    if not apply_mode:
        print("\n" + "=" * 100)
        print("3) DUPLICATES INSIDE EACH PLAYLIST")
        print("=" * 100)
    for pname, lst in contents.items():
        pid = playlist_id_by_name.get(pname)
        orig_id_count, orig_name_count = defaultdict(int), defaultdict(int) # never mutated: true totals
        for t in lst:
            orig_id_count[t["id"]] += 1
            orig_name_count[(t["name"].lower(), t["artists"].lower())] += 1
        seen_id, seen_name = dict(orig_id_count), dict(orig_name_count) # mutated copy: gates the "show once" print
        kept_id, kept_name = set(), set()   # the FIRST occurrence of each duplicate is kept, never removed
        for pos, t in enumerate(lst):
            key = (t["name"].lower(), t["artists"].lower())
            is_id_dup, is_name_dup = orig_id_count[t["id"]] > 1, orig_name_count[key] > 1
            if not (is_id_dup or is_name_dup):
                continue
            if seen_id[t["id"]] > 1 or seen_name[key] > 1:  # unchanged: report each duplicate value once
                reason = f"same track present {orig_id_count[t['id']]}x" if is_id_dup else "same name+artist (different versions/IDs)"
                if not apply_mode:
                    print(f"- [{pname}] {t['name']} - {t['artists']}: {reason} -> DELETE the duplicate")
                rows_dupes.append([pname, t["name"], t["artists"], reason])
                seen_id[t["id"]], seen_name[key] = 1, 1 # show only once

            # action: keep the first occurrence encountered, flag every later one for removal
            if t.get("local") or not pid:
                continue    # no reliable uri to remove a local file's exact playlist entry with
            if is_id_dup:
                if t["id"] not in kept_id:
                    kept_id.add(t["id"])
                else:
                    actions.append({"type": "remove_duplicate", "tier": "confident", "group": None, "playlist_id": pid, "playlist_name": pname,
                                    "position": pos, "track_id": t["id"], "track_name": t["name"], "track_artist": t["artists"], "total_copies": orig_id_count[t["id"]]})
            elif is_name_dup:
                if key not in kept_name:
                    kept_name.add(key)
                else:
                    actions.append({"type": "remove_duplicate", "tier": "review", "group": "dup_variant", "playlist_id": pid, "playlist_name": pname,
                                    "position": pos, "track_id": t["id"], "track_name": t["name"], "track_artist": t["artists"], "total_copies": orig_name_count[key]})
    if not rows_dupes and not apply_mode:
        print("No duplicates detected.")
    return rows_dupes

def list_playlists_to_create(to_create_bpm, to_create_genre, apply_mode):
    """Section 4: lists every BPM bucket / genre that doesn't have a playlist yet, with the tracks
    waiting for it (populated as a side effect of suggest_additions, section 1). Purely informational -
    the actual "confident, but gated behind this unlock" bookkeeping already happened when those
    add_track actions were built."""
    rows_create = []
    if not apply_mode:
        print("\n" + "=" * 100)
        print("4) PLAYLISTS TO CREATE (suggestion)")
        print("=" * 100)
    for label, kind, pending in ([(f"{b} bpm", "BPM", to_create_bpm[b]) for b in sorted(to_create_bpm)]
                                + [(g, "Genre", to_create_genre[g]) for g in sorted(to_create_genre)]):
        if not apply_mode:
            print(f"- '{label}': {len(pending)} pending track(s)")
            for x in pending:
                print(f"      . {x}")
        for x in pending:
            rows_create.append([label, kind, x])
    if not rows_create and not apply_mode:
        print("Your existing playlists cover every analysed track.")
    return rows_create

def dedupe_actions(actions):
    """Safety net, independent of where a duplicate action might sneak in from (a source read twice, a
    pagination hiccup...): the SAME track proposed for the SAME destination should only ever appear
    once. Keeps the first occurrence."""
    seen_actions, deduped = set(), []
    for a in actions:
        key = (a["track_id"], a["type"], a.get("target") or a.get("to_playlist_name") or a.get("position"))
        if key in seen_actions:
            continue
        seen_actions.add(key)
        deduped.append(a)
    if len(deduped) < len(actions):
        print(f"({len(actions) - len(deduped)} duplicate action(s) collapsed - same track, same destination)")
    return deduped

def _setup(apply_mode):
    """Everything up to and including gather_real_data(): config, login, the one read of your library."""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    problems = validate_config(require_spotify=not TEST_EXTERNES)
    if problems:
        sys.exit("The script cannot start: something in the CONFIG block needs fixing.\n\n  * "
                + "\n\n  * ".join(problems)
                + "\n\nFix the line(s) above in the CONFIG block at the top of the file, save, and run again.")
    load_tag_mappings()
    if TEST_EXTERNES:   # test mode: ReccoBeats + Last.fm only, zero Spotify calls
        load_cache()
        run_external_test()
        return None, None, None
    load_cache()
    sp = get_client(apply_mode)
    me = spotify_call(sp.current_user, "authentication")
    print(f"Logged in as: {me['display_name']} ({me['id']})")
    if apply_mode:
        print("Mode: APPLY - re-checking your library, then only writing what you approved.\n")
    elif LASTFM_API_KEY:
        print("Per-track genre: Last.fm ACTIVE (primary source), Spotify artist as fallback\n")
    else:
        print("WARNING: LASTFM_API_KEY missing -> genre via Spotify artists only (free key: last.fm/account/create)\n")
    gathered = gather_real_data(sp, me["id"], me["display_name"], apply_mode)
    return sp, me, gathered

def _run_sort(sp, gathered, apply_mode, auto_open=True):
    """Everything after gather_real_data(), taking the already-fetched data as a parameter instead of fetching it itself.
    Does not save the cache itself so a combined run can save it once, at the end."""
    bpm_playlists, genre_playlists, contents, playlist_id_by_name, source_tracks, tempos, measured_locally, artist_genres, year_contents, source_name = gathered

    # per-playlist ID sets to test membership
    ids_in = {name: {t["id"] for t in lst} for name, lst in contents.items()}
    actions = []    # every proposed change, tagged confident/review - see ACTION CLASSIFICATION above

    # Every analysed track, deduplicated by ID, across source + BPM/genre/liked/extra playlists + yearly ones.
    all_lib_tracks = {t["id"]: t for t in source_tracks}
    for lst in contents.values():
        for t in lst:
            all_lib_tracks.setdefault(t["id"], t)
    for lst in year_contents.values():
        for t in lst:
            all_lib_tracks.setdefault(t["id"], t)

    suggest_artists_to_follow(sp, all_lib_tracks, actions, apply_mode)

    to_create_bpm = defaultdict(list)       # {bucket: [tracks]} -> BPM playlists to create
    to_create_genre = defaultdict(list)     # {genre: [tracks]} -> Genre playlists to create
    unknown_genre_tags = defaultdict(list)  # {raw genre/tag: [tracks]} unmapped
    unmapped_by_track = []                  # [{"track": label, "tags": [t1, t2, ...]}] - one entry per track, for the interface
    rows_add = suggest_additions(source_tracks, tempos, bpm_playlists, genre_playlists, ids_in, measured_locally, artist_genres,
                                actions, to_create_bpm, to_create_genre, unknown_genre_tags, unmapped_by_track, apply_mode, source_name)

    rows_misplaced = find_misplaced_tracks(bpm_playlists, genre_playlists, contents, tempos, artist_genres, actions, apply_mode)

    rows_dupes = find_duplicates(contents, playlist_id_by_name, actions, apply_mode)

    rows_create = list_playlists_to_create(to_create_bpm, to_create_genre, apply_mode)

    actions = dedupe_actions(actions)

    if unknown_genre_tags and not apply_mode:
        print("\nGenres/tags encountered but not mapped to your categories (sorted by frequency - useful to decide")
        print("on a possible new coarse-grained playlist, or to enrich GENRE_RULES):")
        for g, titles in sorted(unknown_genre_tags.items(), key=lambda kv: len(kv[1]), reverse=True)[:15]:
            print(f"  - '{g}': {len(titles)} track(s)  e.g. {titles[0]}")

    if apply_mode:
        apply_actions(sp, actions, to_create_bpm, to_create_genre, playlist_id_by_name)
        return

    # =====================================================================
    # CSV EXPORT + report.json (for review_interface.html)
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
    # The library backup and analysis.json both moved entirely to SpotifyLibraryAnalysis.py: their value
    # is covering your WHOLE analysed library, not just this tool's sort queue - run that script for them.
    report_path = os.path.join(OUTPUT_DIR, "report.json")
    export_report(actions, to_create_bpm, to_create_genre, report_path, unknown_genre_tags, unmapped_by_track)
    if auto_open:
        open_review_interface(report_path, os.path.join(OUTPUT_DIR, "analysis.json"))

    print("\nDone. Nothing was modified on your account (dry-run).")
    if AUTO_OPEN_REVIEW:
        print("Next: in the review page that just opened, decide and export your decisions, then run")
        print("      this script again with --apply to write the approved changes to Spotify.")
    else:
        print("Next: open review_interface.html, load report.json, decide, export decisions.json,")
        print("      then run this script again with --apply to write the approved changes to Spotify.")

def main(apply_mode=False):
    sp, me, gathered = _setup(apply_mode)
    if gathered is None:
        return  # TEST_EXTERNES already ran its own diagnostic inside _setup - nothing further to do
    _run_sort(sp, gathered, apply_mode)
    save_cache(force=True)

if __name__ == "__main__":
    apply_mode = "--apply" in sys.argv[1:]

    # Double-clicking a packaged .exe passes no arguments at all - there is no way to type --apply then.
    if not apply_mode and len(sys.argv) == 1 and getattr(sys, "frozen", False):
        choice = input("Press Enter to analyse (read-only), or type 'apply' then Enter to write your approved changes to Spotify: ").strip().lower()
        apply_mode = choice == "apply"

    if getattr(sys, "frozen", False):
        exit_code = 0
        try:
            main(apply_mode=apply_mode)
        except SystemExit as e:
            if isinstance(e.code, str):
                print(e.code)   # sys.exit("some message") carries the message in e.code, not a print
            exit_code = e.code if isinstance(e.code, int) else (1 if e.code else 0)
        except Exception:
            import traceback
            traceback.print_exc()
            exit_code = 1
        input("\nPress Enter to close this window...")
        sys.exit(exit_code)
    else:
        main(apply_mode=apply_mode)
