#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Analyse (DRY-RUN uniquement) des playlists Spotify :
  1. Pour chaque titre de la playlist source, propose dans quelle playlist "XX bpm" et dans quelle playlist "Genre" il devrait aller.
  2. Vérifie les playlists BPM / Genre existantes et signale les titres qui semblent mal placés.
  3. Détecte les doublons à l'intérieur de chaque playlist.
  4. Liste les playlists (BPM ou Genre) qu'il faudrait créer.

AUCUNE modification n'est faite sur votre compte : le script ne demande que des scopes en LECTURE et écrit un rapport (console + CSV).

PRÉREQUIS :
  1. pip install spotipy requests
  2. Créer une app sur https://developer.spotify.com/dashboard avec le Redirect URI : http://127.0.0.1:8888/callback
  3. Remplir le bloc CONFIG ci-dessous : SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET (+ LASTFM_API_KEY recommandée).
     Les variables d'environnement restent utilisables en secours.

NOTE BPM : Spotify a déprécié l'endpoint /audio-features pour les apps créées après le 27/11/2024 (erreur 403).
Le script essaie d'abord l'API Spotify, puis bascule automatiquement sur l'API gratuite ReccoBeats (https://reccobeats.com)
qui accepte des IDs Spotify. Les titres sans BPM sont listés "BPM inconnu".

NOTE GENRE : Spotify ne fournit pas de genre par TITRE, uniquement par ARTISTE -> jugé trop imprécis.
Cascade appliquée :
  1. tags Last.fm DU TITRE (par musique, source principale) -> LASTFM_API_KEY fortement recommandée (clé gratuite)
  2. secours : genres Spotify des artistes du titre
Chaque suggestion indique sa source ; ça reste une heuristique à valider.

