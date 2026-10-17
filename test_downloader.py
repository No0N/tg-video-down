import pytest
from downloader import validate_url, find_post, ThreadsIE


@pytest.mark.parametrize('url', [
    'https://youtube.com.evil.org/watch?v=abc', 'https://127.0.0.1/video',
    'https://youtube.com@127.0.0.1/watch', 'file:///etc/passwd',
    'https://youtube.com:8080/watch?v=abc', 'https://youtube.com/playlist?list=abc',
    'https://instagram.com/someone/', 'https://threads.com/',
])
def test_reject_invalid_links(url):
    with pytest.raises(ValueError):
        validate_url(url)


@pytest.mark.parametrize('url', [
    'https://youtu.be/abc', 'https://www.youtube.com/watch?v=abc&list=xyz',
    'https://youtube.com/shorts/abc', 'https://instagram.com/reel/abc/',
    'https://www.threads.net/@user/post/abc', 'https://threads.com/share/abc',
])
def test_supported_links(url):
    assert validate_url(url).startswith('https://')


def test_threads_matches_requested_post_only():
    wrong = {'code': 'wrong', 'video_versions': [{'url': 'wrong.mp4'}]}
    correct = {'code': 'target', 'video_versions': [{'url': 'correct.mp4'}]}
    assert find_post({'data': [wrong, {'nested': correct}]}, 'target') is correct
    assert find_post([wrong], 'missing') is None


def test_threads_extractor_precedes_generic():
    import yt_dlp
    with yt_dlp.YoutubeDL({'quiet': True}, auto_init=False) as dl:
        dl.add_info_extractor(ThreadsIE())
        dl.add_default_info_extractors()
        selected = next(ie for ie in dl._ies.values() if ie.suitable('https://threads.com/@user/post/abc'))
        assert selected.IE_NAME == ThreadsIE.IE_NAME


@pytest.mark.parametrize('height', [360, 720, 1080])
def test_all_youtube_format_fallbacks_obey_quality_cap(height):
    import yt_dlp
    from downloader import format_selector
    formats = [{'format_id': str(h), 'height': h, 'url': f'https://example.org/{h}.mp4',
                'ext': 'mp4', 'vcodec': 'avc1', 'acodec': 'mp4a'} for h in (144, 360, 720, 1080, 2160)]
    with yt_dlp.YoutubeDL({'format': format_selector(height), 'quiet': True}) as dl:
        info = dl.process_ie_result({'id': 'test', 'title': 'test', 'formats': formats}, download=False)
        assert info['height'] == height


def test_quality_rejects_invalid_value():
    from downloader import format_selector
    with pytest.raises(ValueError):
        format_selector(9999)
