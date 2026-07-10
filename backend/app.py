import os
import re
import time
import logging

from flask import Flask, request, jsonify
from flask_cors import CORS
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("unspooler")

app = Flask(__name__)
# Wide open CORS since the frontend is a static site on a different origin (GitHub Pages).
CORS(app, resources={r"/api/*": {"origins": "*"}})

PLAYLIST_ID_RE = re.compile(r"playlist[/:]([a-zA-Z0-9]+)")
TRACKLIST_ROW = "[data-testid='tracklist-row']"


def extract_playlist_id(url: str) -> str | None:
    m = PLAYLIST_ID_RE.search(url or "")
    return m.group(1) if m else None


def scrape_playlist(playlist_id: str, max_scroll_attempts: int = 60, stall_limit: int = 4):
    """
    Loads the public Spotify playlist page in a headless browser, scrolls the
    virtualized track list to force every row to mount, and pulls track name,
    artist, and canonical open.spotify.com link out of the rendered DOM.
    """
    url = f"https://open.spotify.com/playlist/{playlist_id}"
    tracks = []
    seen_hrefs = set()
    playlist_name = None

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",  # avoid /dev/shm OOM on small containers (e.g. Render free tier)
            ],
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 1800},
        )
        page = context.new_page()

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)

            # Dismiss the cookie/consent banner if present, it can intercept clicks/scrolls.
            for selector in ["button:has-text('Accept')", "button:has-text('Decline')"]:
                try:
                    page.locator(selector).first.click(timeout=2000)
                    break
                except Exception:
                    pass

            try:
                page.wait_for_selector(TRACKLIST_ROW, timeout=15000)
            except PlaywrightTimeoutError:
                raise RuntimeError(
                    "Spotify didn't return a track list for this playlist. "
                    "It may be private, empty, or region-locked."
                )

            try:
                playlist_name = page.locator("h1").first.inner_text(timeout=3000)
            except Exception:
                playlist_name = None

            stall_count = 0
            for _ in range(max_scroll_attempts):
                rows = page.locator(TRACKLIST_ROW)
                count = rows.count()
                new_this_pass = 0

                for i in range(count):
                    row = rows.nth(i)
                    try:
                        link_el = row.locator("a[data-testid='internal-track-link']").first
                        href = link_el.get_attribute("href", timeout=1000)
                    except Exception:
                        href = None

                    if not href or href in seen_hrefs:
                        continue
                    seen_hrefs.add(href)
                    new_this_pass += 1

                    try:
                        name = link_el.inner_text(timeout=1000).strip()
                    except Exception:
                        name = "Unknown title"

                    artist_names = []
                    try:
                        artist_links = row.locator("a[href*='/artist/']")
                        for j in range(artist_links.count()):
                            t = artist_links.nth(j).inner_text(timeout=1000).strip()
                            if t:
                                artist_names.append(t)
                    except Exception:
                        pass

                    full_url = href if href.startswith("http") else f"https://open.spotify.com{href}"
                    tracks.append({
                        "name": name,
                        "artists": ", ".join(artist_names) if artist_names else "Unknown artist",
                        "link": full_url,
                    })

                if new_this_pass == 0:
                    stall_count += 1
                    if stall_count >= stall_limit:
                        break
                else:
                    stall_count = 0

                page.mouse.wheel(0, 1600)
                time.sleep(0.35)

        finally:
            context.close()
            browser.close()

    return playlist_name, tracks


@app.route("/api/tracks", methods=["POST"])
def api_tracks():
    data = request.get_json(silent=True) or {}
    playlist_url = data.get("playlist_url", "")
    playlist_id = extract_playlist_id(playlist_url)

    if not playlist_id:
        return jsonify({"error": "That doesn't look like a Spotify playlist link."}), 400

    try:
        name, tracks = scrape_playlist(playlist_id)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 502
    except Exception as e:
        log.exception("Scrape failed")
        return jsonify({"error": f"Scrape failed: {e}"}), 500

    if not tracks:
        return jsonify({"error": "No tracks found — the playlist may be private or empty."}), 404

    return jsonify({
        "playlist_name": name or "Tracklist",
        "count": len(tracks),
        "tracks": tracks,
    })


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    # Render assigns a port dynamically via $PORT; Hugging Face Spaces (Docker SDK)
    # expects 7860 specifically, so this falls back to that if PORT isn't set.
    port = int(os.environ.get("PORT", 7860))
    app.run(host="0.0.0.0", port=port)
