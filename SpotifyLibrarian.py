#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SpotifyLibrarian - single entry point for SpotifySortPlaylist and SpotifyLibraryAnalysis.

Double-click this (or the packaged .exe built from it) and pick what you want to do from the menu - no need to remember
which of the two scripts does what, or type --apply by hand. Both tools still work fine on their own too
(python SpotifySortPlaylist.py, python SpotifyLibraryAnalysis.py) - this is purely a convenience shared front door,
especially useful once packaged into one .exe (see the README).

Command-line shortcuts (skip the menu):
    python SpotifyLibrarian.py sort         same as SpotifySortPlaylist.py
    python SpotifyLibrarian.py sort --apply same as SpotifySortPlaylist.py --apply
    python SpotifyLibrarian.py analysis     same as SpotifyLibraryAnalysis.py
    python SpotifyLibrarian.py both         sort's read-only check, then the library stats
"""

import os
import sys

import SpotifySortPlaylist
import SpotifyLibraryAnalysis

def show_menu():
    print("What do you want to do?")
    print("  1) Check what needs sorting (read-only - moves, adds, duplicates, playlists to create)")
    print("  2) Apply those sorting changes (writes what you approved)")
    print("  3) See library stats (genres, tempo, top artists, year-over-year trends)")
    print("  4) Both 1 and 3 - check what needs sorting, then see library stats")
    print("  5) Quit")
    choice = input("> ").strip()
    return {"1": "sort", "2": "sort-apply", "3": "analysis", "4": "both", "5": "quit"}.get(choice)

def run(action):
    if action == "sort":
        SpotifySortPlaylist.main(apply_mode=False)
    elif action == "sort-apply":
        SpotifySortPlaylist.main(apply_mode=True)
    elif action == "analysis":
        SpotifyLibraryAnalysis.main()
    elif action == "both":
        sp, me, gathered = SpotifySortPlaylist._setup(apply_mode=False)
        if gathered is None:
            return  # TEST_EXTERNES ran its own diagnostic inside _setup - nothing further to combine
        SpotifySortPlaylist._run_sort(sp, gathered, apply_mode=False, auto_open=False)
        print("\n" + "=" * 100 + "\n")
        SpotifyLibraryAnalysis._run_analysis(gathered, auto_open=False)
        SpotifySortPlaylist.save_cache(force=True)

        # One page, one open, both fresh datasets - not two tabs from two individual opens.
        SpotifySortPlaylist.open_review_interface(
            os.path.join(SpotifySortPlaylist.OUTPUT_DIR, "report.json"),
            os.path.join(SpotifySortPlaylist.OUTPUT_DIR, "analysis.json"))

if __name__ == "__main__":
    args = sys.argv[1:]

    if args and args[0] == "sort":
        action = "sort-apply" if "--apply" in args else "sort"
    elif args and args[0] == "analysis":
        action = "analysis"
    elif args and args[0] == "both":
        action = "both"
    elif args:
        sys.exit(f"Unknown argument(s): {' '.join(args)}\n"
                 f"Usage: python SpotifyLibrarian.py [sort [--apply] | analysis | both]")
    else:
        action = show_menu()
        if action is None:
            sys.exit("Not a valid choice - run again and pick 1, 2, 3, 4, or 5.")
        if action == "quit":
            sys.exit(0)

    if getattr(sys, "frozen", False):
        # Same reasoning as SpotifySortPlaylist.py's own entry point: a double-clicked .exe's console
        # closes the instant the process ends, taking any message - success, error, or crash - with it.
        exit_code = 0
        try:
            run(action)
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
        run(action)
