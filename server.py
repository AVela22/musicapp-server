from flask import Flask, jsonify, request
from flask_cors import CORS
import yt_dlp
import re
import os
import requests

app = Flask(__name__)
CORS(app)

def is_valid_video_id(video_id):
    return bool(re.match(r'^[a-zA-Z0-9_-]{11}$', video_id))

# ── YouTube ───────────────────────────────────────────────────────────────
@app.route('/audio')
def get_audio():
    video_id = request.args.get('id', '')
    if not is_valid_video_id(video_id):
        return jsonify({'error': 'Invalid video ID'}), 400

    url = f'https://www.youtube.com/watch?v={video_id}'
    ydl_opts = {
        'format': 'bestaudio[ext=m4a]/bestaudio/best',
        'quiet': True,
        'no_warnings': True,
        'extract_flat': False,
        'extractor_args': {'youtube': {'player_client': ['android', 'web']}},
    }
    # usar cookies si existen
    if os.path.exists('cookies.txt'):
        ydl_opts['cookiefile'] = 'cookies.txt'

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            audio_url = info.get('url')
            if not audio_url:
                return jsonify({'error': 'No audio URL found'}), 404
            return jsonify({
                'url':      audio_url,
                'title':    info.get('title', ''),
                'duration': info.get('duration', 0),
            })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ── SoundCloud ────────────────────────────────────────────────────────────
SC_CLIENT_ID = None

def get_sc_client_id():
    """Obtiene el client_id publico de SoundCloud scrapeando su web"""
    global SC_CLIENT_ID
    if SC_CLIENT_ID:
        return SC_CLIENT_ID
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        r = requests.get('https://soundcloud.com', headers=headers, timeout=10)
        # buscar URLs de scripts
        scripts = re.findall(r'src="(https://a-v2\.sndcdn\.com/assets/[^"]+\.js)"', r.text)
        for script_url in scripts[-3:]:
            sr = requests.get(script_url, headers=headers, timeout=10)
            match = re.search(r'client_id:"([a-zA-Z0-9]+)"', sr.text)
            if match:
                SC_CLIENT_ID = match.group(1)
                return SC_CLIENT_ID
    except Exception as e:
        print(f'SC client_id error: {e}')
    return None

@app.route('/sc/search')
def sc_search():
    query = request.args.get('q', '')
    if not query:
        return jsonify({'error': 'No query'}), 400

    client_id = get_sc_client_id()
    if not client_id:
        return jsonify({'error': 'Could not get SoundCloud client_id'}), 500

    try:
        r = requests.get('https://api-v2.soundcloud.com/search/tracks', params={
            'q':         query,
            'client_id': client_id,
            'limit':     8,
        }, timeout=10)
        data = r.json()
        tracks = []
        for t in data.get('collection', []):
            tracks.append({
                'id':       str(t['id']),
                'title':    t['title'],
                'artist':   t['user']['username'],
                'duration': fmt_duration(t['duration'] // 1000),
                'thumb':    t.get('artwork_url', '').replace('large', 't300x300'),
                'source':   'soundcloud',
            })
        return jsonify(tracks)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/sc/stream')
def sc_stream():
    track_id = request.args.get('id', '')
    if not track_id:
        return jsonify({'error': 'No track ID'}), 400

    client_id = get_sc_client_id()
    if not client_id:
        return jsonify({'error': 'Could not get SoundCloud client_id'}), 500

    try:
        # obtener info del track
        r = requests.get(f'https://api-v2.soundcloud.com/tracks/{track_id}',
                         params={'client_id': client_id}, timeout=10)
        track = r.json()

        # obtener URL de stream
        media = track.get('media', {}).get('transcodings', [])
        stream_url = None
        for m in media:
            if m.get('format', {}).get('protocol') == 'progressive':
                sr = requests.get(m['url'], params={'client_id': client_id}, timeout=10)
                stream_url = sr.json().get('url')
                break

        if not stream_url:
            return jsonify({'error': 'No stream URL found'}), 404

        return jsonify({'url': stream_url})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

def fmt_duration(secs):
    m = secs // 60
    s = secs % 60
    return f"{m}:{str(s).zfill(2)}"

@app.route('/ping')
def ping():
    return jsonify({'status': 'ok'})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)