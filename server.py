from flask import Flask, jsonify, request, Response, redirect
from flask_cors import CORS
import yt_dlp
import os
import requests as req
import concurrent.futures
import threading
import tempfile
import ftplib
import ssl

app = Flask(__name__)
CORS(app)

# ══════════════════════════════════════════════════════════════════════════
#  CONFIG — pon estas variables en Render > Environment
# ══════════════════════════════════════════════════════════════════════════
FTP_HOST       = os.environ.get('FTP_HOST',       'ftp.centerdatatech.com')
FTP_PORT       = int(os.environ.get('FTP_PORT',   21))
FTP_USER       = os.environ.get('FTP_USER',       'appmus1c@centerdatatech.com')
FTP_PASS       = os.environ.get('FTP_PASS',       '')   # solo en Render env vars
FTP_REMOTE_DIR = os.environ.get('FTP_REMOTE_DIR', '/home2/centerd1/public_html/audiocache')
CACHE_BASE_URL = os.environ.get('CACHE_BASE_URL', 'https://centerdatatech.com/audiocache')

# ── Fake headers para YT/SC ───────────────────────────────────────────────
FAKE_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/124.0.0.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

YDL_OPTS = {
    'quiet':        True,
    'no_warnings':  True,
    'extract_flat': False,
    'format':       'bestaudio[protocol^=http]/bestaudio',
    'http_headers': FAKE_HEADERS,
}

# ── FTP helpers ───────────────────────────────────────────────────────────

