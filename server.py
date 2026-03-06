from flask import Flask, jsonify, request, Response
from flask_cors import CORS
import yt_dlp
import os
import requests as req
import concurrent.futures

app = Flask(__name__)
CORS(app)

# ── Helpers ────────────────────────────────────────────────────────────────

YDL_COMMON = {
    'quiet': True,
    'no_warnings': True,
    'extract_flat': False,
    # Solo formatos progresivos (http/https). Nunca HLS/DASH.
    'format': 'bestaudio[protocol^=http]/bestaudio',
    # Spoofear como si viniera de un navegador real
    'http_headers': {
        'User-Agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/124.0.0.0 Safari/537.36'
        ),
        'Accept-Language': 'en-US,en;q=0.9',
    },
}


def pick_best_audio(formats):
    """
    Devuelve el mejor formato de audio progresivo (http/https).
    Evita HLS (m3u8) y DASH.
    Prefiere: mp3 > m4a/aac > opus/ogg > cualquier http
    """
    if not formats:
        return None

    def score(f):
        proto  = f.get('protocol', '')
        ext    = f.get('ext', '')
        acodec = f.get('acodec', '')
        if proto not in ('https', 'http'):
            return -1
        if ext == 'mp3' or 'mp3' in acodec:        return 100
        if ext in ('m4a',) or 'aac' in acodec:     return 80
        if ext in ('ogg', 'opus') or 'opus' in acodec: return 60
        if f.get('url'):                             return 10
        return -1

    ranked = sorted(formats, key=score, reverse=True)
    for f in ranked:
        if score(f) >= 0 and f.get('url'):
            return f['url'], f
    return None, None


def format_duration(seconds):
    s = int(seconds or 0)
    return f"{s // 60}:{str(s % 60).zfill(2)}"


def entry_to_result(e, source):
    if not e:
        return None
    duration_s = int(e.get('duration') or 0)
    thumbs = e.get('thumbnails') or []
    thumb = ''
    if thumbs:
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


def search_source(query, source_prefix, n=6):
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
            return results
    except Exception as ex:
        print(f'[search_source] {source_prefix} error: {ex}')
        return []


def resolve_audio_url(page_url):
    """
    Extrae la URL de audio real usando yt-dlp.
    Devuelve (audio_url, http_headers_dict) o (None, None).
    """
    try:
        with yt_dlp.YoutubeDL(YDL_COMMON) as ydl:
            info    = ydl.extract_info(page_url, download=False)
            formats = info.get('formats') or []

            audio_url, fmt = pick_best_audio(formats)

            # fallback: URL elegida directamente por yt-dlp
            if not audio_url:
                audio_url = info.get('url')

            if not audio_url:
                return None, None

            # Algunos formatos incluyen sus propios headers (p.ej. YouTube firma)
            extra_headers = {}
            if fmt:
                extra_headers = fmt.get('http_headers') or {}

            return audio_url, extra_headers
    except Exception as e:
        print(f'[resolve_audio_url] error: {e}')
        return None, None


def proxy_audio(audio_url, extra_headers=None, filename='audio.mp3', range_header=None):
    """
    Hace streaming del audio proxeado hacia el cliente.
    Soporta Range requests para que expo-audio pueda hacer seek.
    """
    is_yt = 'googlevideo' in audio_url or 'youtube' in audio_url

    headers = {
        'User-Agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/124.0.0.0 Safari/537.36'
        ),
        'Referer': 'https://www.youtube.com/' if is_yt else 'https://soundcloud.com',
        'Origin':  'https://www.youtube.com/' if is_yt else 'https://soundcloud.com',
    }
    if extra_headers:
        headers.update(extra_headers)
    if range_header:
        headers['Range'] = range_header

    r = req.get(audio_url, headers=headers, stream=True, timeout=60)

    if r.status_code not in (200, 206):
        return jsonify({'error': f'Upstream {r.status_code}'}), 502

    def generate():
        for chunk in r.iter_content(chunk_size=32768):
            if chunk:
                yield chunk

    resp_headers = {
        'Content-Type':        'audio/mpeg',
        'Accept-Ranges':       'bytes',
        'Cache-Control':       'no-cache',
        'Content-Disposition': f'inline; filename="{filename}"',
    }
    # Propagar Content-Length y Content-Range si el upstream los manda
    for h in ('Content-Length', 'Content-Range'):
        if h in r.headers:
            resp_headers[h] = r.headers[h]

    return Response(
        generate(),
        status=r.status_code,
        headers=resp_headers,
    )


# ── Rutas ──────────────────────────────────────────────────────────────────

@app.route('/search')
def search():
    query = request.args.get('q', '').strip()
    if not query:
        return jsonify({'error': 'No query'}), 400

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        fut_yt = executor.submit(search_source, query, 'ytsearch', 6)
        fut_sc = executor.submit(search_source, query, 'scsearch', 6)
        yt_results = fut_yt.result()
        sc_results = fut_sc.result()

    merged = []
    for yt, sc in zip(yt_results, sc_results):
        merged.append(yt)
        merged.append(sc)
    merged += yt_results[len(sc_results):]
    merged += sc_results[len(yt_results):]

    return jsonify(merged)


@app.route('/stream')
def stream():
    """
    ANTES: devolvía la URL cruda de YouTube (fallaba por bloqueos de IP).
    AHORA: proxea el audio directamente desde el servidor, evitando bloqueos.
    Soporta Range requests para seek/scrubbing en el cliente.
    """
    url = request.args.get('url', '').strip()
    if not url:
        return jsonify({'error': 'No URL'}), 400

    audio_url, extra_headers = resolve_audio_url(url)
    if not audio_url:
        return jsonify({'error': 'No se pudo resolver la URL de audio'}), 500

    range_header = request.headers.get('Range')
    safe_title   = 'audio'

    return proxy_audio(audio_url, extra_headers, filename=safe_title + '.mp3', range_header=range_header)


@app.route('/download')
def download():
    url   = request.args.get('url', '').strip()
    title = request.args.get('title', 'song').strip()
    if not url:
        return jsonify({'error': 'No URL'}), 400

    safe_title = "".join(c for c in title if c.isalnum() or c in (' ', '-', '_')).strip() or 'song'

    audio_url, extra_headers = resolve_audio_url(url)
    if not audio_url:
        return jsonify({'error': 'No audio URL'}), 500

    return proxy_audio(audio_url, extra_headers, filename=safe_title + '.mp3')


@app.route('/ping')
def ping():
    return jsonify({'status': 'ok'})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)