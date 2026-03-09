from flask import Flask, jsonify, request, Response
from flask_cors import CORS
import yt_dlp
import os
import requests as req
import concurrent.futures

app = Flask(__name__)
CORS(app)

FAKE_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/124.0.0.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

# ══════════════════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════════════════

def fmt_dur(seconds):
    s = int(seconds or 0)
    return f"{s // 60}:{str(s % 60).zfill(2)}"


def entry_to_song(e, source):
    if not e:
        return None
    thumbs     = e.get('thumbnails') or []
    candidates = [t for t in thumbs if t.get('url')]
    thumb = ''
    if candidates:
        thumb = (candidates[-2] if len(candidates) >= 2 else candidates[-1]).get('url', '')
    if not thumb:
        thumb = e.get('thumbnail', '')
    return {
        'id':       f"{source}_{e.get('id', '')}",
        'url':      e.get('webpage_url') or e.get('url', ''),
        'title':    e.get('title', 'Sin título'),
        'artist':   e.get('uploader') or e.get('channel') or 'Desconocido',
        'duration': fmt_dur(int(e.get('duration') or 0)),
        'thumb':    thumb,
        'source':   source,
    }


def search_source(query, prefix, n=6):
    opts = {
        'quiet':        True,
        'no_warnings':  True,
        'extract_flat': True,
        'playlist_items': f'1-{n}',
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f'{prefix}{n}:{query}', download=False)
            return [s for e in (info.get('entries') or [])
                    for s in [entry_to_song(e, prefix.replace('search', ''))] if s]
    except Exception as ex:
        print(f'[search] {prefix} error: {ex}')
        return []


def resolve_stream_url(page_url):
    """Obtiene la URL directa de audio de YT o SC."""
    opts = {
        'quiet':       True,
        'no_warnings': True,
        'format':      'bestaudio[protocol^=http]/bestaudio',
        'http_headers': FAKE_HEADERS,
    }
    if 'soundcloud.com' in page_url:
        opts['extractor_args'] = {'soundcloud': {'client_id': ['6QNse33jZWUMFNeFn5QzGfBErFktk7Sa']}}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(page_url, download=False)
            # Intentar formatos primero
            formats = info.get('formats') or []
            for f in sorted(formats, key=lambda x: x.get('abr') or 0, reverse=True):
                proto = f.get('protocol', '')
                if proto in ('https', 'http') and f.get('url'):
                    return f['url'], f.get('http_headers') or {}
            # Fallback a url directa
            if info.get('url'):
                return info['url'], {}
    except Exception as e:
        print(f'[resolve] error: {e}')
    return None, {}

# ══════════════════════════════════════════════════════════════════════════
#  RUTAS
# ══════════════════════════════════════════════════════════════════════════

@app.route('/ping')
def ping():
    return jsonify({'status': 'ok'})


@app.route('/search')
def search():
    query = request.args.get('q', '').strip()
    if not query:
        return jsonify({'error': 'No query'}), 400

    print(f'[search] query: {query}')

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        sc_fut = ex.submit(search_source, query, 'scsearch', 6)
        yt_fut = ex.submit(search_source, query, 'ytsearch', 6)
        sc_r   = sc_fut.result()
        yt_r   = yt_fut.result()

    # SC primero, luego YT intercalado
    merged = []
    for sc, yt in zip(sc_r, yt_r):
        merged += [sc, yt]
    merged += sc_r[len(yt_r):]
    merged += yt_r[len(sc_r):]

    print(f'[search] resultados SC={len(sc_r)} YT={len(yt_r)}')
    return jsonify(merged)


@app.route('/stream')
def stream():
    """
    Recibe ?url=<page_url>  (la webpage_url de YT o SC)
    Resuelve la URL de audio y la proxea al cliente.
    """
    page_url = request.args.get('url', '').strip()
    if not page_url:
        return jsonify({'error': 'Falta url'}), 400

    print(f'[stream] resolviendo: {page_url}')
    audio_url, extra_headers = resolve_stream_url(page_url)

    if not audio_url:
        return jsonify({'error': 'No se pudo resolver el audio'}), 500

    print(f'[stream] URL resuelta OK')

    # Construir headers para el request upstream
    headers = dict(FAKE_HEADERS)
    is_yt = 'youtube' in page_url or 'youtu.be' in page_url
    headers['Referer'] = 'https://www.youtube.com/' if is_yt else 'https://soundcloud.com/'
    headers['Origin']  = headers['Referer']
    if extra_headers:
        headers.update({k: v for k, v in extra_headers.items() if k not in ('User-Agent',)})

    range_hdr = request.headers.get('Range')
    if range_hdr:
        headers['Range'] = range_hdr

    try:
        r = req.get(audio_url, headers=headers, stream=True, timeout=60)
        if r.status_code not in (200, 206):
            print(f'[stream] upstream error: {r.status_code}')
            return jsonify({'error': f'Upstream {r.status_code}'}), 502

        def generate():
            for chunk in r.iter_content(chunk_size=32768):
                if chunk:
                    yield chunk

        out_headers = {
            'Content-Type':  r.headers.get('Content-Type', 'audio/mpeg'),
            'Accept-Ranges': 'bytes',
            'Cache-Control': 'no-cache',
        }
        for h in ('Content-Length', 'Content-Range'):
            if h in r.headers:
                out_headers[h] = r.headers[h]

        return Response(generate(), status=r.status_code, headers=out_headers)

    except Exception as e:
        print(f'[stream] proxy error: {e}')
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)

## original de yoputube