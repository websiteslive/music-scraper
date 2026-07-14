import os
import re
import json
import logging
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("unspooler")

app = Flask(__name__)
# Wide open CORS since the frontend is a static site on a different origin (GitHub Pages).
CORS(app, resources={r"/api/*": {"origins": "*"}})

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

# Safety valve: enriching previews means one extra HTTP request per track.
# On a big playlist that's a lot of round trips, so cap how many we bother
# with per request (Render/HF free tiers tend to have a ~30-60s request
# timeout in front of them anyway). Tracks beyond the cap just come back
# with duration/preview set to null and the frontend shows "--:--".
MAX_PREVIEW_ENRICH = 60

# Reduced from 10 to 3 to prevent immediate IP rate-limiting/blocking from YouTube
PREVIEW_FETCH_WORKERS = 3


def extract_playlist_id(url: str) -> str | None:
    m = PLAYLIST_ID_RE.search(url or "")
    return m.group(1) if m else None


def _fetch_next_data(url: str, timeout: int = 15):
    """
    Shared helper: GETs a Spotify embed page and parses the __NEXT_DATA__
    JSON blob out of it. Returns (data, status_code, raw_text).
    """
    resp = requests.get(url, headers=REQUEST_HEADERS, timeout=timeout)
    match = NEXT_DATA_RE.search(resp.text) if resp.status_code == 200 else None
    if not match:
        return None, resp.status_code, resp.text
    try:
        return json.loads(match.group(1)), resp.status_code, resp.text
    except json.JSONDecodeError:
        return None, resp.status_code, resp.text


def scrape_playlist(playlist_id: str, debug: bool = False):
    """
    Fetches Spotify's embeddable playlist page and pulls the playlist name +
    full track list out of its __NEXT_DATA__ JSON blob — the same data
    Spotify's own frontend uses to render the page. No browser needed.
    """
    url = f"https://open.spotify.com/embed/playlist/{playlist_id}"
    debug_info = None

    data, status_code, raw_text = _fetch_next_data(url)

    if debug:
        debug_info = {
            "url": url,
            "status_code": status_code,
            "html_snippet": raw_text[:8000],
        }

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
        })

    return playlist_name, tracks, debug_info


