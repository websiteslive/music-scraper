import os
import re
import io
import json
import logging
import yt_dlp
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS

try:
    from PIL import Image
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("unspooler")

app = Flask(__name__)
CORS(app)  # Open to all origins to bypass potential CORS blocks from self-hosted frontends

PLAYLIST_ID_RE = re.compile(r"playlist[/:]([a-zA-Z0-9]+)")
NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
    re.DOTALL,
)

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# This caps how many tracks get a lightweight metadata pass (duration + cover art)
MAX_METADATA_ENRICH = 200
METADATA_FETCH_WORKERS = 6

OUTPUT_DIR = os.path.join(app.root_path, "static", "previews")
os.makedirs(OUTPUT_DIR, exist_ok=True)


def extract_playlist_id(url: str) -> str | None:
    m = PLAYLIST_ID_RE.search(url or "")
    return m.group(1) if m else None


def _fetch_next_data(url: str, timeout: int = 15):
    resp = requests.get(url, headers=REQUEST_HEADERS, timeout=timeout)
    match = NEXT_DATA_RE.search(resp.text) if resp.status_code == 200 else None
    if not match:
        return None, resp.status_code, resp.text
    try:
        return json.loads(match.group(1)), resp.status_code, resp.text
    except json.JSONDecodeError:
        return None, resp.status_code, resp.text


def _best_cover_url(cover_art: dict | None) -> str | None:
    """Picks the largest available image from a Spotify coverArt.sources list."""
    if not cover_art:
        return None
    sources = cover_art.get("sources") or []
    if not sources:
        return None
    try:
        best = max(sources, key=lambda s: s.get("width") or 0)
    except (TypeError, ValueError):
        best = sources[0]
    return best.get("url")


def scrape_playlist(playlist_id: str, debug: bool = False):
    url = f"https://open.spotify.com/embed/playlist/{playlist_id}"
    debug_info = None

    data, status_code, raw_text = _fetch_next_data(url)

    if debug:
        debug_info = {"url": url, "status_code": status_code, "html_snippet": raw_text[:8000]}

    if status_code != 200 or data is None:
        err = RuntimeError(
            "Spotify didn't return a track list for this playlist. "
            "It may be private, empty, or region-locked."
        )
        err.debug_info = debug_info
        raise err

    try:
        entity = data["props"]["pageProps"]["state"]["data"]["entity"]
    except (KeyError, TypeError):
        err = RuntimeError(
            "Spotify didn't return a track list for this playlist. "
            "It may be private, empty, or region-locked."
        )
        err.debug_info = debug_info
        raise err

    playlist_name = entity.get("title") or entity.get("name")
    track_list = entity.get("trackList") or []

    tracks = []
    for t in track_list:
        uri = t.get("uri", "")
        track_id = uri.split(":")[-1] if uri else None
        link = f"https://open.spotify.com/embed/track/{track_id}" if track_id else ""
        tracks.append({
            "id": track_id,
            "name": t.get("title") or "Unknown title",
            "artists": t.get("subtitle") or "Unknown artist",
            "link": link,
            "cover_url": _best_cover_url(t.get("coverArt")),
        })

    return playlist_name, tracks, debug_info


def fetch_track_metadata(track: dict) -> dict:
    """Lightweight per-track fetch: duration + fallback cover art. No yt-dlp/ffmpeg."""
    track_id = track.get("id")
    url = f"https://open.spotify.com/embed/track/{track_id}"

    duration_ms = None
    cover_url = track.get("cover_url")

    try:
        data, status_code, _ = _fetch_next_data(url)
        if data:
            entity = data["props"]["pageProps"]["state"]["data"]["entity"]
            duration_ms = entity.get("duration")
            if not cover_url:
                cover_url = _best_cover_url(entity.get("coverArt"))
    except Exception:
        log.exception("Failed to fetch track metadata for %s", track_id)

    return {"duration_ms": duration_ms, "cover_url": cover_url}


def enrich_tracks_with_metadata(tracks: list, max_workers: int = METADATA_FETCH_WORKERS) -> list:
    to_enrich = [t for t in tracks if t.get("id")][:MAX_METADATA_ENRICH]
    results = {}

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_id = {executor.submit(fetch_track_metadata, t): t["id"] for t in to_enrich}
        for future in as_completed(future_to_id):
            tid = future_to_id[future]
            try:
                results[tid] = future.result()
            except Exception:
                results[tid] = {"duration_ms": None, "cover_url": None}

    for t in tracks:
        extra = results.get(t.get("id"), {"duration_ms": None, "cover_url": t.get("cover_url")})
        t["duration_ms"] = extra["duration_ms"]
        if extra.get("cover_url"):
            t["cover_url"] = extra["cover_url"]

    return tracks


