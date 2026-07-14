import os
import re
import io
import json
import logging
import subprocess
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import requests

try:
    from PIL import Image
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("unspooler")

app = Flask(__name__)
# Enable CORS globally to prevent browser CORS blocks ("Failed to fetch")
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

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static_previews")
os.makedirs(OUTPUT_DIR, exist_ok=True)


def scrape_youtube_video_id(query: str) -> str:
    """
    Performs a lightweight, block-resistant HTML search on YouTube
    to find the top video ID for a track.
    """
    try:
        url = f"https://www.youtube.com/results?search_query={requests.utils.quote(query)}"
        r = requests.get(url, headers=REQUEST_HEADERS, timeout=10)
        if r.status_code == 200:
            matches = re.findall(r"watch\?v=([a-zA-Z0-9_-]{11})", r.text)
            if matches:
                return matches[0]
    except Exception as e:
        log.warning("Lightweight YouTube search failed: %s", e)
    return None


def generate_custom_preview(track_name: str, track_artist: str, track_id: str) -> dict:
    """
    Downloads the full MP3 using a 3-Tier bypass system:
    Tier 1: Direct SpotifyDown API (No YouTube blocks, 100% full quality)
    Tier 2: YouTube HTML Scraping + Cobalt Downloader API
    Tier 3: Optimized native yt-dlp (Fallback)
    """
    output_filename = os.path.join(OUTPUT_DIR, f"{track_id}.mp3")

    if os.path.exists(output_filename):
        return {"url": f"/api/previews/{track_id}.mp3", "error": None}

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # --- TIER 1: SpotifyDown API (Fastest & most reliable) ---
    if track_id and len(track_id) == 22:
        try:
            log.info("Tier 1: Querying SpotifyDown for: %s", track_id)
            api_url = f"https://api.spotifydown.com/download/{track_id}"
            headers = {
                "User-Agent": REQUEST_HEADERS["User-Agent"],
                "Referer": "https://spotifydown.com/",
                "Origin": "https://spotifydown.com",
            }
            response = requests.get(api_url, headers=headers, timeout=15)
            if response.status_code == 200:
                data = response.json()
                if data.get("success") and data.get("link"):
                    download_url = data["link"]
                    log.info("SpotifyDown URL found, downloading MP3...")
                    file_resp = requests.get(download_url, headers=headers, timeout=45, stream=True)
                    if file_resp.status_code == 200:
                        with open(output_filename, "wb") as f:
                            for chunk in file_resp.iter_content(chunk_size=8192):
                                if chunk:
                                    f.write(chunk)
                        log.info("Tier 1 successfully downloaded: %s", track_id)
                        return {"url": f"/api/previews/{track_id}.mp3", "error": None}
        except Exception as e:
            log.warning("Tier 1 failed for %s: %s", track_id, e)

    # --- TIER 2: YouTube Search + Cobalt API (Bypasses local IP limits) ---
    try:
        log.info("Tier 2: Scraping YouTube search for: %s - %s", track_name, track_artist)
        search_query = f"{track_name} {track_artist} official audio"
        video_id = scrape_youtube_video_id(search_query)

        if video_id:
            video_url = f"https://www.youtube.com/watch?v={video_id}"
            log.info("Found YouTube Video URL: %s. Handing off to Cobalt...", video_url)
            
            cobalt_url = "https://api.cobalt.tools/api/json"
            cobalt_headers = {
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
            cobalt_payload = {
                "url": video_url,
                "downloadMode": "audio",
                "audioFormat": "mp3",
                "audioQuality": "320"
            }
            
            cobalt_resp = requests.post(cobalt_url, json=cobalt_payload, headers=cobalt_headers, timeout=20)
            if cobalt_resp.status_code == 200:
                cobalt_data = cobalt_resp.json()
                direct_download_url = cobalt_data.get("url")
                if direct_download_url:
                    log.info("Cobalt direct link acquired! Streaming to storage...")
                    file_resp = requests.get(direct_download_url, timeout=45, stream=True)
                    if file_resp.status_code == 200:
                        with open(output_filename, "wb") as f:
                            for chunk in file_resp.iter_content(chunk_size=8192):
                                if chunk:
                                    f.write(chunk)
                        log.info("Tier 2 successfully downloaded: %s", track_id)
                        return {"url": f"/api/previews/{track_id}.mp3", "error": None}
    except Exception as e:
        log.warning("Tier 2 failed for %s: %s", track_id, e)

    # --- TIER 3: Local yt-dlp Native Run (Last Resort fallback) ---
    try:
        log.info("Tier 3: Running local yt-dlp for: %s", track_name)
        search_query = f"ytsearch1:{track_name} {track_artist} official audio"
        ytdlp_cmd = [
            "yt-dlp",
            "--extract-audio",
            "--audio-format", "mp3",
            "--audio-quality", "0",
            "--force-ipv4",
            "--rm-cache-dir",
            "--extractor-args", "youtube:player_client=tv_downgraded,web_creator,web_embedded,android_vr",
            "-o", output_filename,
            search_query
        ]

        subprocess.run(
            ytdlp_cmd, 
            stdout=subprocess.DEVNULL, 
            stderr=subprocess.DEVNULL,
            check=True, 
            timeout=120
        )

        if os.path.exists(output_filename):
            log.info("Tier 3 fallback downloaded track successfully!")
            return {"url": f"/api/previews/{track_id}.mp3", "error": None}

    except FileNotFoundError as e:
        log.error("Missing dependency: %s", e)
        return {"url": None, "error": f"Missing dependency (yt-dlp or ffmpeg not installed): {e}"}
    except Exception as e:
        log.error("All tiers failed to fetch audio for %s: %s", track_id, e)
        return {"url": None, "error": f"All download mechanisms failed: {e}"}


def save_cover_webp(track_id: str, cover_url: str) -> dict:
    """
    Downloads raw image bytes and writes them to a .webp file.
    """
    output_filename = os.path.join(OUTPUT_DIR, f"{track_id}.webp")
    if os.path.exists(output_filename):
        return {"url": f"/api/previews/{track_id}.webp", "error": None}

    try:
        resp = requests.get(cover_url, headers=REQUEST_HEADERS, timeout=15)
        if resp.status_code == 200:
            image_data = resp.content
            if PIL_AVAILABLE:
                try:
                    img = Image.open(io.BytesIO(image_data))
                    img.save(output_filename, format="WEBP", quality=80)
                    return {"url": f"/api/previews/{track_id}.webp", "error": None}
                except Exception as e:
                    log.error("PIL webp conversion failed for %s: %s", track_id, e)

            # Fallback if PIL is not present
            with open(output_filename, "wb") as f:
                f.write(image_data)
            return {"url": f"/api/previews/{track_id}.webp", "error": None}
        else:
            return {"url": None, "error": f"HTTP {resp.status_code}"}
    except Exception as e:
        log.error("Failed to download cover for %s: %s", track_id, e)
        return {"url": None, "error": str(e)}


@app.route("/api/playlist", methods=["POST"])
def api_playlist():
    """
    Accepts a Spotify playlist URL, scrapes metadata natively,
    or falls back to the SpotifyDown playlist API if Spotify blocks the scrape.
    """
    data = request.get_json(silent=True) or {}
    url = data.get("url", "").strip()

    if not url:
        return jsonify({"error": "No URL provided."}), 400

    match = PLAYLIST_ID_RE.search(url)
    if not match:
        return jsonify({"error": "Invalid Spotify URL."}), 400

    playlist_id = match.group(1)

    # --- METHOD 1: Direct Spotify Scraping (Try embed and standard routes) ---
    scrape_urls = [
        f"https://open.spotify.com/embed/playlist/{playlist_id}",
        f"https://open.spotify.com/playlist/{playlist_id}"
    ]

    for scrape_url in scrape_urls:
        try:
            log.info("Attempting to scrape Spotify URL: %s", scrape_url)
            resp = requests.get(scrape_url, headers=REQUEST_HEADERS, timeout=15)
            if resp.status_code == 200:
                script_match = NEXT_DATA_RE.search(resp.text)
                if script_match:
                    next_json = json.loads(script_match.group(1))
                    page_props = next_json.get("props", {}).get("pageProps", {})
                    state = page_props.get("state") or page_props.get("fallback") or {}
                    
                    playlist_key = None
                    for k in state.keys():
                        if "playlist" in k.lower():
                            playlist_key = k
                            break
                    
                    if playlist_key:
                        playlist_obj = state[playlist_key]["data"]["playlistV2"]
                        p_name = playlist_obj.get("name", "Untitled Tape")
                        p_desc = playlist_obj.get("description", "")
                        p_author = playlist_obj.get("ownerV2", {}).get("name", "Unknown Artist")

                        tracks_items = playlist_obj.get("content", {}).get("items", [])
                        tracks = []
                        for index, item in enumerate(tracks_items):
                            item_data = item.get("itemV2", {}).get("data", {})
                            if not item_data:
                                continue

                            t_id = item_data.get("id")
                            t_name = item_data.get("name", "Unknown Track")
                            
                            artists_list = item_data.get("artists", {}).get("items", [])
                            t_artists = ", ".join([a.get("profile", {}).get("name", "Unknown") for a in artists_list])

                            images_list = item_data.get("albumOfTrack", {}).get("coverArt", {}).get("sources", [])
                            t_cover = images_list[0].get("url") if images_list else None

                            duration_ms = item_data.get("duration", {}).get("totalMillisecondsValue", 0)
                            t_duration = f"{int(duration_ms / 60000)}:{int((duration_ms % 60000) / 1000):02d}"

                            tracks.append({
                                "id": t_id or f"track_{index}",
                                "name": t_name,
                                "artists": t_artists,
                                "cover_url": t_cover,
                                "duration": t_duration
                            })

                        log.info("Direct scraping succeeded. Found %d tracks.", len(tracks))
                        return jsonify({
                            "playlist": {
                                "name": p_name,
                                "description": p_desc,
                                "author": p_author,
                                "tracks": tracks
                            }
                        })
        except Exception as e:
            log.warning("Scrape route %s failed: %s", scrape_url, e)

    # --- METHOD 2: Fallback to SpotifyDown API (If Spotify blocks us completely) ---
    try:
        log.info("Direct scraping blocked. Falling back to SpotifyDown API for metadata...")
        tracklist_url = f"https://api.spotifydown.com/tracklist/playlist/{playlist_id}"
        headers = {
            "User-Agent": REQUEST_HEADERS["User-Agent"],
            "Referer": "https://spotifydown.com/",
            "Origin": "https://spotifydown.com",
        }
        resp = requests.get(tracklist_url, headers=headers, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("success"):
                track_list = data.get("trackList", [])
                tracks = []
                for index, t in enumerate(track_list):
                    tracks.append({
                        "id": t.get("id") or f"track_{index}",
                        "name": t.get("title", "Unknown Track"),
                        "artists": t.get("artists", "Unknown Artist"),
                        "cover_url": t.get("cover"),
                        "duration": t.get("duration", "0:00")
                    })
                
                # Retrieve playlist details (title/author)
                meta_url = f"https://api.spotifydown.com/metadata/playlist/{playlist_id}"
                meta_resp = requests.get(meta_url, headers=headers, timeout=10)
                p_name = "Untitled Tape"
                p_author = "Unknown Artist"
                if meta_resp.status_code == 200:
                    meta_data = meta_resp.json()
                    if meta_data.get("success"):
                        p_name = meta_data.get("title", "Untitled Tape")
                        p_author = meta_data.get("artists", "Unknown Artist")

                log.info("SpotifyDown API fallback succeeded. Loaded %d tracks.", len(tracks))
                return jsonify({
                    "playlist": {
                        "name": p_name,
                        "description": "Retrieved via SpotifyDown API Backup",
                        "author": p_author,
                        "tracks": tracks
                    }
                })
    except Exception as e:
        log.error("SpotifyDown playlist fallback failed: %s", e)

    return jsonify({"error": "Failed to retrieve playlist details. Both Spotify direct scraping and our API backup were blocked."}), 500


@app.route("/api/track/<track_id>/media", methods=["POST"])
def api_track_media(track_id):
    """
    On-demand single-track media fetch. Generates the full-song MP3
    and outputs the webp album cover art.
    """
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    artist = (data.get("artists") or "").strip()
    cover_url = data.get("cover_url")

    result = {"preview_url": None, "preview_error": None, "cover_url": None, "cover_error": None}

    if not name or not artist:
        result["preview_error"] = "Missing track name/artist."
    else:
        preview = generate_custom_preview(name, artist, track_id)
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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)