def generate_custom_preview(track_name: str, track_artist: str, track_id: str) -> dict:
    """
    Fallback method: Uses yt-dlp and ffmpeg to fetch the full song from YouTube.
    Requires 'yt-dlp' and 'ffmpeg' installed on the system environment.
    Returns a dictionary containing the 'url' on success, or an 'error' message on failure.
    """
    # Aligned output directory with the Flask serving route (app.root_path)
    output_dir = os.path.join(app.root_path, "static", "previews")
    os.makedirs(output_dir, exist_ok=True)
    output_filename = os.path.join(output_dir, f"{track_id}.mp3")

    # Serve it immediately if we've already cached it
    if os.path.exists(output_filename):
        return {"url": f"/api/previews/{track_id}.mp3", "error": None}

    # Switched to specifically ask for the full song to avoid short promotional clips
    search_query = f"ytsearch1:{track_name} {track_artist} full song official audio"
    try:
        # 1. Get the direct audio URL from YouTube without downloading
        ytdlp_cmd = ["yt-dlp", "-f", "bestaudio", "-g", search_query]
        stream_url = subprocess.check_output(ytdlp_cmd, text=True).strip()

        # 2. Use ffmpeg to grab the full song and save it natively
        ffmpeg_cmd = [
            "ffmpeg", "-y", "-i", stream_url,
            "-c:a", "libmp3lame", output_filename
        ]
        # Added check=True to raise an error if ffmpeg fails
        subprocess.run(ffmpeg_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

        return {"url": f"/api/previews/{track_id}.mp3", "error": None}
    
    except FileNotFoundError as e:
        log.error("Missing system dependency for %s: %s", track_id, e)
        return {"url": None, "error": f"Missing dependency (yt-dlp or ffmpeg not installed): {e}"}
    except subprocess.CalledProcessError as e:
        log.error("Subprocess failed for %s: %s", track_id, e)
        return {"url": None, "error": f"Process failed (YouTube rate limit/block likely): {e}"}
    except Exception as e:
        log.error("Failed to generate custom audio file for %s: %s", track_id, e)
        return {"url": None, "error": str(e)}


def fetch_track_extra(track: dict) -> dict:
    """
    Fetches duration. We explicitly bypass Spotify's native preview URL because
    it forces a short 15-30 second snippet, and instead immediately trigger our 
    own full clip download via YouTube/ffmpeg.
    """
    track_id = track.get("id")
    track_name = track.get("name", "")
    artist_name = track.get("artists", "")
    url = f"https://open.spotify.com/embed/track/{track_id}"
    
    duration_ms = None
    preview_data = {"url": None, "error": None}

    try:
        data, status_code, _ = _fetch_next_data(url)
        if data:
            entity = data["props"]["pageProps"]["state"]["data"]["entity"]
            duration_ms = entity.get("duration")
            # Intentionally ignoring `audioPreview` from Spotify to avoid 15s clips
    except Exception:
        log.exception("Failed to fetch track extra for %s", track_id)

    # Always generate a custom full render from YouTube
    if track_name and artist_name:
        preview_data = generate_custom_preview(track_name, artist_name, track_id)

    return {
        "duration_ms": duration_ms, 
        "preview_url": preview_data.get("url"),
        "preview_error": preview_data.get("error")
    }


def enrich_tracks_with_previews(tracks: list, max_workers: int = PREVIEW_FETCH_WORKERS) -> list:
    """
    Concurrently fetches duration + full audio track for each item. Caps how many 
    tracks get enriched (see MAX_PREVIEW_ENRICH) to keep response times reasonable.
    """
    to_enrich = [t for t in tracks if t.get("id")][:MAX_PREVIEW_ENRICH]
    results = {}

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_id = {
            executor.submit(fetch_track_extra, t): t["id"] for t in to_enrich
        }
        for future in as_completed(future_to_id):
            tid = future_to_id[future]
            try:
                results[tid] = future.result()
            except Exception as e:
                results[tid] = {"duration_ms": None, "preview_url": None, "preview_error": f"Thread failed: {str(e)}"}

    for t in tracks:
        extra = results.get(t.get("id"), {"duration_ms": None, "preview_url": None, "preview_error": None})
        t["duration_ms"] = extra["duration_ms"]
        t["preview_url"] = extra["preview_url"]
        t["preview_error"] = extra["preview_error"]

    return tracks


@app.route("/api/tracks", methods=["POST"])
def api_tracks():
    data = request.get_json(silent=True) or {}
    playlist_url = data.get("playlist_url", "")
    playlist_id = extract_playlist_id(playlist_url)
    debug = bool(data.get("debug", False))
    # Let the frontend opt out of the extra per-track requests if it just
    # wants the fast link list.
    with_previews = bool(data.get("with_previews", True))

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

    if with_previews:
        tracks = enrich_tracks_with_previews(tracks)

    response = {
        "playlist_name": name or "Tracklist",
        "count": len(tracks),
        "tracks": tracks,
        "previews_truncated": with_previews and len(tracks) > MAX_PREVIEW_ENRICH,
    }
    if debug and debug_info:
        response["debug"] = debug_info

    return jsonify(response)


@app.route("/api/previews/<path:filename>")
def serve_custom_preview(filename):
    """Serve the locally rendered mp3 files."""
    return send_from_directory(os.path.join(app.root_path, 'static', 'previews'), filename)


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    # Render assigns a port dynamically via $PORT; Hugging Face Spaces (Docker SDK)
    # expects 7860 specifically, so this falls back to that if PORT isn't set.
    port = int(os.environ.get("PORT", 7860))
    app.run(host="0.0.0.0", port=port)