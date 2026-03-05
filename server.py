from flask import Flask, jsonify, request, Response
from flask_cors import CORS
import yt_dlp
import os
import requests as req
import concurrent.futures

app = Flask(__name__)
CORS(app)

# ── Helpers ────────────────────────────────────────────────────────────────

def pick_best_audio(formats):
    """
    Devuelve la URL del mejor formato de audio progresivo (http/https).
    Evita HLS (m3u8) y DASH porque expo-av los reproduce a velocidad incorrecta.
    Prefiere: mp3 > m4a/aac > opus/ogg > cualquier http
    """
    if not formats:
        return None

    def score(f):
        proto = f.get('protocol', '')
        ext   = f.get('ext', '')
        acodec = f.get('acodec', '')
        if proto not in ('https', 'http'):
            return -1                          # descarta HLS/DASH
        if ext == 'mp3' or 'mp3' in acodec:   return 100
        if ext in ('m4a',) or 'aac' in acodec: return 80
        if ext in ('ogg', 'opus') or 'opus' in acodec: return 60
        if f.get('url'):                        return 10
        return -1

    ranked = sorted(formats, key=score, reverse=True)
    for f in ranked:
        if score(f) >= 0 and f.get('url'):
            return f['url']
    return None


def format_duration(seconds):
    s = int(seconds or 0)
    return f"{s // 60}:{str(s % 60).zfill(2)}"


MIN_DURATION_S = 2 * 60       # 2 minutos
MAX_DURATION_S = 10 * 60      # 10 minutos

def entry_to_result(e, source):
    if not e:
        return None
    duration_s = int(e.get('duration') or 0)

    # Filtrar canciones fuera del rango 2–10 minutos
    # (duration_s == 0 significa que no hay info → también se descarta)
    if duration_s < MIN_DURATION_S or duration_s > MAX_DURATION_S:
        return None

    thumbs = e.get('thumbnails') or []
    # YouTube da muchas miniaturas, preferir la de mejor resolución pero no la más grande (lenta)
    thumb = ''
    if thumbs:
        # intentar la de índice -2 (suele ser 480px), si no la última
        candidates = [t for t in thumbs if t.get('url')]
        if len(candidates) >= 2:
            thumb = candidates[-2].get('url', '')
        elif candidates:
            thumb = candidates[-1].get('url', '')
    if not thumb:
        thumb = e.get('thumbnail', '')

    vid_id = str(e.get('id', ''))
    url    = e.get('webpage_url') or e.get('url', '')

    return {
        'id':       f"{source}_{vid_id}",
        'url':      url,
        'title':    e.get('title', 'Sin título'),
        'artist':   e.get('uploader') or e.get('channel') or 'Desconocido',
        'duration': format_duration(duration_s),
        'thumb':    thumb,
        'source':   source,
    }


def search_source(query, source_prefix, n=12):
    """Busca en una fuente (ytsearch / scsearch) y retorna lista de resultados."""
    ydl_opts = {
        'quiet': True, 'no_warnings': True,
        'extract_flat': True,
        'playlist_items': f'1-{n}',
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info    = ydl.extract_info(f'{source_prefix}{n}:{query}', download=False)
            entries = info.get('entries') or []
            results = []
            for e in entries:
                r = entry_to_result(e, source_prefix.replace('search', '').strip(':') or source_prefix)
                if r:
                    results.append(r)
                    if len(results) >= 6:   # máximo 6 por fuente tras filtrar
                        break
            return results
    except Exception as ex:
        print(f'[search_source] {source_prefix} error: {ex}')
        return []


# ── Rutas ──────────────────────────────────────────────────────────────────

@app.route('/search')
def search():
    query = request.args.get('q', '').strip()
    if not query:
        return jsonify({'error': 'No query'}), 400

    # Busca en YouTube y SoundCloud en paralelo (n=12 para tener candidatos tras filtrar)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        fut_yt = executor.submit(search_source, query, 'ytsearch', 12)
        fut_sc = executor.submit(search_source, query, 'scsearch', 12)
        yt_results = fut_yt.result()
        sc_results = fut_sc.result()

    # Intercalar resultados: YT, SC, YT, SC ... para variedad
    merged = []
    for yt, sc in zip(yt_results, sc_results):
        merged.append(yt)
        merged.append(sc)
    # Agregar los sobrantes si una fuente devolvió más
    merged += yt_results[len(sc_results):]
    merged += sc_results[len(yt_results):]

    return jsonify(merged)


@app.route('/stream')
def stream():
    url = request.args.get('url', '').strip()
    if not url:
        return jsonify({'error': 'No URL'}), 400

    is_youtube = 'youtube.com' in url or 'youtu.be' in url

    ydl_opts = {
        'quiet': True, 'no_warnings': True,
        'extract_flat': False,
    }

    # Para YouTube: dejar que yt-dlp elija el mejor audio (puede ser DASH/opus)
    # Para SoundCloud y otros: preferir progresivo http
    if is_youtube:
        ydl_opts['format'] = 'bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio'
    else:
        ydl_opts['format'] = 'bestaudio[protocol^=http]/bestaudio'

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info    = ydl.extract_info(url, download=False)
            formats = info.get('formats') or []

            # Para YouTube usamos la URL que yt-dlp ya procesó (descifró parámetro n)
            if is_youtube:
                audio_url = info.get('url')
                # Si no, intentar pick_best_audio como fallback
                if not audio_url:
                    audio_url = pick_best_audio(formats)
            else:
                audio_url = pick_best_audio(formats)
                if not audio_url:
                    audio_url = info.get('url')

            if not audio_url:
                return jsonify({'error': 'No stream URL'}), 404

            return jsonify({
                'url':      audio_url,
                'title':    info.get('title', ''),
                'duration': info.get('duration', 0),
                'format':   info.get('ext', ''),
            })
    except Exception as e:
        print(f'[stream] error: {e}')
        return jsonify({'error': str(e)}), 500


@app.route('/download')
def download():
    url   = request.args.get('url', '').strip()
    title = request.args.get('title', 'song').strip()
    if not url:
        return jsonify({'error': 'No URL'}), 400

    safe_title = "".join(c for c in title if c.isalnum() or c in (' ', '-', '_')).strip() or 'song'

    ydl_opts = {
        'quiet': True, 'no_warnings': True,
        'extract_flat': False,
        'format': 'bestaudio[protocol^=http]/bestaudio',
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info    = ydl.extract_info(url, download=False)
            formats = info.get('formats') or []

            audio_url = pick_best_audio(formats)
            if not audio_url:
                audio_url = info.get('url')
            if not audio_url:
                return jsonify({'error': 'No audio URL'}), 404

        # Detectar referer según la fuente
        referer = 'https://www.youtube.com/' if 'youtube' in url or 'youtu.be' in url else 'https://soundcloud.com'

        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Referer': referer,
        }
        r = req.get(audio_url, headers=headers, stream=True, timeout=60)
        if r.status_code != 200:
            return jsonify({'error': f'Upstream {r.status_code}'}), 502

        def generate():
            for chunk in r.iter_content(chunk_size=16384):
                if chunk:
                    yield chunk

        return Response(
            generate(),
            status=200,
            mimetype='audio/mpeg',
            headers={
                'Content-Disposition': f'attachment; filename="{safe_title}.mp3"',
                'Content-Type': 'audio/mpeg',
            }
        )
    except Exception as e:
        print(f'[download] error: {e}')
        return jsonify({'error': str(e)}), 500


@app.route('/ping')
def ping():
    return jsonify({'status': 'ok'})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)