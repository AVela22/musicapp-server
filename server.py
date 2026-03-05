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


def make_ydl_opts(is_youtube=False):
    opts = {
        'quiet':        False,      # mostrar errores en logs de Render
        'no_warnings':  False,
        'extract_flat': False,
        'socket_timeout': 30,
        'http_headers': {
            'User-Agent': (
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/124.0.0.0 Safari/537.36'
            ),
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept': '*/*',
        },
    }
    if is_youtube:
        # tv_embedded es el cliente con menos restricciones en IPs de datacenter
        opts['format'] = 'bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio'
        opts['extractor_args'] = {
            'youtube': {
                'player_client': ['tv_embedded', 'web'],
                'player_skip':   ['webpage', 'configs'],
            }
        }
    else:
        opts['format'] = 'bestaudio[protocol^=http]/bestaudio'
    return opts


def get_audio_url_youtube(url):
    """
    Intenta extraer la URL de audio de YouTube probando múltiples clientes
    en orden de menor a mayor restricción en datacenters.
    """
    clients = [
        ['tv_embedded'],
        ['web_embedded'],
        ['mweb'],
        ['web'],
        ['android'],
    ]
    for client_list in clients:
        try:
            opts = {
                'quiet': False,
                'no_warnings': False,
                'extract_flat': False,
                'socket_timeout': 25,
                'format': 'bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio',
                'http_headers': {
                    'User-Agent': (
                        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                        'AppleWebKit/537.36 (KHTML, like Gecko) '
                        'Chrome/124.0.0.0 Safari/537.36'
                    ),
                },
                'extractor_args': {
                    'youtube': {
                        'player_client': client_list,
                        'player_skip':   ['webpage', 'configs'],
                    }
                },
            }
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
                audio_url = info.get('url')
                if not audio_url:
                    fmts = info.get('formats') or []
                    for f in reversed(fmts):
                        if f.get('url') and f.get('acodec') not in (None, 'none'):
                            audio_url = f['url']
                            break
                if audio_url:
                    print(f'[yt] client={client_list}  ext={info.get("ext")}  got URL ok')
                    return audio_url, info
        except Exception as ex:
            print(f'[yt] client={client_list} failed: {ex}')
            continue
    return None, None


@app.route('/stream')
def stream():
    url = request.args.get('url', '').strip()
    if not url:
        return jsonify({'error': 'No URL'}), 400

    is_youtube = 'youtube.com' in url or 'youtu.be' in url

    try:
        if is_youtube:
            audio_url, info = get_audio_url_youtube(url)
            if not audio_url:
                return jsonify({'error': 'YouTube blocked all clients on this server IP. Try a SoundCloud result.'}), 500
        else:
            with yt_dlp.YoutubeDL(make_ydl_opts(False)) as ydl:
                info     = ydl.extract_info(url, download=False)
                formats  = info.get('formats') or []
                audio_url = pick_best_audio(formats) or info.get('url')
                if not audio_url and formats:
                    audio_url = formats[-1].get('url')

        if not audio_url:
            return jsonify({'error': 'No stream URL found'}), 404

        print(f'[stream] ok  is_yt={is_youtube}  url[:80]={audio_url[:80]}')

        return jsonify({
            'url':      audio_url,
            'title':    (info or {}).get('title', ''),
            'duration': (info or {}).get('duration', 0),
            'format':   (info or {}).get('ext', ''),
        })
    except Exception as e:
        print(f'[stream] error: {e}')
        return jsonify({'error': str(e)}), 500


@app.route('/proxy')
def proxy():
    """
    Hace streaming del audio directamente desde el servidor.
    Útil para YouTube: la URL resuelta es válida desde la IP del servidor,
    así el cliente solo consume bytes del proxy y nunca toca YouTube directo.
    Soporta Range requests para que expo-audio pueda hacer seek.
    """
    url = request.args.get('url', '').strip()
    if not url:
        return jsonify({'error': 'No URL'}), 400

    is_youtube = 'youtube.com' in url or 'youtu.be' in url

    try:
        if is_youtube:
            audio_url, info = get_audio_url_youtube(url)
            if not audio_url:
                return jsonify({'error': 'No YouTube URL'}), 500
        else:
            with yt_dlp.YoutubeDL(make_ydl_opts(False)) as ydl:
                info = ydl.extract_info(url, download=False)
                fmts = info.get('formats') or []
                audio_url = pick_best_audio(fmts) or info.get('url')

        if not audio_url:
            return jsonify({'error': 'No audio URL'}), 404

        # Reenviar Range header si el cliente lo manda (necesario para seek)
        upstream_headers = {
            'User-Agent': (
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/124.0.0.0 Safari/537.36'
            ),
            'Referer': 'https://www.youtube.com/' if is_youtube else 'https://soundcloud.com',
        }
        if 'Range' in request.headers:
            upstream_headers['Range'] = request.headers['Range']

        r = req.get(audio_url, headers=upstream_headers, stream=True, timeout=60)

        def generate():
            for chunk in r.iter_content(chunk_size=32768):
                if chunk:
                    yield chunk

        resp_headers = {
            'Content-Type':  r.headers.get('Content-Type', 'audio/mp4'),
            'Accept-Ranges': 'bytes',
        }
        if 'Content-Length' in r.headers:
            resp_headers['Content-Length'] = r.headers['Content-Length']
        if 'Content-Range' in r.headers:
            resp_headers['Content-Range'] = r.headers['Content-Range']

        status_code = r.status_code if r.status_code in (200, 206) else 200
        return Response(generate(), status=status_code, headers=resp_headers)

    except Exception as e:
        print(f'[proxy] error: {e}')
        return jsonify({'error': str(e)}), 500


@app.route('/download')
def download():
    url   = request.args.get('url', '').strip()
    title = request.args.get('title', 'song').strip()
    if not url:
        return jsonify({'error': 'No URL'}), 400

    safe_title = "".join(c for c in title if c.isalnum() or c in (' ', '-', '_')).strip() or 'song'
    is_youtube = 'youtube.com' in url or 'youtu.be' in url

    try:
        if is_youtube:
            audio_url, _ = get_audio_url_youtube(url)
        else:
            ydl_opts = {
                'quiet': True, 'no_warnings': True,
                'extract_flat': False,
                'format': 'bestaudio[protocol^=http]/bestaudio',
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info      = ydl.extract_info(url, download=False)
                audio_url = pick_best_audio(info.get('formats') or []) or info.get('url')

        if not audio_url:
            return jsonify({'error': 'No audio URL'}), 404

        referer = 'https://www.youtube.com/' if is_youtube else 'https://soundcloud.com'
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Referer':    referer,
        }
        r = req.get(audio_url, headers=headers, stream=True, timeout=60)
        if r.status_code != 200:
            return jsonify({'error': f'Upstream {r.status_code}'}), 502

        def generate():
            for chunk in r.iter_content(chunk_size=16384):
                if chunk:
                    yield chunk

        return Response(
            generate(), status=200, mimetype='audio/mpeg',
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