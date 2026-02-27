from flask import Flask, jsonify, request
from flask_cors import CORS
import yt_dlp
import requests
import re
import os

app = Flask(__name__)
CORS(app)

# ── SoundCloud search ─────────────────────────────────────────────────────
@app.route('/search')
def search():
    query = request.args.get('q', '').strip()
    if not query:
        return jsonify({'error': 'No query'}), 400

    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'extract_flat': True,
        'playlist_items': '1-8',
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f'scsearch8:{query}', download=False)
            entries = info.get('entries', [])
            results = []
            for e in entries:
                if not e:
                    continue
                duration_s = e.get('duration') or 0
                m = int(duration_s) // 60
                s = int(duration_s) % 60
                results.append({
                    'id':       e.get('id') or e.get('url', ''),
                    'url':      e.get('url', ''),
                    'title':    e.get('title', 'Sin titulo'),
                    'artist':   e.get('uploader') or e.get('channel') or 'Desconocido',
                    'duration': f"{m}:{str(s).zfill(2)}",
                    'thumb':    e.get('thumbnail') or e.get('thumbnails', [{}])[-1].get('url', '') if e.get('thumbnails') else '',
                    'source':   'soundcloud',
                })
            return jsonify(results)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ── SoundCloud stream URL ─────────────────────────────────────────────────
@app.route('/stream')
def stream():
    url = request.args.get('url', '').strip()
    if not url:
        return jsonify({'error': 'No URL'}), 400

    ydl_opts = {
        'format': 'bestaudio/best',
        'quiet': True,
        'no_warnings': True,
        'extract_flat': False,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            audio_url = info.get('url')
            if not audio_url:
                return jsonify({'error': 'No stream URL found'}), 404
            return jsonify({
                'url':      audio_url,
                'title':    info.get('title', ''),
                'duration': info.get('duration', 0),
            })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/ping')
def ping():
    return jsonify({'status': 'ok'})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)