from flask import Flask, jsonify, request, Response
from flask_cors import CORS
import yt_dlp
import os
import requests as req
import concurrent.futures

app = Flask(__name__)
CORS(app)

# ── Helpers ────────────────────────────────────────────────────────────────

FAKE_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/124.0.0.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

YDL_OPTS = {
    'quiet': True,
    'no_warnings': True,
    'extract_flat': False,
    'format': 'bestaudio[protocol^=http]/bestaudio',
    'http_headers': FAKE_HEADERS,
}


def pick_best_audio(formats):
    """Elige la mejor URL de audio progresivo (http/https). Evita HLS/DASH."""
    if not formats:
        return None, None

    def score(f):
        proto  = f.get('protocol', '')
        ext    = f.get('ext', '')
        acodec = f.get('acodec', '')
        if proto not in ('https', 'http'):      return -1
        if ext == 'mp3' or 'mp3' in acodec:    return 100
        if ext == 'm4a' or 'aac' in acodec:    return 80
        if ext in ('ogg','opus') or 'opus' in acodec: return 60
        if f.get('url'):                        return 10
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
    thumbs = e.get('thumbnails') or []
    thumb  = ''
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
        'duration': format_duration(int(e.get('duration') or 0)),
        'thumb':    thumb,
        'source':   source,
    }


def search_source(query, source_prefix, n=6):
    opts = {'quiet': True, 'no_warnings': True, 'extract_flat': True, 'playlist_items': f'1-{n}'}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info    = ydl.extract_info(f'{source_prefix}{n}:{query}', download=False)
            entries = info.get('entries') or []
            src     = source_prefix.replace('search', '').strip(':') or source_prefix
            return [r for e in entries for r in [entry_to_result(e, src)] if r]
    except Exception as ex:
        print(f'[search_source] {source_prefix} error: {ex}')
        return []


def resolve_audio(page_url):
    """Resuelve la URL de audio real con yt-dlp. Devuelve (url, fmt_headers)."""
    try:
        with yt_dlp.YoutubeDL(YDL_OPTS) as ydl:
            info    = ydl.extract_info(page_url, download=False)
            formats = info.get('formats') or []
            audio_url, fmt = pick_best_audio(formats)
            if not audio_url:
                audio_url = info.get('url')
            extra = (fmt or {}).get('http_headers') or {}
            return audio_url, extra
    except Exception as e:
        print(f'[resolve_audio] error: {e}')
        return None, {}


def make_proxy_response(audio_url, extra_headers, filename, range_header=None):
    """
    Proxea el audio hacia el cliente.
    Soporta Range para que expo-av pueda hacer seek y detectar duración.
    """
    is_yt = 'googlevideo' in audio_url or 'youtube' in audio_url

    headers = dict(FAKE_HEADERS)
    headers['Referer'] = 'https://www.youtube.com/' if is_yt else 'https://soundcloud.com'
    headers['Origin']  = headers['Referer']
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
    for h in ('Content-Length', 'Content-Range'):
        if h in r.headers:
            resp_headers[h] = r.headers[h]

    return Response(generate(), status=r.status_code, headers=resp_headers)


# ── Rutas ──────────────────────────────────────────────────────────────────

@app.route('/search')
def search():
    query = request.args.get('q', '').strip()
    if not query:
        return jsonify({'error': 'No query'}), 400

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        fut_yt = ex.submit(search_source, query, 'ytsearch', 6)
        fut_sc = ex.submit(search_source, query, 'scsearch', 6)
        yt_r   = fut_yt.result()
        sc_r   = fut_sc.result()

    merged = []
    for yt, sc in zip(yt_r, sc_r):
        merged += [yt, sc]
    merged += yt_r[len(sc_r):]
    merged += sc_r[len(yt_r):]
    return jsonify(merged)


@app.route('/stream')
def stream():
    """
    Proxea el audio del servidor al cliente.
    El cliente NO recibe URLs crudas de YouTube (que fallan por bloqueos de IP).
    Soporta Range requests para seek / detección de duración.
    """
    url = request.args.get('url', '').strip()
    if not url:
        return jsonify({'error': 'No URL'}), 400

    audio_url, extra = resolve_audio(url)
    if not audio_url:
        return jsonify({'error': 'No se pudo resolver el audio'}), 500

    return make_proxy_response(
        audio_url, extra,
        filename='audio.mp3',
        range_header=request.headers.get('Range'),
    )


@app.route('/download')
def download():
    url   = request.args.get('url', '').strip()
    title = request.args.get('title', 'song').strip()
    if not url:
        return jsonify({'error': 'No URL'}), 400

    safe = "".join(c for c in title if c.isalnum() or c in (' ','-','_')).strip() or 'song'
    audio_url, extra = resolve_audio(url)
    if not audio_url:
        return jsonify({'error': 'No audio URL'}), 500

    return make_proxy_response(audio_url, extra, filename=safe + '.mp3')


@app.route('/ping')
def ping():
    return jsonify({'status': 'ok'})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)