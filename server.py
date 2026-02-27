from flask import Flask, jsonify, request, send_file
from flask_cors import CORS
import yt_dlp
import os
import tempfile

app = Flask(__name__)
CORS(app)

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
            entries = info.get('entries', []) or []
            results = []
            for e in entries:
                if not e:
                    continue
                duration_s = int(e.get('duration') or 0)
                m = duration_s // 60
                s = duration_s % 60
                thumbs = e.get('thumbnails') or []
                thumb  = thumbs[-1].get('url', '') if thumbs else e.get('thumbnail', '')
                sc_url = e.get('webpage_url') or e.get('url', '')
                results.append({
                    'id':       str(e.get('id', '')),
                    'url':      sc_url,
                    'title':    e.get('title', 'Sin titulo'),
                    'artist':   e.get('uploader') or e.get('channel') or 'Desconocido',
                    'duration': f"{m}:{str(s).zfill(2)}",
                    'thumb':    thumb,
                    'source':   'soundcloud',
                })
            return jsonify(results)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/stream')
def stream():
    url = request.args.get('url', '').strip()
    if not url:
        return jsonify({'error': 'No URL'}), 400

    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'extract_flat': False,
        'format': 'bestaudio',
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            formats = info.get('formats') or []
            audio_url = None
            for f in reversed(formats):
                if f.get('protocol') in ('https', 'http') and f.get('url'):
                    audio_url = f.get('url')
                    break
            if not audio_url:
                audio_url = info.get('url')
            if not audio_url:
                return jsonify({'error': 'No stream URL'}), 404
            return jsonify({
                'url':      audio_url,
                'title':    info.get('title', ''),
                'duration': info.get('duration', 0),
            })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/download')
def download():
    """Descarga y comprime el audio a 64kbps mp3"""
    url   = request.args.get('url', '').strip()
    title = request.args.get('title', 'song').strip()
    if not url:
        return jsonify({'error': 'No URL'}), 400

    # limpiar nombre de archivo
    safe_title = "".join(c for c in title if c.isalnum() or c in (' ', '-', '_')).strip()
    if not safe_title:
        safe_title = 'song'

    tmp_dir  = tempfile.mkdtemp()
    out_path = os.path.join(tmp_dir, f'{safe_title}.mp3')

    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'format': 'bestaudio',
        'outtmpl': out_path.replace('.mp3', '.%(ext)s'),
        'postprocessors': [{
            'key':            'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '64',   # 64kbps = ~30MB/hora, buena calidad
        }],
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        # buscar el archivo generado
        mp3_file = None
        for f in os.listdir(tmp_dir):
            if f.endswith('.mp3'):
                mp3_file = os.path.join(tmp_dir, f)
                break

        if not mp3_file or not os.path.exists(mp3_file):
            return jsonify({'error': 'File not created'}), 500

        return send_file(
            mp3_file,
            mimetype='audio/mpeg',
            as_attachment=True,
            download_name=f'{safe_title}.mp3',
        )
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        # limpiar archivos temporales
        try:
            for f in os.listdir(tmp_dir):
                os.remove(os.path.join(tmp_dir, f))
            os.rmdir(tmp_dir)
        except:
            pass


@app.route('/ping')
def ping():
    return jsonify({'status': 'ok'})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)