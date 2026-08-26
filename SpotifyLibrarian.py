#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SpotifyLibrarian - single entry point for SpotifySortPlaylist and SpotifyLibraryAnalysis.

Double-click this (or the packaged .exe built from it) and pick what you want to do from the menu -
no need to remember which of the two scripts does what, or type --apply by hand. Both tools still work
fine on their own too (python SpotifySortPlaylist.py, python SpotifyLibraryAnalysis.py) - this is purely
a convenience shared front door, especially useful once packaged into one .exe (see the README).

Command-line shortcuts (skip the menu):
    python SpotifyLibrarian.py sort            same as SpotifySortPlaylist.py
    python SpotifyLibrarian.py sort --apply    same as SpotifySortPlaylist.py --apply
    python SpotifyLibrarian.py analysis        same as SpotifyLibraryAnalysis.py
"""

import sys

import SpotifySortPlaylist
import SpotifyLibraryAnalysis


def show_menu():
    print("What do you want to do?")
    print("  1) Sort my library (analyse - read-only)")
    print("  2) Sort my library (--apply - writes your approved changes)")
    print("  3) Analyse my library (genres, tempo, trends, backup)")
    print("  4) Quit")
    choice = input("> ").strip()
    return {"1": "sort", "2": "sort-apply", "3": "analysis", "4": "quit"}.get(choice)


def run(action):
    if action == "sort":
        SpotifySortPlaylist.main(apply_mode=False)
    elif action == "sort-apply":
        SpotifySortPlaylist.main(apply_mode=True)
    elif action == "analysis":
        SpotifyLibraryAnalysis.main()


if __name__ == "__main__":
    args = sys.argv[1:]

    if args and args[0] == "sort":
        action = "sort-apply" if "--apply" in args else "sort"
    elif args and args[0] == "analysis":
        action = "analysis"
    elif args:
        sys.exit(f"Unknown argument(s): {' '.join(args)}\n"
                 f"Usage: python SpotifyLibrarian.py [sort [--apply] | analysis]")
    else:
        action = show_menu()
        if action is None:
            sys.exit("Not a valid choice - run again and pick 1, 2, 3, or 4.")
        if action == "quit":
            sys.exit(0)

    if getattr(sys, "frozen", False):
        # Same reasoning as SpotifySortPlaylist.py's own entry point: a double-clicked .exe's console
        # closes the instant the process ends, taking any message - success, error, or crash - with it.
        exit_code = 0
        try:
            run(action)
        except SystemExit as e:
            exit_code = e.code if isinstance(e.code, int) else (1 if e.code else 0)
        except Exception:
            import traceback
            traceback.print_exc()
            exit_code = 1
        input("\nPress Enter to close this window...")
        sys.exit(exit_code)
    else:
        run(action)