NOTE QUOTA (v9) : le quota journalier Spotify est vite épuisé depuis la migration 02/2026. Le script maintient donc
un CACHE DISQUE (cache_spotify_tri.json) : contenus de playlists (invalidés par snapshot_id, donc rafraîchis
uniquement si la playlist a changé), BPM ReccoBeats et tags Last.fm. Un run interrompu par le quota reprend là où
il en était au lancement suivant, sans re-consommer ce qui a déjà été lu.
"""

import csv
import json
import os
import re
import sys
import time
from collections import defaultdict

import requests
import spotipy
from spotipy.oauth2 import SpotifyOAuth

# ======================================================================================================================
# CONFIG — TOUT SE RENSEIGNE ICI
# ======================================================================================================================

# --- 1) IDENTIFIANTS SPOTIFY (obligatoire) -> https://developer.spotify.com/dashboard :
#        créer une app, copier Client ID / Client Secret, et déclarer le Redirect URI EXACTEMENT identique à celui ci-dessous.
SPOTIFY_CLIENT_ID     = "XXX" # /!\
SPOTIFY_CLIENT_SECRET = "XXX" # /!\
SPOTIFY_REDIRECT_URI  = "http://127.0.0.1:8888/callback"

# --- 2) CLÉ LAST.FM (fortement recommandée : source principale du genre PAR TITRE ;sans elle, on retombe sur les genres d'artistes Spotify)
#        Clé gratuite : https://www.last.fm/api/account/create
LASTFM_API_KEY = "XXX" # /!\

# --- 3) PLAYLISTS
SOURCE_PLAYLIST_ID = "XXX" # /!\

# --- 4) RÉGLAGES BPM — tranches X0-X9 : un titre à 87 bpm va dans "80 bpm", un titre à 154 bpm va dans "150 bpm" (troncature à la dizaine)
BPM_STEP = 10
# Un titre à 154 bpm peut aussi se "sentir" à 77 : afficher l'alternative moitié/double dans le rapport.
SHOW_HALF_DOUBLE_TEMPO = True

# --- 5) RÈGLES DE GENRE (grosse maille) — nom exact de vos playlists + mots-clés (minuscules) comparés aux tags
#        Last.fm du titre et aux genres d'artistes Spotify. L'ordre = priorité : le premier genre qui matche gagne.
#        Les micro-genres ("melodic drill", "bedroom pop"...) sont rabattus sur vos grandes catégories.
#        La section "genres non mappés" du rapport vous aidera à décider d'une éventuelle nouvelle catégorie.
GENRE_RULES = [
    ("Française",  ["french", "chanson", "variete francaise", "variété française", "francoton"]),
    ("Latina",     ["latin", "reggaeton", "cumbia", "salsa", "bachata", "urbano", "corrido", "mariachi"]),
    ("Classique",  ["classical", "baroque", "romantic era", "opera", "orchestr", "chamber", "requiem"]),
    ("Jazz",       ["jazz", "bebop", "swing", "bossa nova", "big band"]),
    ("Reggae",     ["reggae", "dancehall", "ska", "dub", "roots"]),
    ("Rap",        ["rap", "hip hop", "hip-hop", "trap", "drill", "grime", "boom bap"]),
    ("Soul",       ["soul", "r&b", "rnb", "funk", "motown", "gospel"]),
    ("Electro",    ["electro", "edm", "house", "techno", "trance", "dubstep", "drum and bass", "dnb", "bass music", "synthwave", "big room"]),
    ("Dance",      ["dance pop", "dance", "disco", "eurodance", "hyperpop"]),
    ("Rock",       ["rock", "metal", "punk", "grunge", "emo", "hardcore", "shoegaze", "garage"]),
    ("Pop",        ["pop", "indie", "singer-songwriter"]),  # fourre-tout en dernier
]

# --- 5bis) SECOURS GENRE VIA SPOTIFY — depuis la migration API 02/2026, récupérer les genres d'artistes exige
#           un appel par artiste, ce qui épuise le quota journalier de l'app sur les grosses bibliothèques
#           (blocage ~24 h). Désactivé par défaut : Last.fm reste la source principale du genre ; les titres
#           inconnus de Last.fm seront "genre non identifié" au lieu d'être estimés via l'artiste.
USE_SPOTIFY_ARTIST_GENRES = False
MAX_ARTIST_LOOKUPS = 200    # plafond d'appels unitaires si le secours est activé (protège le quota)

# --- 6) SORTIE ET CACHE
OUTPUT_DIR = "rapport_spotify"          # dossier des CSV du rapport
CACHE_FILE = "cache_spotify_tri.json"   # cache disque : playlists (par snapshot), BPM ReccoBeats, tags Last.fm
                                        # -> à supprimer pour forcer une relecture complète

# --- 6bis) PROXY D'ENTREPRISE (optionnel — utile derrière un pare-feu qui bloque l'accès direct à Internet)
#           Format : "http://proxy.mondomaine.fr:8080" ou "http://user:motdepasse@proxy.mondomaine.fr:8080"
#           Laisser vide pour une connexion directe (maison). Le proxy s'applique à Spotify, Last.fm et ReccoBeats.
PROXY_URL = "" # /!\

# --- 6ter) CERTIFICATS SSL (proxys d'entreprise avec inspection SSL -> erreur CERTIFICATE_VERIFY_FAILED)
#           USE_SYSTEM_CERTS = True : Python utilise le magasin de certificats Windows (où l'IT a installé le
#           certificat racine de l'entreprise). Nécessite : pip install truststore
USE_SYSTEM_CERTS = True

# --- 7) AVANCÉ (ne toucher que si besoin)
RECCOBEATS_URL = "https://api.reccobeats.com/v1/audio-features"
LASTFM_URL = "https://ws.audioscrobbler.com/2.0/"
LASTFM_MIN_TAG_COUNT = 10                                       # ignorer les tags Last.fm trop marginaux
SCOPES = "playlist-read-private playlist-read-collaborative"    # lecture seule !

# ======================================================================================================================
# FIN DE LA CONFIG — rien à modifier en dessous
# ======================================================================================================================

GENRE_PLAYLIST_NAMES = [name for name, _ in GENRE_RULES]

# Variables d'environnement utilisables en secours si les champs ci-dessus sont vides
# (pratique pour ne pas committer ses secrets).
SPOTIFY_CLIENT_ID     = SPOTIFY_CLIENT_ID or os.environ.get("SPOTIPY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = SPOTIFY_CLIENT_SECRET or os.environ.get("SPOTIPY_CLIENT_SECRET", "")
SPOTIFY_REDIRECT_URI  = SPOTIFY_REDIRECT_URI or os.environ.get("SPOTIPY_REDIRECT_URI", "")
LASTFM_API_KEY        = LASTFM_API_KEY or os.environ.get("LASTFM_API_KEY", "")

# Proxy : requests (utilisé par spotipy, Last.fm et ReccoBeats) lit HTTP_PROXY/HTTPS_PROXY dans l'environnement.
if PROXY_URL:
    os.environ["HTTP_PROXY"] = PROXY_URL
    os.environ["HTTPS_PROXY"] = PROXY_URL
    os.environ["NO_PROXY"] = "127.0.0.1,localhost"  # le callback OAuth reste local, il ne doit pas passer par le proxy

# Certificats : proxys d'entreprise avec inspection SSL.
if USE_SYSTEM_CERTS:
    try:
        import truststore
        truststore.inject_into_ssl()  # Python valide désormais via le magasin de certificats de l'OS
    except ImportError:
        print("INFO : module 'truststore' absent (pip install truststore) -> validation SSL via les certificats\n"
              "       par défaut de Python. Sans impact en connexion directe ; derrière un proxy à inspection SSL,\n"
              "       attendez-vous à une erreur CERTIFICATE_VERIFY_FAILED.", file=sys.stderr)

# ======================================================================================================================
# CACHE DISQUE — protège le quota Spotify et accélère les relances
# ======================================================================================================================
_cache = {"playlists": {}, "tempos": {}, "lastfm": {}}
_cache_dirty = 0


def load_cache():
    global _cache
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        for k in _cache:
            _cache[k].update(data.get(k, {}))
        print(f"Cache chargé : {len(_cache['playlists'])} playlists, {len(_cache['tempos'])} BPM, "
              f"{len(_cache['lastfm'])} lookups Last.fm ({CACHE_FILE})")
    except FileNotFoundError:
        pass
    except (json.JSONDecodeError, OSError) as e:
        print(f"! cache illisible ({e}) -> on repart de zéro", file=sys.stderr)


def save_cache(force=False):
    """Écrit le cache sur disque. Appelé après chaque bloc de travail (et périodiquement pendant Last.fm)."""
    global _cache_dirty
    if not force and _cache_dirty < 50:
        return
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(_cache, f, ensure_ascii=False)
        _cache_dirty = 0
    except OSError as e:
        print(f"! cache non sauvegardé : {e}", file=sys.stderr)


def quota_exit(context):
    save_cache(force=True)
    sys.exit(f"\nERREUR : Spotify refuse les requêtes ({context}) — quota journalier de l'app très probablement "
             f"épuisé.\n"
             f"-> Option 1 : réessayer dans ~24 h.\n"
             f"-> Option 2 : créer une SECONDE app sur developer.spotify.com/dashboard (même Redirect URI),\n"
             f"   reporter ses Client ID/Secret dans le CONFIG et SUPPRIMER le fichier .spotify_token_cache.\n"
             f"Le cache disque ({CACHE_FILE}) a été sauvegardé : le prochain run ne relira que le manquant.")


# ======================================================================================================================
# HELPERS SPOTIFY
# ======================================================================================================================
def get_client() -> spotipy.Spotify:
    if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
        sys.exit("ERREUR : SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET non renseignés.\n"
                 "-> Ouvrez le script et remplissez le bloc CONFIG en haut du fichier (identifiants à créer sur\n"
                 "   https://developer.spotify.com/dashboard, avec le Redirect URI exactement égal\n"
                 "   à SPOTIFY_REDIRECT_URI).")
    # status_retries=0 : sur un 429 (quota), spotipy lève l'erreur immédiatement au lieu de dormir des heures
    auth = SpotifyOAuth(client_id=SPOTIFY_CLIENT_ID, client_secret=SPOTIFY_CLIENT_SECRET, redirect_uri=SPOTIFY_REDIRECT_URI,
                        scope=SCOPES, open_browser=True, cache_path=".spotify_token_cache")
    return spotipy.Spotify(auth_manager=auth, retries=3, status_retries=0, requests_timeout=15)


def fetch_all(sp, first_page):
    """Itère sur toutes les pages d'un résultat paginé Spotify."""
    items, page = list(first_page["items"]), first_page
    while page["next"]:
        page = sp.next(page)
        items.extend(page["items"])
    return items


