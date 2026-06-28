"""
AnimeHeaven Microservice — Flask API
Wraps the AnimeHeaven scraper and exposes endpoints for the Zentrix backend.
"""

import os
import re
import requests
from urllib.parse import urljoin
from flask import Flask, jsonify, request, Response, stream_with_context
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

BASE_URL     = "https://animeheaven.me"
SEARCH_URL   = f"{BASE_URL}/search.php"
PORT         = int(os.environ.get("PORT", 5000))

VIDEO_SUBDOMAINS = ["rk", "fi", "cc", "la", "ny", "py", "ct", "ck", "cw"]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": f"{BASE_URL}/",
    "Origin": BASE_URL,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

session = requests.Session()
session.headers.update(HEADERS)

# ─── Helpers ──────────────────────────────────────────────────────────────────

def clean_title(title):
    return re.sub(r'\s+', ' ', re.sub(r'[^a-z0-9\s]', ' ', title.lower())).strip()

def title_score(query, result):
    q = clean_title(query)
    r = clean_title(result)
    if r == q:         return 100
    if r.startswith(q): return 90
    if q in r:         return 70
    q_words = set(q.split())
    r_words = set(r.split())
    overlap = len(q_words & r_words)
    return int((overlap / max(len(q_words), 1)) * 50)

def extract_urls_from_gate_html(html):
    stream_url   = None
    download_url = None

    source_patterns = [
        r"<source\s+src='(https?://[^'\"]+\.animeheaven\.me/video\.mp4\?[^'\"&]+&[^'\"]+)'\s+type='video/mp4'\s+onerror=\"xhr\(\)\">",
        r"<source\s+src='(https?://[^'\"]+\.animeheaven\.me/video\.mp4\?[^'\"]+)'\s+type='video/mp4'",
        r"src=[\"']?(https?://[^'\">\s]+\.animeheaven\.me/video\.mp4\?[^'\">\s]+)[\"']?",
    ]
    for pat in source_patterns:
        m = re.search(pat, html)
        if m:
            stream_url = m.group(1)
            break

    download_patterns = [
        r"href='(https?://[^'\"]+\.animeheaven\.me/video\.mp4\?[^'\"]+&d)'",
        r'href="(https?://[^"\']+\.animeheaven\.me/video\.mp4\?[^"\']+&d)"',
    ]
    for pat in download_patterns:
        m = re.search(pat, html)
        if m:
            download_url = m.group(1)
            break

    if stream_url and not download_url:
        base = stream_url.split('&')[0]
        download_url = base + '&d'

    return stream_url, download_url

def verify_video_url(url):
    try:
        resp = session.head(url, timeout=10, allow_redirects=True)
        if resp.status_code == 200:
            ct = resp.headers.get('Content-Type', '')
            cl = int(resp.headers.get('Content-Length', '0'))
            return 'video' in ct or cl > 100000
    except Exception:
        pass
    return False

# ─── Routes ───────────────────────────────────────────────────────────────────

@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'service': 'AnimeHeaven API'})

