"""Regression coverage for exports that outlive their browser and explicit stop."""
import json
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
import export_control
import presentation_recording as recording
import recording_routes
import server as model_server


class BackgroundExportTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.pages = [{'name': 'page-1.svg', 'token': 'a' * 12}]
        staged = self.root / 'upload'
        staged.write_bytes(b'original')
        self.take = recording.save_take(self.root, 1, staged,
                                       dict(self.pages[0], duration=1, mime='video/webm'))
        self.app = FastAPI()
        self.app.include_router(recording_routes.router(lambda _: (self.root, self.pages)))

    def tearDown(self):
        self.temp.cleanup()

    def wait(self, job):
        for _ in range(400):
            result = recording.get_job(self.root, job['id'])
            if result['status'] not in ('running', 'cancelling') and job['id'] not in recording._controls:
                return result
            time.sleep(.01)
        self.fail('worker did not release its slot')

    def test_browser_disconnect_continues_and_completion_survives_process_state_loss(self):
        entered, release = threading.Event(), threading.Event()
        def encode(clips, durations, work, progress):
            entered.set()
            release.wait(5)
            progress(90)
            output = work / 'presentation.mp4'
            output.write_bytes(b'mp4')
            return output
        with patch.object(recording, 'encode_mp4', side_effect=encode):
            with TestClient(self.app) as client:
                job = client.post('/api/recording/exports').json()
                self.assertTrue(entered.wait(3))
                self.assertTrue(client.get('/api/recording/exports/active').json()['active'])
            # The client is gone; neither request cancellation nor polling is required.
            self.assertEqual(recording.list_takes(self.root, self.pages)['export']['status'], 'running')
            release.set()
            self.assertEqual(self.wait(job)['status'], 'complete')
        with recording._jobs_lock:
            recording._jobs.pop(job['id'])
        with TestClient(self.app) as reopened:
            restored = reopened.get('/api/recording').json()['export']
            self.assertEqual(restored['id'], job['id'])
            self.assertEqual(restored['status'], 'complete')
            self.assertEqual(reopened.get(f'/api/recording/exports/{job["id"]}/video').content, b'mp4')
            self.assertFalse(reopened.get('/api/recording/exports/active').json()['active'])

    def test_cancel_stops_real_ffmpeg_is_idempotent_and_preserves_take(self):
        entered = threading.Event()
        def encode(clips, durations, work, progress):
            entered.set()
            recording._run_ffmpeg(['-re', '-f', 'lavfi', '-i', 'sine=frequency=440',
                                   '-t', '60', str(work / 'wait.wav')])
            raise AssertionError('conversion was not stopped')
        with patch.object(recording, 'encode_mp4', side_effect=encode):
            job = recording.start_export(self.root, self.pages)
            self.assertTrue(entered.wait(3))
            other = self.root / 'other'
            other.mkdir()
            with self.assertRaises(ValueError):
                recording.cancel_export(other, job['id'])
            time.sleep(.2)
            with TestClient(self.app) as client:
                self.assertIn(client.post(f'/api/recording/exports/{job["id"]}/cancel').json()['status'], ['cancelling', 'cancelled'])
                client.post(f'/api/recording/exports/{job["id"]}/cancel')
                self.assertEqual(client.post('/api/recording/exports/../cancel').status_code, 404)
            self.assertEqual(self.wait(job)['status'], 'cancelled')
        self.assertFalse(list(self.root.glob('.export-*')))
        self.assertFalse(list(self.root.glob('*.mp4')))
        self.assertEqual(recording.media_path(self.root, self.take).read_bytes(), b'original')
        def finish(clips, durations, work, progress):
            output = work / 'presentation.mp4'
            output.write_bytes(b'new')
            return output
        with patch.object(recording, 'encode_mp4', side_effect=finish):
            next_job = recording.start_export(self.root, self.pages)
            self.assertEqual(self.wait(next_job)['status'], 'complete')
            self.assertEqual(recording.cancel_export(self.root, next_job['id'])['status'], 'complete')

    def test_private_model_cancel_releases_only_its_job_and_slot(self):
        entered = threading.Event()
        class Engine:
            def status(self):
                return {'denoise': True}
            def process(self, source, output, *, control, **kwargs):
                entered.set()
                while source.read_bytes() == b'long':
                    control.report(25)
                    time.sleep(.02)
                output.write_bytes(b'RIFF')
        app = model_server.create_app(Engine(), 't' * 64, self.root)
        headers = {'Authorization': 'Bearer ' + 't' * 64, 'X-Audio-Job-Id': 'a' * 32}
        with TestClient(app) as client:
            results = []
            thread = threading.Thread(target=lambda: results.append(client.post('/process', headers=headers, files={'audio': ('x.wav', b'long')})))
            thread.start()
            try:
                self.assertTrue(entered.wait(3))
                self.assertEqual(client.post('/process/' + 'b' * 32 + '/cancel', headers=headers).status_code, 404)
                self.assertEqual(client.get('/process/' + 'a' * 32, headers=headers).json()['progress'], 25)
                self.assertEqual(client.post('/process/' + 'a' * 32 + '/cancel').status_code, 401)
                self.assertEqual(client.post('/process/' + 'a' * 32 + '/cancel', headers=headers).status_code, 200)
            finally:
                client.post('/process/' + 'a' * 32 + '/cancel', headers=headers)
                thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(results[0].status_code, 422)
            self.assertEqual(client.post('/process', headers=headers, files={'audio': ('x.wav', b'short')}).status_code, 200)
        self.assertFalse(list(self.root.glob('audio-*')))
