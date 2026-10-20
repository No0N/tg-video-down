import shutil
import subprocess
from pathlib import Path

import pytest

from downloader import prepare, probe_video, video_metadata, pixel_ratio, rotation

pytestmark = pytest.mark.skipif(not shutil.which('ffmpeg') or not shutil.which('ffprobe'), reason='FFmpeg required')


@pytest.mark.parametrize('size,sar', [('180x320', '1/1'), ('320x180', '1/1'), ('320x180', '2/1')])
def test_geometry_and_telegram_metadata(tmp_path, size, sar):
    source = tmp_path / 'input.mp4'
    subprocess.run(['ffmpeg', '-y', '-v', 'error', '-f', 'lavfi', '-i', f'color=size={size}:duration=0.3',
                    '-vf', f'setsar={sar}', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(source)], check=True)
    original = next(s for s in probe_video(source)['streams'] if s['codec_type'] == 'video')
    expected_ratio = original['width'] * float(pixel_ratio(original)) / original['height']
    output = prepare(source, tmp_path, 0)
    metadata = video_metadata(output, tmp_path, 0)
    final = next(s for s in probe_video(output)['streams'] if s['codec_type'] == 'video')
    assert pixel_ratio(final) == 1
    assert abs(metadata['width'] / metadata['height'] - expected_ratio) < .01
    assert metadata['duration'] == 1
    assert Path(metadata['thumbnail']).stat().st_size < 200_000
    thumb = probe_video(Path(metadata['thumbnail']))['streams'][0]
    assert max(thumb['width'], thumb['height']) <= 320


def test_rotated_video_is_normalized(tmp_path):
    source = tmp_path / 'base.mp4'
    rotated = tmp_path / 'rotated.mp4'
    subprocess.run(['ffmpeg', '-y', '-v', 'error', '-f', 'lavfi', '-i', 'color=size=320x180:duration=0.3',
                    '-c:v', 'libx264', str(source)], check=True)
    subprocess.run(['ffmpeg', '-y', '-v', 'error', '-display_rotation', '90', '-i', str(source),
                    '-c', 'copy', str(rotated)], check=True)
    assert rotation(probe_video(rotated)['streams'][0]) == 90
    output = prepare(rotated, tmp_path, 0)
    final = next(s for s in probe_video(output)['streams'] if s['codec_type'] == 'video')
    assert (final['width'], final['height']) == (180, 320)
    assert rotation(final) == 0
    assert pixel_ratio(final) == 1
