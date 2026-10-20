"""Isolated download worker. Never include credentials or media URLs in logs."""
import json
import os
import re
import subprocess
import sys
import math
from fractions import Fraction
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import yt_dlp
from yt_dlp.extractor.common import InfoExtractor
from yt_dlp.utils import ExtractorError

LIMIT = 48 * 1024 * 1024
HOSTS = {'youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com',
         'youtu.be', 'www.youtu.be', 'instagram.com', 'www.instagram.com',
         'threads.net', 'www.threads.net', 'threads.com', 'www.threads.com'}


def validate_url(value):
    try:
        u = urlsplit(value)
        if (u.scheme not in ('http', 'https') or u.hostname not in HOSTS
                or u.username or u.password or u.port not in (None, 80, 443)):
            raise ValueError()
    except ValueError:
        raise ValueError('Пришли ссылку на YouTube, Instagram или Threads.') from None
    host = u.hostname
    if 'youtube' in host and u.path not in ('/watch',) and not u.path.startswith(('/shorts/', '/live/', '/embed/')):
        raise ValueError('Нужна ссылка на отдельное видео, а не канал или плейлист.')
    if 'instagram' in host and not u.path.startswith(('/p/', '/reel/', '/reels/', '/share/')):
        raise ValueError('Нужна ссылка на публикацию или Reels.')
    if 'threads' in host and not re.match(r'^/(?:@[^/]+/post/|t/|share/)[\w-]+', u.path):
        raise ValueError('Нужна ссылка на публикацию Threads.')
    return urlunsplit(('https', host, u.path, u.query, ''))


def find_post(data, code):
    if isinstance(data, dict):
        if data.get('code') == code and ('video_versions' in data or 'carousel_media' in data):
            return data
        for value in data.values():
            found = find_post(value, code)
            if found:
                return found
    elif isinstance(data, list):
        for value in data:
            found = find_post(value, code)
            if found:
                return found
    return None


class ThreadsIE(InfoExtractor):
    _VALID_URL = r'https?://(?:www\.)?threads\.(?:net|com)/(?:@[^/]+/post|t|share)/(?P<id>[\w-]+)'

    def _real_extract(self, url):
        code = self._match_id(url)
        page = self._download_webpage(url, code, headers={
            'User-Agent': 'Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)'})
        if '/share/' in url:
            canonical = self._og_search_property('url', page, default='')
            match = re.search(r'/post/([\w-]+)', canonical)
            if not match:
                raise ExtractorError('Use a canonical Threads post URL', expected=True)
            code = match[1]
        post = None
        for block in re.findall(r'<script\b[^>]*type="application/json"[^>]*>(.*?)</script>', page, re.S):
            try:
                post = find_post(json.loads(block), code)
            except ValueError:
                continue
            if post:
                break
        if not post:
            raise ExtractorError('Threads post unavailable', expected=True)
        entries = []
        for index, item in enumerate(post.get('carousel_media') or [post]):
            formats = [{'url': v['url'], 'ext': 'mp4', 'width': v.get('width'),
                        'height': v.get('height')} for v in item.get('video_versions', []) if v.get('url')]
            if formats:
                entries.append({'id': f'{code}_{index}', 'title': 'Threads video', 'formats': formats})
        if not entries:
            raise ExtractorError('No video in post', expected=True)
        return self.playlist_result(entries, code)


class QuietLogger:
    def debug(self, msg):
        pass
    info = warning = error = debug


def run(command):
    result = subprocess.run(command, capture_output=True, timeout=240)
    if result.returncode:
        raise RuntimeError('Не удалось обработать видео.')
    return result.stdout


