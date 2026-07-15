from flask import Flask, jsonify, request
from flask_cors import CORS
import threading
import subprocess
import os
import shutil

app = Flask(__name__)
CORS(app) # Allows GitHub Pages to talk to Render

DOWNLOAD_DIR = "downloads"
if not os.path.exists(DOWNLOAD_DIR): os.makedirs(DOWNLOAD_DIR)

# Global status
status = {"progress": 0, "total": 192, "running": False, "msg": "Idle"}

def run_downloader():
    global status
    status["running"] = True
    status["msg"] = "Downloading..."
    
    # Run the user's logic
    subprocess.run(["spotdl", "download", "https://open.spotify.com/playlist/0Zm7WxSd6Oirjk8oc8ekx5", 
                    "--output", DOWNLOAD_DIR, "--format", "mp3"])
    
    status["running"] = False
    status["msg"] = "Done"

@app.route('/start', methods=['POST'])
def start():
    if not status["running"]:
        threading.Thread(target=run_downloader).start()
        return jsonify({"status": "started"})
    return jsonify({"status": "already_running"})

@app.route('/status', methods=['GET'])
def get_status():
    count = len([f for f in os.listdir(DOWNLOAD_DIR) if f.endswith('.mp3')])
    return jsonify({"count": count, "running": status["running"]})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)