@app.route('/search')
def search():
    q = request.args.get('q', '')
    if not q:
        return jsonify({'error': 'q required'}), 400

    try:
        resp = session.get(SEARCH_URL, params={'s': q}, timeout=15)
        html = resp.text

        results = []
        seen = set()

        # Parse search results
        link_re = re.compile(r'href=["\']([^"\']*anime\.php\?[^"\']+)["\'][^>]*>\s*([^<]+)\s*<')
        for m in link_re.finditer(html):
            href  = m.group(1).strip()
            title = m.group(2).strip()
            if not title or not href:
                continue
            anime_id = href.split('?')[-1] if '?' in href else ''
            if not anime_id or anime_id in seen:
                continue
            seen.add(anime_id)
            results.append({
                'title': title,
                'url':   urljoin(BASE_URL, href),
                'id':    anime_id,
                'score': title_score(q, title),
            })

        results.sort(key=lambda x: x['score'], reverse=True)
        return jsonify({'success': True, 'results': results, 'count': len(results)})

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/episodes')
def episodes():
    anime_id = request.args.get('id', '')
    if not anime_id:
        return jsonify({'error': 'id required'}), 400

    try:
        url  = f"{BASE_URL}/anime.php?{anime_id}"
        resp = session.get(url, timeout=15)
        html = resp.text

        episodes = []
        seen = set()

        # Method 1: gate.php links
        gate_re = re.compile(
            r'<a\s+href=["\']gate\.php["\'][^>]*id=["\']([^"\']+)["\'][^>]*>[\s\S]*?(\d+(?:\.\d+)?)',
            re.IGNORECASE
        )
        for m in gate_re.finditer(html):
            ep_id  = m.group(1)
            ep_num = m.group(2)
            if ep_num not in seen:
                seen.add(ep_num)
                episodes.append({
                    'number':    ep_num,
                    'title':     f'Episode {ep_num}',
                    'ep_id':     ep_id,
                    'watch_url': f'{BASE_URL}/watch.php?{anime_id}&e={ep_num}',
                })

        # Method 2: maxep fallback
        if not episodes:
            maxep_m = re.search(r'var\s+maxep\s*=\s*(\d+)', html)
            if maxep_m:
                max_ep = int(maxep_m.group(1))
                for i in range(1, max_ep + 1):
                    episodes.append({
                        'number':    str(i),
                        'title':     f'Episode {i}',
                        'ep_id':     '',
                        'watch_url': f'{BASE_URL}/watch.php?{anime_id}&e={i}',
                    })

        episodes.sort(key=lambda x: float(x['number']))
        return jsonify({'success': True, 'episodes': episodes, 'count': len(episodes)})

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/source')
def source():
    anime_id  = request.args.get('anime_id', '')
    ep_number = request.args.get('episode', '')
    ep_id     = request.args.get('ep_id', '')

    if not anime_id or not ep_number:
        return jsonify({'error': 'anime_id and episode required'}), 400

    host = request.host_url.rstrip('/')
    stream_url   = None
    download_url = None

    # Step 1: gate.php with cookie (most reliable)
    if ep_id:
        try:
            gate_session = requests.Session()
            gate_session.headers.update(HEADERS)
            gate_session.cookies.set('key', ep_id, domain='animeheaven.me')
            resp = gate_session.get(f'{BASE_URL}/gate.php', timeout=20, allow_redirects=True)
            if resp.status_code == 200:
                stream_url, download_url = extract_urls_from_gate_html(resp.text)
        except Exception:
            pass

    # Step 2: watch page fallback
    if not stream_url:
        try:
            watch_url = f'{BASE_URL}/watch.php?{anime_id}&e={ep_number}'
            resp = session.get(watch_url, timeout=15)
            stream_url, download_url = extract_urls_from_gate_html(resp.text)
        except Exception:
            pass

    # Step 3: Direct CDN construction
    if not stream_url and ep_id:
        for sub in VIDEO_SUBDOMAINS:
            url = f'https://{sub}.animeheaven.me/video.mp4?{ep_id}'
            if verify_video_url(url):
                stream_url   = url
                download_url = url + '&d'
                break

    if not stream_url:
        return jsonify({'success': False, 'error': 'No video source found'}), 404

    return jsonify({
        'success':      True,
        'streamUrl':    f'{host}/stream?url={requests.utils.quote(stream_url)}',
        'downloadUrl':  f'{host}/download?url={requests.utils.quote(download_url or "")}',
        'rawStream':    stream_url,
        'rawDownload':  download_url,
    })


@app.route('/stream')
def stream():
    url = request.args.get('url', '')
    if not url:
        return jsonify({'error': 'url required'}), 400

    range_header = request.headers.get('Range')
    fetch_headers = dict(HEADERS)
    if range_header:
        fetch_headers['Range'] = range_header

    try:
        upstream = session.get(url, headers=fetch_headers, stream=True, timeout=30)
        status   = upstream.status_code

        resp_headers = {
            'Content-Type':              upstream.headers.get('Content-Type', 'video/mp4'),
            'Accept-Ranges':             'bytes',
            'Access-Control-Allow-Origin': '*',
            'Cache-Control':             'no-cache',
        }
        if 'Content-Length' in upstream.headers:
            resp_headers['Content-Length'] = upstream.headers['Content-Length']
        if 'Content-Range' in upstream.headers:
            resp_headers['Content-Range'] = upstream.headers['Content-Range']

        return Response(
            stream_with_context(upstream.iter_content(chunk_size=8192)),
            status=status,
            headers=resp_headers,
        )
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/download')
def download():
    url     = request.args.get('url', '')
    title   = request.args.get('title', 'anime')
    episode = request.args.get('episode', '1')
    if not url:
        return jsonify({'error': 'url required'}), 400

    try:
        upstream = session.get(url, stream=True, timeout=30)
        ct  = upstream.headers.get('Content-Type', 'video/mp4')
        ext = 'mp4' if 'mp4' in ct else 'mkv'
        safe_title = re.sub(r'[^a-zA-Z0-9\s\-_]', '', title).strip().replace(' ', '_')
        safe_ep    = str(episode).zfill(2)
        filename   = f'{safe_title}_EP{safe_ep}.{ext}'

        resp_headers = {
            'Content-Type':               ct,
            'Content-Disposition':        f'attachment; filename="{filename}"',
            'Access-Control-Allow-Origin': '*',
        }
        if 'Content-Length' in upstream.headers:
            resp_headers['Content-Length'] = upstream.headers['Content-Length']

        return Response(
            stream_with_context(upstream.iter_content(chunk_size=8192)),
            status=200,
            headers=resp_headers,
        )
    except Exception as e:
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=PORT, debug=False)
