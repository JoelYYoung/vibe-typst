"""Optional processing contract, isolation, and real mux/timing checks."""
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
sys.path.insert(0, str(ROOT / 'audio_models'))
import audio_processing
import presentation_recording as recording
import recording_routes
import server as model_server


class OptionalAudioTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.pages = [{'name': 'page-1.svg', 'token': 'a' * 12}, {'name': 'page-2.svg', 'token': 'b' * 12}]
        self.api = FastAPI()
        self.api.include_router(recording_routes.router(lambda _: (self.root, self.pages)))
        self.available = {'denoise': True, 'seed_vc': True, 'denoiser': 'test model'}

    def tearDown(self):
        self.temp.cleanup()

    def save(self, page, content=b'original', duration=2):
        path = self.root / 'upload'
        path.write_bytes(content)
        return recording.save_take(self.root, page, path, {**self.pages[page - 1], 'page': page, 'duration': duration, 'mime': 'video/webm'})

    def wait_job(self, job):
        for _ in range(200):
            result = recording.get_job(self.root, job['id'])
            if result['status'] != 'running':
                return result
            time.sleep(.01)
        self.fail('export did not finish')

    def test_default_does_not_contact_models_and_absent_service_is_unavailable(self):
        with patch.dict(os.environ, {'TCB_AUDIO_MODELS_URL': ''}), patch.object(audio_processing.httpx, 'get', side_effect=AssertionError):
            self.assertFalse(audio_processing.capabilities()['seed_vc'])
            self.assertEqual(audio_processing.validate_options(None), {'denoise': 'basic', 'voice': 'original'})
            with TestClient(self.api) as client:
                self.assertEqual(client.get('/api/recording/audio-models').json()['denoise'], False)
        for option in ({'voice': 'seed-vc'}, {'denoise': 'model'}):
            with patch.object(audio_processing, 'capabilities', return_value={'denoise': False, 'seed_vc': False}):
                with self.assertRaisesRegex(ValueError, 'unavailable'):
                    audio_processing.validate_options(option)

    def test_invalid_processing_options_and_missing_reference_reject_before_job(self):
        self.save(1)
        with patch.object(audio_processing, 'capabilities', return_value=self.available), TestClient(self.api) as client:
            for options in ({'audio': {'voice': 'unknown'}}, {'audio': {'denoise': False}}, {'audio': {'voice': 'seed-vc'}},
                            {'audio': {'voice': 'seed-vc', 'reference_page': True}}, {'audio': {'command': 'run'}}, {'path': '/tmp/file'}):
                response = client.post('/api/recording/exports', json={**options, 'skip_pages': [2]})
                self.assertEqual(response.status_code, 400, response.text)
        self.assertFalse(list(self.root.glob('.export-*')))

    def test_reference_is_pinned_and_replacement_rejected(self):
        first, second = self.save(1, b'first'), self.save(2, b'second')
        options = {'denoise': 'model', 'voice': 'seed-vc', 'reference_page': 2, 'reference_take': second['take']}
        ready, release = threading.Event(), threading.Event()
        captured = {}
        def extract(source, target, duration, denoise=False):
            target.write_bytes(source.read_bytes())
        def encode(clips, durations, work, progress, audio_options, reference):
            ready.set()
            release.wait(5)
            captured.update(clips=[p.read_bytes() for p in clips], reference=reference.read_bytes(), options=audio_options)
            path = work / 'presentation.mp4'
            path.write_bytes(b'mp4')
            return path
        with patch.object(audio_processing, 'capabilities', return_value=self.available), patch.object(recording, 'extract_audio', side_effect=extract), patch.object(recording, 'encode_mp4', side_effect=encode):
            job = recording.start_export(self.root, self.pages, audio_options=options)
            try:
                self.assertTrue(ready.wait(5))
                recording.clear_take(self.root, 2)
                self.save(1, b'retaken')
            finally:
                release.set()
            self.assertEqual(self.wait_job(job)['status'], 'complete')
        self.assertEqual(captured['clips'], [b'first', b'second'])
        self.assertEqual(captured['reference'], b'second')
        new = self.save(2, b'new')
        with patch.object(audio_processing, 'capabilities', return_value=self.available):
            with self.assertRaisesRegex(ValueError, 'reference recording changed'):
                recording.start_export(self.root, self.pages, audio_options=options)
        self.assertEqual(recording._read_take(self.root, 2)['take'], new['take'])

    def test_worker_authorization_order_single_slot_and_temporary_cleanup(self):
        entered, release = threading.Event(), threading.Event()
        class FakeEngine:
            def status(self):
                return {'seed_vc': True, 'denoise': True, 'denoiser': 'test'}
            def process(self, source, output, *, denoise, reference):
                entered.set()
                release.wait(5)
                self.call = (denoise, source.read_bytes(), reference.read_bytes())
                output.write_bytes(b'RIFF-result')
        engine = FakeEngine()
        app = model_server.create_app(engine, 't' * 64, self.root)
        with TestClient(app) as client:
            self.assertEqual(client.get('/health').status_code, 401)
            headers = {'Authorization': 'Bearer ' + 't' * 64}
            self.assertTrue(client.get('/health', headers=headers).json()['seed_vc'])
            responses = []
            def request():
                responses.append(client.post('/process', headers=headers, data={'denoise': 'true'}, files={'audio': ('source.wav', b'voice'), 'reference': ('ref.wav', b'ref')}))
            thread = threading.Thread(target=request)
            thread.start()
            try:
                self.assertTrue(entered.wait(5))
                response = client.post('/process', headers=headers, data={'denoise': 'false'}, files={'audio': ('s.wav', b'v')})
                self.assertEqual(response.status_code, 409)
            finally:
                release.set()
                thread.join(5)
            self.assertEqual(responses[0].content, b'RIFF-result')
            self.assertEqual(engine.call, (True, b'voice', b'ref'))
            self.assertFalse(list(self.root.iterdir()))

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
    def test_uploaded_reference_is_validated_and_failed_models_leave_originals_and_slot(self):
        for page in (1, 2):
            clip = self.root / f'source-{page}.webm'
            subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'color=c=blue:s=160x90:r=30',
                            '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000', '-t', '2', '-c:v', 'libvpx', '-c:a', 'libopus', str(clip)], check=True)
            self.save(page, clip.read_bytes())
        reference = self.root / 'ref.wav'
        recording.extract_audio(clip, reference, 2)
        options = {'audio': {'voice': 'seed-vc', 'denoise': 'model'}}
        hashes = [hashlib.sha256(recording.media_path(self.root, recording._read_take(self.root, page)).read_bytes()).hexdigest() for page in (1, 2)]
        with patch.object(audio_processing, 'capabilities', return_value=self.available), TestClient(self.api) as client:
            bad = client.post('/api/recording/exports', data={'options': __import__('json').dumps(options)}, files={'reference': ('bad.wav', b'bad')})
            self.assertEqual(bad.status_code, 400)
            with patch.object(audio_processing, 'process', side_effect=ValueError('Model failed')):
                response = client.post('/api/recording/exports', data={'options': __import__('json').dumps(options)}, files={'reference': ('ref.wav', reference.read_bytes())})
                self.assertEqual(response.status_code, 200, response.text)
                result = self.wait_job(response.json())
                self.assertEqual(result['status'], 'failed')
                self.assertEqual(result['error'], 'Model failed')
            # No leaked lock or slot: a subsequent ordinary export succeeds.
            response = client.post('/api/recording/exports', json={})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(self.wait_job(response.json())['status'], 'complete')
        self.assertEqual(hashes, [hashlib.sha256(recording.media_path(self.root, recording._read_take(self.root, page)).read_bytes()).hexdigest() for page in (1, 2)])
        self.assertFalse(list(self.root.glob('.reference-*')))
        self.assertFalse(list(self.root.glob('.export-*')))

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
    def test_processing_replaces_only_audio_preserves_duration_and_uses_one_clean_reference(self):
        clips = []
        for index in (0, 1):
            clip = self.root / f'clip-{index}.webm'
            subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', f'color=c={"red" if index == 0 else "blue"}:s=160x90:r=30',
                            '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000', '-t', '2', '-c:v', 'libvpx', '-c:a', 'libopus', str(clip)], check=True)
            clips.append(clip)
        reference = self.root / 'ref.wav'
        recording.extract_audio(clips[0], reference, 2)
        calls = []
        def process(source, output, *, denoise, reference=None):
            calls.append((source.name, denoise, reference.read_bytes() if reference else None))
            if reference:
                subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'sine=frequency=880:sample_rate=48000', '-t', '1.98', str(output)], check=True)
            else:
                shutil.copyfile(source, output)
        work = self.root / 'work'
        work.mkdir()
        progress = []
        with patch.object(audio_processing, 'process', side_effect=process):
            output = recording.encode_mp4(clips, [2, 2], work, progress.append, {'denoise': 'model', 'voice': 'seed-vc'}, reference)
        self.assertIsNone(calls[0][2])
        self.assertEqual([row[1] for row in calls], [True, True, True])
        self.assertEqual(calls[1][2], calls[2][2])
        import json
        info = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_format', '-show_streams', '-of', 'json', str(output)]))
        self.assertAlmostEqual(float(info['format']['duration']), 4, delta=.1)
        self.assertTrue(any(s['codec_name'] == 'aac' and s['sample_rate'] == '48000' for s in info['streams']))
        # Independent zero crossings establish the converted tone reached the MP4.
        from array import array
        pcm = array('f', subprocess.check_output(['ffmpeg', '-v', 'error', '-ss', '0.5', '-i', str(output), '-t', '1', '-ac', '1', '-f', 'f32le', 'pipe:1']))
        crossings = sum(a <= 0 < b for a, b in zip(pcm, pcm[1:]))
        self.assertAlmostEqual(crossings, 880, delta=5)
        self.assertEqual(progress, sorted(progress))


if __name__ == '__main__':
    unittest.main()