def _parse_playlist_items(items):
    """Extrait les pistes d'une liste d'items, au format ancien (clé 'track') comme nouveau (clé 'item')."""
    tracks = []
    for it in items:
        t = it.get("track") or it.get("item") or {}
        if not t or t.get("is_local") or not t.get("id"):
            continue
        if t.get("type") and t["type"] != "track":
            continue  # épisodes de podcast, etc.
        tracks.append({"id": t["id"], "name": t["name"],
                       "artists": ", ".join(a["name"] for a in t.get("artists", [])),
                       "artist_ids": [a["id"] for a in t.get("artists", []) if a.get("id")]})
    return tracks

def get_playlist_tracks(sp, playlist_id, snapshot_id=None):
    """
    Retourne [{id, name, artists, artist_ids}] pour une playlist. Titres locaux/indisponibles/épisodes ignorés.
    CACHE : si le snapshot_id n'a pas changé depuis le dernier run, le contenu vient du disque (0 appel API).
    Migration API Spotify 02/2026 : on interroge d'abord le NOUVEAU endpoint /playlists/{id}/items (pagination
    manuelle, spotipy n'a pas encore de méthode dédiée) ; l'ancien /tracks ne sert plus que de repli, pour ne
    pas payer deux fois la pagination sur les apps post-migration. Un 429 arrête le script proprement.
    """
    global _cache_dirty
    cached = _cache["playlists"].get(playlist_id)
    if cached and snapshot_id and cached.get("snapshot_id") == snapshot_id:
        return cached["tracks"]

    # 1) nouvel endpoint /items (post-migration)
    raw_items, offset, new_ok = [], 0, True
    while True:
        try:
            page = sp._get(f"playlists/{playlist_id}/items", limit=100, offset=offset)
        except spotipy.exceptions.SpotifyException as e:
            if e.http_status == 429:
                quota_exit(f"lecture de la playlist {playlist_id}")
            new_ok = offset > 0  # échec dès la 1re page -> on tentera l'ancien endpoint
            break
        except requests.exceptions.RequestException as e:
            print(f"  ! réseau instable sur {playlist_id} ({type(e).__name__}) -> nouvelle tentative dans 5 s",
                  file=sys.stderr)
            time.sleep(5)
            continue
        items = page.get("items", [])
        raw_items.extend(items)
        if not items or not page.get("next"):
            break
        offset += len(items)

    # 2) repli : ancien endpoint /tracks via spotipy (apps d'avant la migration)
    if not new_ok and not raw_items:
        try:
            page = sp.playlist_items(playlist_id, additional_types=("track",))
            raw_items = fetch_all(sp, page)
        except spotipy.exceptions.SpotifyException as e:
            if e.http_status == 429:
                quota_exit(f"lecture de la playlist {playlist_id} (ancien endpoint)")
            print(f"  ! playlist {playlist_id} illisible ({e.http_status})", file=sys.stderr)
            raw_items = []

    tracks = _parse_playlist_items(raw_items)
    if snapshot_id:
        _cache["playlists"][playlist_id] = {"snapshot_id": snapshot_id, "tracks": tracks}
        _cache_dirty += 1
        save_cache(force=True)  # chaque playlist lue est immédiatement sécurisée sur disque
    return tracks


