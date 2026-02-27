from flask import Flask, jsonify, request, send_file, Response
from flask_cors import CORS
import yt_dlp
import os
import tempfile
import requests as req

app = Flask(__name__)
CORS(app)

@app.route('/search')
def search():
    query = request.args.get('q', '').strip()
    if not query:
        return jsonify({'error': 'No query'}), 400
    ydl_opts = {
        'quiet': True, 'no_warnings': True,
        'extract_flat': True, 'playlist_items': '1-8',
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info    = ydl.extract_info(f'scsearch8:{query}', download=False)
            entries = info.get('entries', []) or []
            results = []
            for e in entries:
                if not e: continue
                duration_s = int(e.get('duration') or 0)
                thumbs = e.get('thumbnails') or []
                thumb  = thumbs[-1].get('url','') if thumbs else e.get('thumbnail','')
                results.append({
                    'id':       str(e.get('id','')),
                    'url':      e.get('webpage_url') or e.get('url',''),
                    'title':    e.get('title','Sin titulo'),
                    'artist':   e.get('uploader') or e.get('channel') or 'Desconocido',
                    'duration': f"{duration_s//60}:{str(duration_s%60).zfill(2)}",
                    'thumb':    thumb,
                    'source':   'soundcloud',
                })
            return jsonify(results)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/stream')
def stream():
    url = request.args.get('url','').strip()
    if not url:
        return jsonify({'error': 'No URL'}), 400
    ydl_opts = {
        'quiet': True, 'no_warnings': True,
        'extract_flat': False, 'format': 'bestaudio',
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info    = ydl.extract_info(url, download=False)
            formats = info.get('formats') or []
            audio_url = None
            for f in reversed(formats):
                if f.get('protocol') in ('https','http') and f.get('url'):
                    audio_url = f.get('url')
                    break
            if not audio_url:
                audio_url = info.get('url')
            if not audio_url:
                return jsonify({'error': 'No stream URL'}), 404
            return jsonify({'url': audio_url, 'title': info.get('title',''), 'duration': info.get('duration',0)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/download')
def download():
    url   = request.args.get('url','').strip()
    title = request.args.get('title','song').strip()
    if not url:
        return jsonify({'error': 'No URL'}), 400

    safe_title = "".join(c for c in title if c.isalnum() or c in (' ','-','_')).strip() or 'song'

    # obtener URL de audio directo via yt-dlp
    ydl_opts = {
        'quiet': True, 'no_warnings': True,
        'extract_flat': False, 'format': 'bestaudio',
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info    = ydl.extract_info(url, download=False)
            formats = info.get('formats') or []
            audio_url = None
            # preferir mp3 o m4a progresivo
            for f in reversed(formats):
                if f.get('protocol') in ('https','http') and f.get('url'):
                    audio_url = f.get('url')
                    break
            if not audio_url:
                audio_url = info.get('url')
            if not audio_url:
                return jsonify({'error': 'No audio URL'}), 404

        # hacer proxy del archivo al cliente
        headers = {
            'User-Agent': 'Mozilla/5.0',
            'Referer': 'https://soundcloud.com',
        }
        r = req.get(audio_url, headers=headers, stream=True, timeout=30)
        if r.status_code != 200:
            return jsonify({'error': f'Upstream error {r.status_code}'}), 502

        content_type = r.headers.get('Content-Type', 'audio/mpeg')

        def generate():
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    yield chunk

        response = Response(
            generate(),
            status=200,
            mimetype='audio/mpeg',
            headers={
                'Content-Disposition': f'attachment; filename="{safe_title}.mp3"',
                'Content-Type': 'audio/mpeg',
            }
        )
        return response

    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/ping')
def ping():
    return jsonify({'status': 'ok'})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)