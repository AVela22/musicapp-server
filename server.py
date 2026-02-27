from flask import Flask, jsonify, request
import yt_dlp
import re

app = Flask(__name__)

def is_valid_video_id(video_id):
    return bool(re.match(r'^[a-zA-Z0-9_-]{11}$', video_id))

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
    }

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

@app.route('/ping')
def ping():
    return jsonify({'status': 'ok'})

if __name__ == '__main__':
    import os
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)