def generate_custom_preview(track_name: str, track_artist: str, track_id: str, duration_ms: int = None) -> dict:
    """
    Searches SoundCloud with strict studio-matching filters.
    Discards pitched, sped-up, slowed, and cover edits.
    """
    output_filename = os.path.join(OUTPUT_DIR, f"{track_id}.mp3")
    output_template = os.path.join(OUTPUT_DIR, f"{track_id}.%(ext)s")

    # If already downloaded, return immediately
    if os.path.exists(output_filename):
        return {"url": f"/api/previews/{track_id}.mp3", "error": None}

    # Search up to 10 SoundCloud tracks to inspect candidates
    search_query = f"scsearch10:{track_name} {track_artist}"
    target_duration = duration_ms / 1000 if duration_ms else None

    # Sunnify's Match Filter: Strict duration checks and bedroom-edit blocklist
    def match_filter(info, *, incomplete):
        if not target_duration:
            return None # Skip validation if we don't have the original track duration

        video_duration = info.get('duration')
        if not video_duration:
            return None

        # 1. ULTRA-STRICT DURATION CHECK
        # Studio tracks rarely deviate from their official length by more than 4 seconds.
        # Sped-up, slowed-down, or extended edits will fail this window.
        if abs(video_duration - target_duration) > 4:
            return f"Duration mismatch: found {video_duration}s, expected {target_duration}s"

        # 2. BEDROOM-EDIT BLACKLIST
        # Weed out common SoundCloud alterations unless they are part of the official track details.
        title = (info.get('title') or '').lower()
        orig_name_lower = track_name.lower()
        orig_artist_lower = track_artist.lower()

        blacklist_terms = [
            "sped up", "slowed", "reverb", "nightcore", "cover", "remix", 
            "instrumental", "karaoke", "tribute", "mashup", "pitch", "edit", "8d"
        ]
        
        for term in blacklist_terms:
            # If the term is found in SoundCloud's title but is NOT in the official title/artist, block it.
            if term in title and term not in orig_name_lower and term not in orig_artist_lower:
                return f"Unauthentic version detected (contains '{term}')"

        return None

    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': output_template,
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '320',
        }],
        'match_filter': match_filter,
        'max_downloads': 1, 
        'noplaylist': True,
        'quiet': True,
        'no_warnings': True,
        'ignoreerrors': True,  # Bypasses blocked files to keep scanning down the search list
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([search_query])
            
    except Exception as e:
        if os.path.exists(output_filename):
            pass 
        else:
            log.error("Failed to generate preview for %s: %s", track_id, e)
            err_msg = str(e).replace("ERROR: ", "")
            
            if "Duration mismatch" in err_msg or "Unauthentic version" in err_msg:
                return {"url": None, "error": "No results matched the official studio master."}
                
            return {"url": None, "error": err_msg}

    # Final verification
    if not os.path.exists(output_filename):
        return {"url": None, "error": "No valid, authentic studio matches found."}

    return {"url": f"/api/previews/{track_id}.mp3", "error": None}


def save_cover_webp(track_id: str, cover_url: str) -> dict:
    """Downloads a track's cover art and saves it as a .webp for the spooler zip."""
    output_filename = os.path.join(OUTPUT_DIR, f"{track_id}_cover.webp")

    if os.path.exists(output_filename):
        return {"url": f"/api/previews/{track_id}_cover.webp", "error": None}

    if not PIL_AVAILABLE:
        return {"url": None, "error": "Pillow isn't installed on the server (add 'Pillow' to requirements.txt)."}

    try:
        resp = requests.get(cover_url, headers=REQUEST_HEADERS, timeout=15)
        resp.raise_for_status()
        img = Image.open(io.BytesIO(resp.content)).convert("RGB")
        img.save(output_filename, "WEBP", quality=90)
        return {"url": f"/api/previews/{track_id}_cover.webp", "error": None}
    except Exception as e:
        log.error("Failed to save cover art for %s: %s", track_id, e)
        return {"url": None, "error": str(e)}


@app.route("/api/tracks", methods=["POST"])
def api_tracks():
    data = request.get_json(silent=True) or {}
    playlist_url = data.get("playlist_url", "")
    playlist_id = extract_playlist_id(playlist_url)
    debug = bool(data.get("debug", False))

    if not playlist_id:
        return jsonify({"error": "That doesn't look like a Spotify playlist link."}), 400

    try:
        name, tracks, debug_info = scrape_playlist(playlist_id, debug=debug)
    except RuntimeError as e:
        payload = {"error": str(e)}
        if debug and getattr(e, "debug_info", None):
            payload["debug"] = e.debug_info
        return jsonify(payload), 502
    except requests.RequestException as e:
        log.exception("Request to Spotify failed")
        return jsonify({"error": f"Couldn't reach Spotify: {e}"}), 502
    except Exception as e:
        log.exception("Scrape failed")
        return jsonify({"error": f"Scrape failed: {e}"}), 500

    if not tracks:
        payload = {"error": "No tracks found — the playlist may be private or empty."}
        if debug and debug_info:
            payload["debug"] = debug_info
        return jsonify(payload), 404

    # Fast pass only: duration + cover art. No audio generation here anymore.
    tracks = enrich_tracks_with_metadata(tracks)

    response = {"playlist_name": name or "Tracklist", "count": len(tracks), "tracks": tracks}
    if debug and debug_info:
        response["debug"] = debug_info

    return jsonify(response)


@app.route("/api/track/<track_id>/media", methods=["POST"])
def api_track_media(track_id):
    """
    On-demand, single-track endpoint. Generates the full-song mp3 and, if a
    cover_url is provided, saves cover art as .webp. Called only when the
    user wants to play/download/zip that specific track.
    """
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    artist = (data.get("artists") or "").strip()
    cover_url = data.get("cover_url")
    duration_ms = data.get("duration_ms")

    result = {"preview_url": None, "preview_error": None, "cover_url": None, "cover_error": None}

    if not name or not artist:
        result["preview_error"] = "Missing track name/artist."
    else:
        preview = generate_custom_preview(name, artist, track_id, duration_ms)
        result["preview_url"] = preview.get("url")
        result["preview_error"] = preview.get("error")

    if cover_url:
        cover = save_cover_webp(track_id, cover_url)
        result["cover_url"] = cover.get("url")
        result["cover_error"] = cover.get("error")

    return jsonify(result)


@app.route("/api/previews/<path:filename>")
def serve_custom_preview(filename):
    return send_from_directory(OUTPUT_DIR, filename)


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "pillow": PIL_AVAILABLE})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 7860))
    app.run(host="0.0.0.0", port=port)