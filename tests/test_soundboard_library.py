from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from ksound_hub import soundboard_library as library


def wav_bytes():
    stream = io.BytesIO()
    with wave.open(stream, 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        wav.writeframes(b'\x00\x00' * 80)
    return stream.getvalue()


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_local_batch_filters_and_deduplicates(self):
        (self.root / 'sub').mkdir()
        for name in ['a.wav', 'sub/b.wav']:
            (self.root / name).write_bytes(wav_bytes())
        (self.root / 'not-a-sound.txt').write_text('skip')
        (self.root / 'link.wav').symlink_to(self.root / 'a.wav')
        entries = library.folder_entries(str(self.root))
        self.assertEqual(len(entries), 2)
        self.assertEqual(len(library.local_entries([self.root/'a.wav'] * 3)), 1)
        with patch.object(library, 'MAX_ITEMS', 1), self.assertRaises(ValueError):
            library.folder_entries(str(self.root))

    def test_bank_relative_urls_metadata_and_invalid_schemes(self):
        entries = library.parse_bank({'sounds': [
            {'name': 'Click', 'url': 'media/click.wav', 'creator': 'Author', 'license': 'CC0'},
            'media/click.wav', 'file:///etc/passwd', 'javascript:alert(1)', {'name': 'missing'}
        ]}, 'https://example.com/bank/index.json')
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].url, 'https://example.com/bank/media/click.wav')
        self.assertEqual(entries[0].creator, 'Author')
        self.assertEqual(entries[0].license, 'CC0')
        for url in ['file:///tmp/a.wav', 'https://key:secret@example.com/sound', 'data:audio/wav,abc']:
            with self.assertRaises(ValueError):
                library.web_url(url)

    def test_redirect_does_not_leak_api_key(self):
        req = Request('https://freesound.org/apiv2/search/', headers={'Authorization': 'Token secret'})
        redirector = library._Redirects()
        for target in ['https://example.com/file', 'http://freesound.org/file']:
            redirected = redirector.redirect_request(req, None, 302, 'Found', {}, target)
            self.assertIsNone(redirected.get_header('Authorization'))
        redirected = redirector.redirect_request(req, None, 302, 'Found', {}, 'https://freesound.org/other')
        self.assertEqual(redirected.get_header('Authorization'), 'Token secret')

    def test_bank_and_download_http_and_limits(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == '/bank.json':
                    body, mime = json.dumps({'sounds': [{'name': 'Click', 'url': '/click.wav', 'license': 'CC0'}]}).encode(), 'application/json'
                elif self.path == '/links':
                    body, mime = b'<a href="/click.wav">Click</a>', 'text/html'
                elif self.path == '/page.wav':
                    body, mime = b'<html>not audio</html>', 'text/html'
                else:
                    body, mime = wav_bytes(), 'audio/wav'
                self.send_response(200)
                self.send_header('Content-Type', mime)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *args): pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        try:
            entries = library.load_bank(base + '/bank.json')
            self.assertEqual(entries[0].title, 'Click')
            self.assertEqual(library.load_bank(base+'/links')[0].url, base+'/click.wav')
            with patch.object(library, 'media_thumbnail', return_value=''):
                result = library.prepare_import(entries, self.root)
                self.assertFalse(result['errors'])
                path = Path(result['ready'][0]['path'])
                self.assertEqual(path.read_bytes(), wav_bytes())
                with patch.object(library, 'MAX_MEDIA_BYTES', 8):
                    failed = library.prepare_import(entries, self.root)
                self.assertFalse(failed['ready'])
                self.assertTrue(failed['errors'])
                self.assertEqual(path.read_bytes(), wav_bytes())
                page = library.prepare_import([library.SoundEntry('Page', url=base+'/page.wav')], self.root)
                self.assertFalse(page['ready'])
            self.assertFalse(list((self.root/'library-media').glob('.incoming-*')))
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_commit_preserves_live_settings_and_duplicate_paths(self):
        sound = self.root / 'sound.wav'
        sound.write_bytes(wav_bytes())
        entry = library.SoundEntry('Click', url='https://example.com/click.wav', creator='Someone', license='CC0')
        prepared = [{'entry': library.asdict(entry), 'path': str(sound), 'background_path': ''}]
        live = {'global_volume': 43, 'folders': ['Memes'], 'slots': [], 'other': {'preserve': True}}
        updated, count = library.append_import(live, prepared, 'memes')
        self.assertEqual(count, 1)
        self.assertEqual(updated['global_volume'], 43)
        self.assertEqual(updated['other'], live['other'])
        self.assertEqual(updated['slots'][0]['folder'], 'Memes')
        self.assertEqual(updated['slots'][0]['license'], 'CC0')
        self.assertEqual(live['slots'], [])
        again, count = library.append_import(updated, prepared, 'New')
        self.assertEqual(count, 0)
        self.assertNotIn('New', again['folders'])
        library.record_imports(self.root, prepared)
        self.assertIn(entry.url, library.active_identities(updated, self.root))
        self.assertNotIn(entry.url, library.active_identities(live, self.root))

    def test_saving_bank_does_not_activate_sounds(self):
        library.save_bank(self.root, 'Mine', 'https://example.com/bank.json')
        library.save_bank(self.root, 'Renamed', 'https://example.com/bank.json')
        data = library.read_library(self.root)
        self.assertEqual(len(data['banks']), 1)
        self.assertEqual(data['banks'][0]['name'], 'Renamed')
        self.assertFalse((self.root/'soundboard.json').exists())

    def test_bulk_move_only_changes_selected_folder_assignments(self):
        live = {'global_volume': 55, 'folders': ['Memes'], 'slots': [
            {'id': str(i), 'path': f'/sounds/{i}.wav', 'trim_db': 3, 'folder': 'Old'} for i in range(4)]}
        updated, moved = library.move_slots_to_folder(live, [0, 2, 2, 99], 'memes')
        self.assertEqual(moved, 2)
        self.assertEqual(updated['slots'][0]['folder'], 'Memes')
        self.assertEqual(updated['slots'][2]['folder'], 'Memes')
        self.assertEqual(updated['slots'][1], live['slots'][1])
        self.assertEqual(updated['slots'][0]['path'], '/sounds/0.wav')
        self.assertEqual(updated['slots'][0]['trim_db'], 3)
        self.assertEqual(live['slots'][0]['folder'], 'Old')
        new, moved = library.move_slots_to_folder(updated, [1, 3], 'New folder')
        self.assertIn('New folder', new['folders'])
        main, moved = library.move_slots_to_folder(new, [1, 3], 'Main')
        self.assertEqual(main['slots'][1]['folder'], '')
        self.assertEqual(len(main['slots']), 4)

    def test_cancellation_does_not_prepare_or_commit_anything(self):
        result = library.prepare_import([library.SoundEntry('skip', url='https://example.com/a.wav')],
                                        self.root, cancelled=lambda: True)
        self.assertTrue(result['cancelled'])
        self.assertEqual(result['ready'], [])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_freesound_mapping_and_key_is_not_in_result(self):
        class Response(io.BytesIO): pass
        response = Response(json.dumps({'results': [{'name': 'Sound', 'username': 'Creator',
            'license': 'CC-BY', 'url': 'https://freesound.org/s/123',
            'previews': {'preview-hq-mp3': 'https://cdn.freesound.org/123.mp3'}}]}).encode())
        with patch.object(library, '_open', return_value=response) as opened:
            results = library.search_freesound('birds', 'session-secret', 2)
        self.assertIn('/apiv2/search/?', opened.call_args.args[0])
        self.assertIn('page=2', opened.call_args.args[0])
        self.assertNotIn('session-secret', opened.call_args.args[0])
        self.assertEqual(results[0].creator, 'Creator')
        self.assertEqual(results[0].license, 'CC-BY')
        self.assertNotIn('session-secret', str(results))

    @unittest.skipUnless(shutil.which('ffmpeg'), 'ffmpeg not installed')
    def test_video_thumbnail_is_created_once_and_cached(self):
        video = self.root/'sample.mp4'
        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i',
            'color=c=blue:s=64x48:d=0.1', '-c:v', 'mpeg4', '-threads', '1', str(video)], check=True)
        thumbnail = library.media_thumbnail(video, self.root/'thumbnails')
        self.assertTrue(Path(thumbnail).is_file())
        with patch.object(library.subprocess, 'run', side_effect=AssertionError('must use cache')):
            self.assertEqual(library.media_thumbnail(video, self.root/'thumbnails'), thumbnail)


if __name__ == '__main__':
    unittest.main()