def probe_video(path):
    return json.loads(run(['ffprobe', '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(path)]))


def pixel_ratio(video):
    try:
        ratio = Fraction(video.get('sample_aspect_ratio', '1:1').replace(':', '/'))
        return ratio if ratio > 0 else Fraction(1)
    except (ValueError, ZeroDivisionError):
        return Fraction(1)


def rotation(video):
    return float(next((s['rotation'] for s in video.get('side_data_list', []) if 'rotation' in s),
                      video.get('tags', {}).get('rotate', 0))) % 360


def video_metadata(path, folder, index):
    probe = probe_video(path)
    video = next(s for s in probe['streams'] if s['codec_type'] == 'video')
    thumb = folder / f'thumbnail-{index}.jpg'
    run(['ffmpeg', '-y', '-v', 'error', '-i', str(path), '-map', '0:v:0', '-frames:v', '1',
         '-vf', 'scale=320:320:force_original_aspect_ratio=decrease,setsar=1', '-q:v', '5', str(thumb)])
    return {'width': video['width'], 'height': video['height'],
            'duration': math.ceil(float(probe['format']['duration'])), 'thumbnail': str(thumb)}


def prepare(path, folder, index, height=720):
    probe = probe_video(path)
    duration = float(probe['format'].get('duration') or 0)
    video = next((s for s in probe['streams'] if s['codec_type'] == 'video'), None)
    if not video or duration <= 0:
        raise RuntimeError('В публикации не найдено видео.')
    if duration > int(os.getenv('MAX_DURATION', '1800')):
        raise RuntimeError('Видео слишком длинное. Максимум — 30 минут.')
    audio = next((s for s in probe['streams'] if s['codec_type'] == 'audio'), None)
    if (path.stat().st_size <= LIMIT and path.suffix == '.mp4' and video['codec_name'] == 'h264'
            and (not audio or audio['codec_name'] == 'aac') and pixel_ratio(video) == 1 and rotation(video) == 0):
        return path
    target = folder / f'ready-{index}.mp4'
    bitrate = min(5000 if height == 1080 else 2500, int(LIMIT * 8 / duration / 1000 * .90) - 96)
    if bitrate < 100:
        raise RuntimeError('Видео слишком большое для отправки в Telegram.')
    run(['ffmpeg', '-y', '-v', 'error', '-i', str(path), '-map', '0:v:0', '-map', '0:a:0?',
         '-vf', f"scale=w='max(2,trunc(iw*sar/2)*2)':h=ih,setsar=1,scale=w='min({height * 16 // 9},iw)':h='min({height},ih)':force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1",
         '-c:v', 'libx264', '-preset', 'veryfast', '-b:v', f'{bitrate}k', '-maxrate', f'{bitrate}k',
         '-bufsize', f'{bitrate * 2}k', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '96k',
         '-movflags', '+faststart', str(target)])
    if target.stat().st_size > LIMIT:
        raise RuntimeError('Видео слишком большое даже после сжатия.')
    return target


def format_selector(height):
    if height not in (360, 720, 1080):
        raise ValueError('Неподдерживаемое качество.')
    return (f'bv*[height<={height}][ext=mp4]+ba[ext=m4a]/'
            f'b[height<={height}][ext=mp4]/bv*[height<={height}]+ba/b[height<={height}]')


def download(url, folder, height=720):
    def guard(status):
        if status.get('downloaded_bytes', 0) > 250 * 1024 * 1024:
            raise RuntimeError('Видео слишком большое для обработки.')
    options = {
        'outtmpl': str(folder / '%(id)s.%(ext)s'), 'noplaylist': True, 'playlistend': 5,
        'format': format_selector(height) if 'youtu' in urlsplit(url).hostname else 'bv*[height<=720][ext=mp4]+ba[ext=m4a]/b[ext=mp4]/b',
        'merge_output_format': 'mp4', 'max_filesize': 250 * 1024 * 1024,
        'socket_timeout': 20, 'retries': 2, 'fragment_retries': 2,
        'quiet': True, 'logger': QuietLogger(), 'progress_hooks': [guard],
        'match_filter': lambda info, **kw: 'Видео длиннее 30 минут' if info.get('duration', 0) > int(os.getenv('MAX_DURATION', '1800')) else None,
        'extractor_args': {'youtube': {'player_client': ['default']}},
    }
    if os.getenv('COOKIES_FILE'):
        options['cookiefile'] = os.environ['COOKIES_FILE']
    with yt_dlp.YoutubeDL(options, auto_init=False) as dl:
        dl.add_info_extractor(ThreadsIE())
        dl.add_default_info_extractors()
        dl.extract_info(validate_url(url), download=True)
    sources = sorted(p for p in folder.iterdir() if p.suffix in ('.mp4', '.webm', '.mkv', '.mov'))
    if not sources:
        raise RuntimeError('Видео недоступно, слишком длинное или превышает лимит размера.')
    return [str(prepare(path, folder, i, height)) for i, path in enumerate(sources[:5])]


if __name__ == '__main__':
    try:
        files = download(sys.argv[1], Path(sys.argv[2]), int(sys.argv[3]) if len(sys.argv) > 3 else 720)
        print(json.dumps({'files': files, 'metadata': {
            file: video_metadata(Path(file), Path(sys.argv[2]), i) for i, file in enumerate(files)}}))
    except Exception as exc:
        if isinstance(exc, (RuntimeError, ValueError)):
            message = str(exc)
        else:
            message = 'Не удалось скачать видео. Пост может быть закрыт, удалён или сайт требует входа. Попробуй другую ссылку.'
        print(json.dumps({'error': message}))
        sys.exit(1)
