---
title: Unspooler Backend
emoji: 🎞️
colorFrom: yellow
colorTo: gray
sdk: docker
app_port: 7860
pinned: false
---

# Unspooler backend

Flask API that scrapes a public Spotify playlist page with a headless
Chromium (via Playwright) and returns each track's name, artist(s), and
direct `open.spotify.com/track/...` link — no Spotify API credentials
required.

## Endpoint

`POST /api/tracks`

Request body:
```json
{ "playlist_url": "https://open.spotify.com/playlist/xxxxxxxx" }
```

Response body:
```json
{
  "playlist_name": "Today's Top Hits",
  "count": 50,
  "tracks": [
    { "name": "Song Title", "artists": "Artist Name", "link": "https://open.spotify.com/track/..." }
  ]
}
```

`GET /api/health` returns `{"status": "ok"}` for uptime checks.

## Notes

- Only works on **public** playlists — private/collaborative playlists require a logged-in session, which this doesn't do.
- Scraping is inherently more fragile than the official Web API: if Spotify changes its page structure, the selectors in `app.py` (`data-testid="tracklist-row"` / `internal-track-link`) may need updating.
- Each request launches a fresh headless browser, so responses take a few seconds, longer for large playlists (the tracklist is virtualized, so the scraper scrolls to force every row to render).
- Free-tier Spaces sleep after inactivity — the first request after a while will be slow while it wakes up.

## Deploying

1. Create a new [Space](https://huggingface.co/new-space) → SDK: **Docker**.
2. Push these files (`app.py`, `requirements.txt`, `Dockerfile`, this `README.md`) to the Space's repo.
3. Once it builds, your API lives at `https://<your-username>-<space-name>.hf.space`.
4. Paste that base URL into the frontend's "Backend URL" field.
