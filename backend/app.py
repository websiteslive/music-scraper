import os
import re
import json
import logging

import requests
from flask import Flask, request, jsonify
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


def extract_playlist_id(url: str) -> str | None:
    m = PLAYLIST_ID_RE.search(url or "")
    return m.group(1) if m else None


def scrape_playlist(playlist_id: str, debug: bool = False):
    """
    Fetches Spotify's embeddable playlist page and pulls the playlist name +
    full track list out of its __NEXT_DATA__ JSON blob — the same data
    Spotify's own frontend uses to render the page. No browser needed.
    """
    url = f"https://open.spotify.com/embed/playlist/{playlist_id}"
    debug_info = None

    resp = requests.get(url, headers=REQUEST_HEADERS, timeout=15)

    if debug:
        debug_info = {
            "url": url,
            "status_code": resp.status_code,
            "html_snippet": resp.text[:8000],
        }

    if resp.status_code != 200:
        err = RuntimeError(
            "Spotify didn't return a track list for this playlist. "
            "It may be private, empty, or region-locked."
        )
        err.debug_info = debug_info
        raise err

    match = NEXT_DATA_RE.search(resp.text)
    if not match:
        err = RuntimeError(
            "Spotify didn't return a track list for this playlist. "
            "It may be private, empty, or region-locked."
        )
        err.debug_info = debug_info
        raise err

    try:
        data = json.loads(match.group(1))
        entity = data["props"]["pageProps"]["state"]["data"]["entity"]
    except (json.JSONDecodeError, KeyError, TypeError):
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
            "name": t.get("title") or "Unknown title",
            "artists": t.get("subtitle") or "Unknown artist",
            "link": link,
        })

    return playlist_name, tracks, debug_info


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

    response = {
        "playlist_name": name or "Tracklist",
        "count": len(tracks),
        "tracks": tracks,
    }
    if debug and debug_info:
        response["debug"] = debug_info

    return jsonify(response)


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    # Render assigns a port dynamically via $PORT; Hugging Face Spaces (Docker SDK)
    # expects 7860 specifically, so this falls back to that if PORT isn't set.
    port = int(os.environ.get("PORT", 7860))
    app.run(host="0.0.0.0", port=port)
