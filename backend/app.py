from flask import Flask, jsonify, request, send_from_directory, send_file
from flask_cors import CORS
import threading
import subprocess
import os

app = Flask(__name__)
CORS(app)

DOWNLOAD_DIR = "downloads"
if not os.path.exists(DOWNLOAD_DIR): os.makedirs(DOWNLOAD_DIR)

status = {"count": 0, "running": False}

# Added url parameter here
def run_downloader(url):
    global status
    status["running"] = True
    # The actual subprocess now uses the dynamic URL
    subprocess.run(["spotdl", "download", url, "--output", DOWNLOAD_DIR, "--format", "mp3"])
    status["running"] = False

@app.route('/start', methods=['POST'])
def start():
    # Capture the URL from the frontend request
    data = request.get_json()
    url = data.get('url')
    
    if not url:
        return jsonify({"status": "error", "message": "No URL provided"}), 400

    if not status["running"]:
        # Pass the url to the thread
        threading.Thread(target=run_downloader, args=(url,)).start()
        return jsonify({"status": "started"})
    
    return jsonify({"status": "already_running"})

@app.route('/status', methods=['GET'])
def get_status():
    files = [f for f in os.listdir(DOWNLOAD_DIR) if f.endswith('.mp3')]
    return jsonify({"count": len(files), "running": status["running"], "files": files})

@app.route('/download/<filename>')
def download_file(filename):
    return send_from_directory(DOWNLOAD_DIR, filename)

@app.route('/zip')
def download_zip():
    return send_file("spooled_library.zip", as_attachment=True)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)