def get_my_playlists(sp):
    try:
        return fetch_all(sp, sp.current_user_playlists(limit=50))
    except spotipy.exceptions.SpotifyException as e:
        if e.http_status == 429:
            quota_exit("liste des playlists")
        raise


# ======================================================================================================================
# BPM : Spotify -> fallback ReccoBeats (avec cache disque)
# ======================================================================================================================
def get_tempos(sp, track_ids):
    """dict {track_id: bpm float ou None}. Les BPM déjà connus viennent du cache disque, 0 appel."""
    global _cache_dirty
    tempos = {tid: _cache["tempos"].get(tid) for tid in track_ids}
    missing = [tid for tid, v in tempos.items() if v is None]
    if not missing:
        print("  -> tous les BPM sont en cache")
        return tempos
    print(f"  -> {len(track_ids) - len(missing)} BPM en cache, {len(missing)} à récupérer")

    # --- 1) tentative via Spotify (fonctionne seulement pour les apps créées avant le 27/11/2024)
    spotify_ok = True
    try:
        feats = sp.audio_features(missing[:1])
        if not feats or feats[0] is None:
            spotify_ok = False
    except spotipy.exceptions.SpotifyException:
        spotify_ok = False

    if spotify_ok:
        print("  -> BPM via l'API Spotify (audio-features)")
        for i in range(0, len(missing), 100):
            for f in sp.audio_features(missing[i:i + 100]) or []:
                if f and f.get("tempo"):
                    tempos[f["id"]] = round(float(f["tempo"]), 1)
                    _cache["tempos"][f["id"]] = tempos[f["id"]]
                    _cache_dirty += 1
        save_cache(force=True)
        return tempos

    # --- 2) fallback ReccoBeats (accepte des IDs Spotify, max ~40/requête)
    print("  -> API Spotify indisponible (endpoint déprécié), fallback ReccoBeats")
    for i in range(0, len(missing), 40):
        batch = missing[i:i + 40]
        try:
            r = requests.get(RECCOBEATS_URL, params={"ids": ",".join(batch)}, timeout=20)
            r.raise_for_status()
            for item in r.json().get("content", []):
                m = re.search(r"track/([A-Za-z0-9]+)", item.get("href", ""))  # href = .../track/{spotify_id}
                sid = m.group(1) if m else None
                if sid in tempos and item.get("tempo"):
                    tempos[sid] = round(float(item["tempo"]), 1)
                    _cache["tempos"][sid] = tempos[sid]
                    _cache_dirty += 1
        except requests.RequestException as e:
            print(f"     ! ReccoBeats erreur sur un lot : {e}", file=sys.stderr)
        save_cache()
        time.sleep(0.5)  # politesse
    save_cache(force=True)
    return tempos