def ftp_connect():
    """Abre conexión FTPS explícita (TLS puerto 21)."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode    = ssl.CERT_NONE   # cert wildcard de HostGator
    ftp = ftplib.FTP_TLS(context=ctx)
    ftp.connect(FTP_HOST, FTP_PORT, timeout=30)
    ftp.login(FTP_USER, FTP_PASS)
    ftp.prot_p()       # canal de datos cifrado
    ftp.set_pasv(True)
    return ftp


def cache_exists(song_id: str) -> bool:
    """HEAD rápido — true si el MP3 ya está en el hosting."""
    try:
        r = req.head(f'{CACHE_BASE_URL}/{song_id}.mp3', timeout=5)
        return r.status_code == 200
    except Exception:
        return False


def upload_to_ftp(song_id: str, local_path: str):
    """Sube el MP3 al hosting. Crea la carpeta si no existe."""
    try:
        ftp = ftp_connect()
        try:
            ftp.mkd(FTP_REMOTE_DIR)
        except ftplib.error_perm:
            pass  # ya existe
        ftp.cwd(FTP_REMOTE_DIR)
        with open(local_path, 'rb') as f:
            ftp.storbinary(f'STOR {song_id}.mp3', f, blocksize=65536)
        ftp.quit()
        print(f'[cache] ✓ subido {song_id}.mp3')
    except Exception as e:
        print(f'[cache] ✗ error subiendo {song_id}: {e}')


def cache_in_background(song_id: str, audio_url: str, extra_headers: dict):
    """Descarga el audio y lo sube al hosting en un hilo daemon."""
    def _run():
        tmp_path = None
        try:
            headers = dict(FAKE_HEADERS)
            is_yt   = 'googlevideo' in audio_url or 'youtube' in audio_url
            headers['Referer'] = 'https://www.youtube.com/' if is_yt else 'https://soundcloud.com'
            headers['Origin']  = headers['Referer']
            if extra_headers:
                headers.update(extra_headers)

            r = req.get(audio_url, headers=headers, stream=True, timeout=180)
            if r.status_code != 200:
                print(f'[cache] descarga falló con status {r.status_code}')
                return

            tmp = tempfile.NamedTemporaryFile(suffix='.mp3', delete=False)
            tmp_path = tmp.name
            tmp.close()
            with open(tmp_path, 'wb') as f:
                for chunk in r.iter_content(chunk_size=65536):
                    if chunk:
                        f.write(chunk)

            upload_to_ftp(song_id, tmp_path)
        except Exception as e:
            print(f'[cache] background error: {e}')
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)

    threading.Thread(target=_run, daemon=True).start()

# ── yt-dlp helpers ────────────────────────────────────────────────────────

def pick_best_audio(formats):
    if not formats:
        return None, None

    def score(f):
        proto  = f.get('protocol', '')
        ext    = f.get('ext',      '')
        acodec = f.get('acodec',   '')
        if proto not in ('https', 'http'):         return -1
        if ext == 'mp3'  or 'mp3'  in acodec:     return 100
        if ext == 'm4a'  or 'aac'  in acodec:     return 80
        if ext in ('ogg','opus') or 'opus' in acodec: return 60
        if f.get('url'):                           return 10
        return -1

    for f in sorted(formats, key=score, reverse=True):
        if score(f) >= 0 and f.get('url'):
            return f['url'], f
    return None, None


def fmt_dur(seconds):
    s = int(seconds or 0)
    return f"{s // 60}:{str(s % 60).zfill(2)}"


def entry_to_song(e, source):
    if not e:
        return None
    thumbs     = e.get('thumbnails') or []
    candidates = [t for t in thumbs if t.get('url')]
    thumb = (candidates[-2] if len(candidates) >= 2 else candidates[-1]).get('url', '') if candidates else e.get('thumbnail', '')
    src   = source.replace('search', '').strip(':') or source
    return {
        'id':       f"{src}_{e.get('id','')}",
        'url':      e.get('webpage_url') or e.get('url', ''),
        'title':    e.get('title', 'Sin título'),
        'artist':   e.get('uploader') or e.get('channel') or 'Desconocido',
        'duration': fmt_dur(int(e.get('duration') or 0)),
        'thumb':    thumb,
        'source':   src,
    }


def search_source(query, prefix, n=6):
    opts = {'quiet': True, 'no_warnings': True, 'extract_flat': True, 'playlist_items': f'1-{n}'}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f'{prefix}{n}:{query}', download=False)
            return [s for e in (info.get('entries') or []) for s in [entry_to_song(e, prefix)] if s]
    except Exception as ex:
        print(f'[search] {prefix} error: {ex}')
        return []


def resolve_audio(page_url):
    try:
        with yt_dlp.YoutubeDL(YDL_OPTS) as ydl:
            info      = ydl.extract_info(page_url, download=False)
            audio_url, fmt = pick_best_audio(info.get('formats') or [])
            if not audio_url:
                audio_url = info.get('url')
            return audio_url, (fmt or {}).get('http_headers') or {}
    except Exception as e:
        print(f'[resolve] error: {e}')
        return None, {}


def proxy_response(audio_url, extra, filename, range_hdr=None):
    is_yt   = 'googlevideo' in audio_url or 'youtube' in audio_url
    headers = dict(FAKE_HEADERS)
    headers['Referer'] = 'https://www.youtube.com/' if is_yt else 'https://soundcloud.com'
    headers['Origin']  = headers['Referer']
    if extra:
        headers.update(extra)
    if range_hdr:
        headers['Range'] = range_hdr

    r = req.get(audio_url, headers=headers, stream=True, timeout=60)
    if r.status_code not in (200, 206):
        return jsonify({'error': f'Upstream {r.status_code}'}), 502

    def gen():
        for chunk in r.iter_content(chunk_size=32768):
            if chunk:
                yield chunk

    out_headers = {
        'Content-Type':        'audio/mpeg',
        'Accept-Ranges':       'bytes',
        'Cache-Control':       'no-cache',
        'Content-Disposition': f'inline; filename="{filename}"',
    }
    for h in ('Content-Length', 'Content-Range'):
        if h in r.headers:
            out_headers[h] = r.headers[h]

    return Response(gen(), status=r.status_code, headers=out_headers)

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

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        yt_r = ex.submit(search_source, query, 'ytsearch', 6).result()
        sc_r = ex.submit(search_source, query, 'scsearch', 6).result()

    merged = []
    for yt, sc in zip(yt_r, sc_r):
        merged += [yt, sc]
    merged += yt_r[len(sc_r):]
    merged += sc_r[len(yt_r):]
    return jsonify(merged)


@app.route('/stream')
def stream():
    """
    1) Si el MP3 ya está en HostGator  →  redirect 302  (Render no toca el audio)
    2) Si no  →  resuelve de YT/SC, proxea al cliente
                 y dispara un hilo que descarga + sube a HostGator
    """
    page_url = request.args.get('url', '').strip()
    song_id  = request.args.get('id',  '').strip()

    if not page_url:
        return jsonify({'error': 'No URL'}), 400

    # ── 1. Caché hit ──────────────────────────────────────────────────────
    if song_id and FTP_PASS and cache_exists(song_id):
        cached = f'{CACHE_BASE_URL}/{song_id}.mp3'
        print(f'[cache] HIT → {cached}')
        return redirect(cached, code=302)

    # ── 2. Caché miss ─────────────────────────────────────────────────────
    print(f'[cache] MISS → resolviendo {page_url}')
    audio_url, extra = resolve_audio(page_url)
    if not audio_url:
        return jsonify({'error': 'No se pudo resolver el audio'}), 500

    # Cachear en background (solo si tenemos FTP configurado)
    if song_id and FTP_PASS:
        cache_in_background(song_id, audio_url, extra)

    return proxy_response(
        audio_url, extra,
        filename=f'{song_id or "audio"}.mp3',
        range_hdr=request.headers.get('Range'),
    )


@app.route('/download')
def download():
    page_url = request.args.get('url',   '').strip()
    song_id  = request.args.get('id',    '').strip()
    title    = request.args.get('title', 'song').strip()

    if not page_url:
        return jsonify({'error': 'No URL'}), 400

    safe = ''.join(c for c in title if c.isalnum() or c in (' ', '-', '_')).strip() or 'song'

    # Si ya está cacheado, redirigir directo
    if song_id and FTP_PASS and cache_exists(song_id):
        return redirect(f'{CACHE_BASE_URL}/{song_id}.mp3', code=302)

    audio_url, extra = resolve_audio(page_url)
    if not audio_url:
        return jsonify({'error': 'No audio URL'}), 500

    # Cachear en background también al descargar
    if song_id and FTP_PASS:
        cache_in_background(song_id, audio_url, extra)

    return proxy_response(audio_url, extra, filename=safe + '.mp3')


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)