def bpm_bucket(tempo):
    """Tranche X0-X9 : 87.3 -> 80, 154 -> 150 (troncature à la dizaine)."""
    return None if tempo is None else int(tempo // BPM_STEP) * BPM_STEP

# ======================================================================================================================
# GENRE : cascade "la meilleure info par titre d'abord"
#   1) Tags Last.fm DU TITRE (par musique -> la source la plus précise ici). Nécessite LASTFM_API_KEY (clé gratuite).
#   2) Secours : genres Spotify des artistes du titre (Spotify n'a pas de genre par titre dans son API, uniquement par artiste).
# ======================================================================================================================
def get_artist_genres(sp, artist_ids):
    """
    dict {artist_id: [genres]}. Secours du genre (Last.fm est la source principale). Migration 02/2026 :
    le batch GET /artists est supprimé ; les appels unitaires épuisent le quota journalier de l'app sur les grosses bibliothèques.
    Désactivé par défaut (USE_SPOTIFY_ARTIST_GENRES) ; si activé, plafonné et avec arrêt immédiat sur 429 pour ne jamais bloquer 24 h.
    """
    if not USE_SPOTIFY_ARTIST_GENRES:
        print("  (secours genres Spotify désactivé — Last.fm seul, cf. USE_SPOTIFY_ARTIST_GENRES)")
        return {}
    out, ids = {}, list(set(artist_ids))
    try:  # 1) batch (50 par appel) — apps d'avant la migration
        for i in range(0, len(ids), 50):
            for a in sp.artists(ids[i:i + 50])["artists"]:
                if a:
                    out[a["id"]] = [g.lower() for g in a.get("genres", [])]
        return out
    except spotipy.exceptions.SpotifyException:
        print("  ! endpoint batch /artists supprimé (migration Spotify 02/2026) -> appels unitaires", file=sys.stderr)
    if len(ids) > MAX_ARTIST_LOOKUPS:
        print(f"  ! {len(ids)} artistes > plafond MAX_ARTIST_LOOKUPS={MAX_ARTIST_LOOKUPS} : "
              f"seuls les {MAX_ARTIST_LOOKUPS} premiers seront interrogés (protection du quota)", file=sys.stderr)
    try:  # 2) unitaire — plafonné
        for i, aid in enumerate(ids[:MAX_ARTIST_LOOKUPS]):
            a = sp.artist(aid)
            if a:
                out[aid] = [g.lower() for g in a.get("genres", [])]
            if i % 50 == 49:
                time.sleep(1)
    except spotipy.exceptions.SpotifyException as e:
        if e.http_status == 429:
            print("  ! quota Spotify atteint (429) -> secours genres abandonné, Last.fm reste la source",
                  file=sys.stderr)
        else:
            print(f"  ! genres d'artistes Spotify indisponibles ({e.http_status}) -> secours abandonné",
                  file=sys.stderr)
    return out


def get_lastfm_track_tags(artist, title):
    """Tags Last.fm pour un titre précis (par musique). [] si indisponible. Cache disque (y compris les échecs)."""
    global _cache_dirty
    if not LASTFM_API_KEY:
        return []
    key = f"{artist.lower()}||{title.lower()}"
    if key in _cache["lastfm"]:
        return _cache["lastfm"][key]
    tags = []
    try:
        r = requests.get(LASTFM_URL, params={"method": "track.gettoptags", "artist": artist, "track": title,
                                             "api_key": LASTFM_API_KEY, "format": "json", "autocorrect": 1}, timeout=15)
        r.raise_for_status()
        raw = r.json().get("toptags", {}).get("tag", [])
        tags = [t["name"].lower() for t in raw if int(t.get("count", 0)) >= LASTFM_MIN_TAG_COUNT][:10]
    except (requests.RequestException, ValueError, KeyError, TypeError):
        pass
    _cache["lastfm"][key] = tags
    _cache_dirty += 1
    save_cache()  # sauvegarde périodique (tous les ~50 nouveaux lookups)
    time.sleep(0.25)  # respect du rate-limit Last.fm
    return tags

def match_category(genre_list):
    """Rabat une liste de genres/tags bruts sur vos catégories grosse maille."""
    for category, keywords in GENRE_RULES:
        for g in genre_list:
            if any(kw in g for kw in keywords):
                return category
    return None

_genre_cache = {}

def resolve_genre(track, artist_genres):
    """
    Retourne (categorie|None, genres_bruts, source). Priorité INVERSÉE volontairement :
    le genre Spotify n'existe qu'au niveau ARTISTE (imprécis pour un titre donné), donc on interroge d'abord Last.fm
    avec les tags DU TITRE, et Spotify ne sert que de filet de sécurité si Last.fm ne connaît pas le morceau.
    """
    if track["id"] in _genre_cache:
        return _genre_cache[track["id"]]

    # 1) Last.fm : tags par TITRE (la "bonne info", par musique)
    main_artist = track["artists"].split(",")[0].strip()
    tags = get_lastfm_track_tags(main_artist, track["name"])
    cat = match_category(tags) if tags else None
    if cat:
        result = (cat, tags, "Last.fm (titre)")
    else:
        # 2) Secours : genres Spotify des artistes du titre
        sp_genres = [g for aid in track["artist_ids"] for g in artist_genres.get(aid, [])]
        cat2 = match_category(sp_genres) if sp_genres else None
        if cat2:
            result = (cat2, sp_genres, "Spotify (artiste)")
        else:
            result = (None, tags or sp_genres, "aucune")  # aucune source concluante : renvoyer ce qu'on a vu, pour info

    _genre_cache[track["id"]] = result
    return result

# ======================================================================================================================
# MAIN
# ======================================================================================================================
def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    load_cache()
    sp = get_client()
    try:
        me = sp.current_user()
    except spotipy.exceptions.SpotifyException as e:
        if e.http_status == 429:
            quota_exit("authentification")
        raise
    print(f"Connecté en tant que : {me['display_name']} ({me['id']})")
    if LASTFM_API_KEY:
        print("Genre par titre : Last.fm ACTIF (source principale), Spotify artiste en secours\n")
    else:
        print("ATTENTION : LASTFM_API_KEY absente -> genres estimés uniquement via les artistes Spotify\n"
              "(moins précis par titre).\n"
              "Clé gratuite : https://www.last.fm/api/account/create\n")

    # ---------------- Playlists de l'utilisateur ----------------
    print("Lecture de vos playlists...")
    all_playlists = get_my_playlists(sp)
    bpm_playlists, genre_playlists = {}, {}  # {60: {"id","name"}} / {"Pop": {"id","name"}}
    for p in all_playlists:
        name = p["name"].strip()
        m = re.fullmatch(r"(\d{2,3})\s*bpm", name, re.IGNORECASE)
        if m:
            bpm_playlists[int(m.group(1))] = {"id": p["id"], "name": name}
        elif name in GENRE_PLAYLIST_NAMES:
            genre_playlists[name] = {"id": p["id"], "name": name}

    print(f"  Playlists BPM trouvées   : {sorted(bpm_playlists)}")
    print(f"  Playlists Genre trouvées : {sorted(genre_playlists)}\n")

    # --- diagnostic : si rien n'est reconnu, montrer ce que l'API renvoie pour comprendre pourquoi
    if not bpm_playlists and not genre_playlists:
        print("!! DIAGNOSTIC : aucune playlist BPM ni Genre reconnue. Voici les noms exacts renvoyés par l'API")
        print(f"!! ({len(all_playlists)} playlists au total) — comparez avec vos noms attendus :")
        for p in all_playlists:
            print(f"!!   - {p['name']!r} (owner: {p['owner']['id']})")
        print("!! Causes fréquentes : nom différent (espace, casse, accent), ou autre compte Spotify.\n")

    # ---------------- Contenu de toutes les playlists concernées ----------------
    contents = {}  # {playlist_name: [tracks]}
    for info in list(bpm_playlists.values()) + list(genre_playlists.values()):
        contents[info["name"]] = get_playlist_tracks(sp, info["id"], snapshots.get(info["id"]))
        print(f"  {info['name']:<12} : {len(contents[info['name']])} titres")

    source_tracks = get_playlist_tracks(sp, SOURCE_PLAYLIST_ID, snapshots.get(SOURCE_PLAYLIST_ID))
    print(f"\nPlaylist source : {len(source_tracks)} titres\n")
    if not source_tracks:
        print("!! DIAGNOSTIC : la playlist source ne renvoie aucun titre exploitable.")
        try:
            meta = sp.playlist(SOURCE_PLAYLIST_ID)
            owner = meta.get("owner", {}).get("id", "?")
            total = meta.get("tracks", {}).get("total", "?")
            print(f"!!   Nom: {meta.get('name')!r} | owner: {owner} | total annoncé: {total}")
            print(f"!!   Champs renvoyés par l'API : {sorted(meta.keys())}")
        except Exception as e:
            print(f"!!   Impossible de lire les métadonnées de la playlist : {e}")
        try:
            raw = sp.playlist_items(SOURCE_PLAYLIST_ID, limit=3)  # ancien endpoint, réponse brute
            items = raw.get("items", [])
            print(f"!!   Ancien endpoint /tracks : total={raw.get('total', '?')}, items page 1={len(items)}")
            for it in items[:2]:
                print(f"!!     clés de l'item : {sorted(it.keys())}")
                print(f"!!     extrait brut   : {str(it)[:250]}")
        except Exception as e:
            print(f"!!   Ancien endpoint /tracks : erreur {e}")
        try:
            raw2 = sp._get(f"playlists/{SOURCE_PLAYLIST_ID}/items", limit=3)  # nouvel endpoint 02/2026
            items2 = raw2.get("items", [])
            print(f"!!   Nouvel endpoint /items : total={raw2.get('total', '?')}, items page 1={len(items2)}")
            for it in items2[:2]:
                print(f"!!     clés de l'item : {sorted(it.keys())}")
                print(f"!!     extrait brut   : {str(it)[:250]}")
        except Exception as e:
            print(f"!!   Nouvel endpoint /items : erreur {e}")
        print("!!   -> Envoyez ce bloc de diagnostic tel quel pour analyse.\n")

    # ---------------- BPM + genres pour TOUS les titres ----------------
    all_tracks = {t["id"]: t for t in source_tracks}
    for lst in contents.values():
        for t in lst:
            all_tracks.setdefault(t["id"], t)

    print("Récupération des BPM...")
    tempos = get_tempos(sp, list(all_tracks))

    print("Récupération des genres d'artistes...")
    artist_genres = get_artist_genres(sp, [aid for t in all_tracks.values() for aid in t["artist_ids"]])
    print()

    # sets d'IDs par playlist pour tester la présence
    ids_in = {name: {t["id"] for t in lst} for name, lst in contents.items()}

    # =====================================================================
    # 1) PROPOSITIONS D'AJOUT pour la playlist source
    # =====================================================================
    rows_add = []
    to_create_bpm = defaultdict(list)       # {bucket: [titres]} -> playlists BPM à créer
    to_create_genre = defaultdict(list)     # {genre: [titres]}  -> playlists Genre à créer
    unknown_genre_tags = defaultdict(list)  # {genre/tag brut: [titres]} non mappés
    print("=" * 100)
    print("1) TITRES DE LA PLAYLIST SOURCE -> AJOUTS PROPOSÉS")
    print("=" * 100)
    for t in source_tracks:
        tempo = tempos.get(t["id"])
        bucket = bpm_bucket(tempo)
        genre, raw_genres, genre_src = resolve_genre(t, artist_genres)

        # --- cible BPM
        if tempo is None:
            bpm_action = "BPM inconnu (à vérifier manuellement)"
        elif bucket in bpm_playlists:
            pname = bpm_playlists[bucket]["name"]
            bpm_action = f"déjà dans '{pname}'" if t["id"] in ids_in.get(pname, set()) else f"AJOUTER à '{pname}'"
        else:
            bpm_action = f"CRÉER la playlist '{bucket} bpm' puis y ajouter ce titre"
            to_create_bpm[bucket].append(f"{t['name']} — {t['artists']}")

        alt = ""
        if SHOW_HALF_DOUBLE_TEMPO and tempo:
            alts = [f"{x} bpm" for x in {bpm_bucket(tempo / 2), bpm_bucket(tempo * 2)} - {bucket} if x in bpm_playlists]
            if alts:
                alt = f" (alternative demi/double tempo : {', '.join(alts)})"

        # --- cible Genre
        if genre is None:
            genre_action = f"genre non identifié ({'; '.join(raw_genres[:3]) or 'aucune info Spotify ni Last.fm'})"
            for g in raw_genres:
                unknown_genre_tags[g].append(f"{t['name']} — {t['artists']}")
        elif genre in genre_playlists:
            pname = genre_playlists[genre]["name"]
            base = f"déjà dans '{pname}'" if t["id"] in ids_in.get(pname, set()) else f"AJOUTER à '{pname}'"
            genre_action = f"{base} [source: {genre_src}]"
        else:
            genre_action = f"CRÉER la playlist '{genre}' puis y ajouter ce titre [source: {genre_src}]"
            to_create_genre[genre].append(f"{t['name']} — {t['artists']}")

        print(f"- {t['name']} — {t['artists']}")
        print(f"    BPM {tempo if tempo else '?'} -> {bpm_action}{alt}")
        print(f"    Genre -> {genre_action}")
        rows_add.append([t["name"], t["artists"], tempo, bucket, bpm_action, genre or "?", genre_src, genre_action, "; ".join(raw_genres[:5])])

    # =====================================================================
    # 2) TITRES POTENTIELLEMENT MAL PLACÉS
    # =====================================================================
    rows_misplaced = []
    print("\n" + "=" * 100)
    print("2) TITRES POTENTIELLEMENT MAL PLACÉS DANS VOS PLAYLISTS")
    print("=" * 100)

    for bucket_val, info in sorted(bpm_playlists.items()):
        for t in contents[info["name"]]:
            tempo = tempos.get(t["id"])
            if tempo is None:
                continue
            real = bpm_bucket(tempo)
            # tolérance : ok si le bucket exact OU la version demi/double tempo correspond
            if real == bucket_val or bpm_bucket(tempo / 2) == bucket_val or bpm_bucket(tempo * 2) == bucket_val:
                continue
            dest = bpm_playlists.get(real)
            dest_name = dest["name"] if dest else f"{real} bpm (n'existe pas)"
            print(f"- [{info['name']}] {t['name']} — {t['artists']} : BPM mesuré {tempo} -> DÉPLACER vers '{dest_name}'")
            rows_misplaced.append([info["name"], t["name"], t["artists"], tempo, dest_name, "BPM"])

    for gname, info in sorted(genre_playlists.items()):
        for t in contents[info["name"]]:
            genre, raw, src = resolve_genre(t, artist_genres)
            if genre and genre != gname:
                print(f"- [{gname}] {t['name']} — {t['artists']} : genre estimé '{genre}' via {src} ({'; '.join(raw[:3])}) -> à vérifier")
                rows_misplaced.append([gname, t["name"], t["artists"], "; ".join(raw[:5]), genre, f"Genre ({src})"])

    if not rows_misplaced:
        print("Rien à signaler.")

    # =====================================================================
    # 3) DOUBLONS
    # =====================================================================
    rows_dupes = []
    print("\n" + "=" * 100)
    print("3) DOUBLONS À L'INTÉRIEUR DE CHAQUE PLAYLIST")
    print("=" * 100)
    for pname, lst in contents.items():
        seen_id, seen_name = defaultdict(int), defaultdict(int)
        for t in lst:
            seen_id[t["id"]] += 1
            seen_name[(t["name"].lower(), t["artists"].lower())] += 1
        for t in lst:
            key = (t["name"].lower(), t["artists"].lower())
            if seen_id[t["id"]] > 1 or seen_name[key] > 1:
                reason = f"même titre présent {seen_id[t['id']]}x" if seen_id[t["id"]] > 1 else "même nom+artiste (versions/ID différents)"
                print(f"- [{pname}] {t['name']} — {t['artists']} : {reason} -> SUPPRIMER le doublon")
                rows_dupes.append([pname, t["name"], t["artists"], reason])
                seen_id[t["id"]], seen_name[key] = 1, 1  # n'afficher qu'une fois
    if not rows_dupes:
        print("Aucun doublon détecté.")

    # =====================================================================
    # 4) PLAYLISTS À CRÉER (BPM manquants / genres manquants)
    # =====================================================================
    rows_create = []
    print("\n" + "=" * 100)
    print("4) PLAYLISTS À CRÉER (proposition)")
    print("=" * 100)
    for bucket_val in sorted(to_create_bpm):
        print(f"- '{bucket_val} bpm' : {len(to_create_bpm[bucket_val])} titre(s) en attente")
        for x in to_create_bpm[bucket_val]:
            print(f"      . {x}")
            rows_create.append([f"{bucket_val} bpm", "BPM", x])
    for gname in sorted(to_create_genre):
        print(f"- '{gname}' : {len(to_create_genre[gname])} titre(s) en attente")
        for x in to_create_genre[gname]:
            print(f"      . {x}")
            rows_create.append([gname, "Genre", x])
    if not rows_create:
        print("Vos playlists existantes couvrent tous les titres analysés.")

    if unknown_genre_tags:
        print("\nGenres/tags rencontrés mais non mappés vers vos catégories (triés par fréquence — utile pour décider d'une")
        print("éventuelle nouvelle playlist 'grosse maille', ou pour enrichir GENRE_RULES) :")
        for g, titles in sorted(unknown_genre_tags.items(), key=lambda kv: len(kv[1]), reverse=True)[:15]:
            print(f"  - '{g}' : {len(titles)} titre(s)  ex: {titles[0]}")

    # =====================================================================
    # EXPORT CSV
    # =====================================================================
    def write_csv(fname, header, rows):
        path = os.path.join(OUTPUT_DIR, fname)
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(header)
            w.writerows(rows)
        print(f"  -> {path}")

    print("\nExport des rapports CSV :")
    write_csv("1_ajouts_proposes.csv", ["Titre", "Artistes", "BPM", "Bucket BPM", "Action BPM", "Genre estimé",
                                        "Source genre", "Action Genre", "Genres/tags bruts"], rows_add)
    write_csv("2_mal_places.csv", ["Playlist actuelle", "Titre", "Artistes", "Mesure", "Destination suggérée", "Type"], rows_misplaced)
    write_csv("3_doublons.csv", ["Playlist", "Titre", "Artistes", "Raison"], rows_dupes)
    write_csv("4_playlists_a_creer.csv", ["Playlist à créer", "Type", "Titre en attente"], rows_create)

    print("\nTerminé. Aucune modification n'a été faite sur votre compte (dry-run).")

if __name__ == "__main__":
    main()
