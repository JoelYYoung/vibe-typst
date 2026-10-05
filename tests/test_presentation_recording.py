import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
import presentation_recording as recording
import recording_routes
import app as workspace
import projects
import typst_recording


class RecordingTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name)
        self.main = self.project / "main.typ"
        self.main.write_text("test")
        self.root = recording.directory(self.project, self.main)
        self.slides = [{"name": f"page-{page}.svg", "token": str(page) * 12} for page in (1, 2)]

    def tearDown(self):
        self.temp.cleanup()

    def save(self, page, content=b"video", duration=0.6):
        staged = self.root / "staged"
        staged.write_bytes(content)
        return recording.save_take(self.root, page, staged, {
            "page": page, **self.slides[page - 1], "duration": duration,
            "mime": "video/webm", "pointer": [{"t": 0.2, "x": 0.25, "y": 0.75}],
        })

    def test_retake_is_atomic_and_does_not_touch_other_pages(self):
        first = self.save(1, b"old")
        second = self.save(2, b"other")
        new = self.save(1, b"new")
        self.assertNotEqual(new["take"], first["take"])
        self.assertEqual(recording._read_take(self.root, 2)["take"], second["take"])
        self.assertEqual(recording.media_path(self.root, new).read_bytes(), b"new")
        self.assertFalse((self.root / first["take"]).exists())
        staged = self.root / "failed"
        staged.write_bytes(b"failed")
        with patch.object(recording, "write_json", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                recording.save_take(self.root, 1, staged, new)
        self.assertEqual(recording._read_take(self.root, 1)["take"], new["take"])
        self.assertEqual(recording.media_path(self.root, new).read_bytes(), b"new")

    def test_persisted_takes_reload_and_changed_page_is_stale(self):
        self.save(1)
        self.save(2)
        self.slides[1]["token"] = "a" * 12
        result = recording.list_takes(recording.directory(self.project, self.main), self.slides)
        self.assertFalse(result["takes"][0]["stale"])
        self.assertTrue(result["takes"][1]["stale"])
        self.assertNotIn("pointer", result["takes"][0])

    def test_clear_page_is_isolated_idempotent_and_blocks_incomplete_export(self):
        first = self.save(1)
        second = self.save(2)
        api = FastAPI()
        api.include_router(recording_routes.router(lambda project_id: (self.root, self.slides)))
        with TestClient(api) as client:
            self.assertEqual(client.delete('/api/recording/pages/1').status_code, 200)
            self.assertEqual(client.delete('/api/recording/pages/1').status_code, 200)
            self.assertEqual(client.delete('/api/recording/pages/0').status_code, 400)
        self.assertIsNone(recording._read_take(self.root, 1))
        self.assertFalse((self.root / first['take']).exists())
        self.assertEqual(recording._read_take(self.root, 2)['take'], second['take'])
        with self.assertRaisesRegex(ValueError, 'page 1'):
            recording.start_export(self.root, self.slides)

    def test_stable_bindings_follow_reorder_and_delete_without_reusing_old_page_number(self):
        keys = ['a' * 32 + '-0', 'b' * 32 + '-0']
        for page, key in enumerate(keys, 1):
            staged = self.root / 'staged'
            staged.write_bytes(f'video-{page}'.encode())
            recording.save_take(self.root, page, staged, {**self.slides[page - 1], 'page': page, 'duration': page, 'mime': 'video/webm'}, key)
        moved = [{**self.slides[1], 'name': 'page-1.svg', 'recording_id': keys[1]},
                 {**self.slides[0], 'name': 'page-2.svg', 'recording_id': keys[0]}]
        self.assertEqual([take['recording_id'] for take in recording.list_takes(self.root, moved)['takes']], keys[::-1])
        self.assertTrue(all(not take['stale'] for take in recording.list_takes(self.root, moved)['takes']))
        remaining = [moved[0]]
        result = recording.list_takes(self.root, remaining)['takes']
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['recording_id'], keys[1])
        recording.clear_take(self.root, 1, keys[1])
        self.assertIsNotNone(recording._read_take(self.root, 2, keys[0]))

    @unittest.skipUnless(shutil.which('typst'), 'Typst required')
    def test_real_touying_source_anchors_survive_source_reorder_and_deletion(self):
        prefix = '#import "@preview/touying:0.6.1": *\n#import themes.simple: *\n#show: simple-theme.with(aspect-ratio: "16-9")\n'
        a = '#slide[Alpha] // vibe-typst-recording: ' + 'a' * 32 + '\n'
        b = '#slide[Beta] // vibe-typst-recording: ' + 'b' * 32 + '\n'
        def bindings(source, count):
            self.main.write_text(source)
            render = self.project / 'render'
            render.mkdir(exist_ok=True)
            (render / 'render-source.json').write_text(json.dumps({'source': source, 'pages': count}))
            with patch.object(typst_recording.runtime, 'render_dir', return_value=render):
                return typst_recording.page_bindings(self.main, self.project, count)
        self.assertEqual(bindings(prefix + a + b, 2), ['a' * 32 + '-0', 'b' * 32 + '-0'])
        self.assertEqual(bindings(prefix + b + a, 2), ['b' * 32 + '-0', 'a' * 32 + '-0'])
        self.assertEqual(bindings(prefix + b, 1), ['b' * 32 + '-0'])
        self.assertEqual(typst_recording.anchor_edits(prefix + b), [])
        self.assertEqual(len(typst_recording.anchor_edits(prefix + b + b)), 1)
        self.main.write_text(prefix + a)
        with patch.object(typst_recording.runtime, 'render_dir', return_value=self.project / 'render'):
            with self.assertRaisesRegex(ValueError, 'recompiling'):
                typst_recording.page_bindings(self.main, self.project, 1)

    def test_legacy_migration_requires_unique_content_and_preserves_unmatched_takes(self):
        first, second = self.save(1), self.save(2)
        after = [{**slide, 'recording_id': char * 32 + '-0'} for slide, char in zip(self.slides, 'ab')]
        typst_recording.migrate_legacy(self.root, self.slides, after)
        self.assertEqual(recording._read_take(self.root, 1, 'a' * 32 + '-0')['take'], first['take'])
        self.assertEqual(recording._read_take(self.root, 2, 'b' * 32 + '-0')['take'], second['take'])
        unmatched = self.save(1)
        mismatched = [{**self.slides[0], 'token': 'f' * 12}]
        typst_recording.migrate_legacy(self.root, mismatched, [{**mismatched[0], 'recording_id': 'c' * 32 + '-0'}])
        self.assertEqual(recording._read_take(self.root, 1)['take'], unmatched['take'])

    def test_source_bindings_ignore_examples_strings_and_comments(self):
        source = '#slide[\n```typst\n#slide[Example]\n```\n/*\n#slide[Comment]\n*/\n]\n#slide[Real]\n'
        self.assertEqual(len(typst_recording.anchors(source)), 2)
        edits = typst_recording.anchor_edits(source)
        self.assertEqual(len(edits), 2)
        self.assertEqual(edits[1]['selector']['text'], '#slide[Real]')

    def test_repeated_openers_resolve_with_editor_selectors(self):
        source = '#let x = 1\n#slide[\nAlpha\n]\n#slide[\nBeta\n]\n'
        for duplicated in (False, True):
            if duplicated:
                source = source.replace('#slide[', '#slide[ // vibe-typst-recording: ' + 'a' * 32)
            edits = typst_recording.anchor_edits(source)
            resolved = []
            for edit in edits:
                start, end, _, error, _ = typst_recording.docstore._resolve_selector(edit['selector'], source)
                self.assertIsNone(error)
                resolved.append((start, end, edit['text']))
            result = source
            for start, end, text in sorted(resolved, reverse=True):
                result = result[:start] + text + result[end:]
            ids = [identity for _, identity in typst_recording.anchors(result)]
            self.assertEqual(len(set(ids)), 2)
            self.assertNotIn(None, ids)
            self.assertEqual(re.sub(r' // vibe-typst-recording: [a-f0-9]{32}', '', result),
                             re.sub(r' // vibe-typst-recording: [a-f0-9]{32}', '', source))

    def test_mutations_reject_stale_identity_even_when_two_slides_look_identical(self):
        first, second = self.save(1), self.save(2)
        api = FastAPI()
        slides = [{**self.slides[0], 'recording_id': 'a' * 32 + '-0', 'binding_required': True}]
        api.include_router(recording_routes.router(lambda project_id: (self.root, slides)))
        metadata = {**self.slides[0], 'duration': .5, 'mime': 'video/webm', 'recording_id': 'b' * 32 + '-0'}
        with TestClient(api) as client:
            r = client.put('/api/recording/pages/1', data={'metadata': json.dumps(metadata)}, files={'video': ('video', b'bad')})
            self.assertEqual(r.status_code, 409)
        api = FastAPI()
        api.include_router(recording_routes.router(lambda project_id: (self.root, self.slides)))
        with TestClient(api) as client:
            r = client.request('DELETE', '/api/recording/pages/1', json={'take': second['take']})
            self.assertEqual(r.status_code, 409)
            self.assertEqual(client.get('/api/recording/pages/1/video?take=' + second['take']).status_code, 404)
            self.assertEqual(recording._read_take(self.root, 1)['take'], first['take'])

    def test_export_refuses_missing_and_changed_pages_and_releases_slot(self):
        with patch.object(recording.shutil, "which", return_value="ffmpeg"):
            with self.assertRaisesRegex(ValueError, "page 1"):
                recording.start_export(self.root, self.slides)
            self.save(1)
            with self.assertRaisesRegex(ValueError, "page 2"):
                recording.start_export(self.root, self.slides)
            self.save(2)
            self.slides[0]["token"] = "f" * 12
            with self.assertRaisesRegex(ValueError, "page 1"):
                recording.start_export(self.root, self.slides)
        self.assertEqual(list(self.root.glob(".export-*")), [])

    def test_recordings_are_isolated_by_document_and_project(self):
        self.save(1)
        other_main = self.project / "other.typ"
        other_main.write_text("other")
        other_root = recording.directory(self.project, other_main)
        self.assertEqual(recording.list_takes(other_root, self.slides)["takes"], [])
        self.assertNotEqual(other_root, self.root)

    def test_storage_rejects_symlinks_and_file_browser_hides_recording_contents(self):
        other = self.project / "other"
        other.mkdir()
        (other / ".tcb").symlink_to(self.project / ".tcb", target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            recording.directory(other, other / "main.typ")
        self.save(1)
        visible = projects.list_project_items(self.project)
        self.assertFalse(any(".tcb" in row["path"] for row in visible))

    def test_pointer_metadata_rejects_nonfinite_outside_and_backward_samples(self):
        good = {**self.slides[0], "duration": 1, "mime": "video/webm", "pointer": [{"t": .1, "x": .5, "y": .5}]}
        self.assertEqual(recording.validate_metadata(good, 1)["page"], 1)
        invalid = [float("nan"), float("inf"), -1, True]
        for duration in invalid:
            with self.subTest(duration=duration), self.assertRaises(ValueError):
                recording.validate_metadata({**good, "duration": duration}, 1)
        for pointer in ([{"t": .1, "x": 2, "y": .5}], [{"t": .3, "x": None, "y": None}, {"t": .1, "x": .5, "y": .5}], [{"t": 2, "x": .5, "y": .5}]):
            with self.subTest(pointer=pointer), self.assertRaises(ValueError):
                recording.validate_metadata({**good, "pointer": pointer}, 1)

    def test_http_save_rejects_bad_upload_and_keeps_previous_take(self):
        old = self.save(1)
        api = FastAPI()
        api.include_router(recording_routes.router(lambda pid: (self.root, self.slides)))
        with TestClient(api) as client, patch.object(recording, "validate_media", side_effect=ValueError("bad media")):
            metadata = {**old, "pointer": []}
            response = client.put("/api/recording/pages/1", data={"metadata": json.dumps(metadata)}, files={"video": ("video", b"bad")})
            self.assertEqual(response.status_code, 400, response.text)
            self.assertEqual(recording._read_take(self.root, 1)["take"], old["take"])
            self.assertEqual(list(self.root.glob(".upload-*")), [])
            metadata["token"] = "a" * 12
            response = client.put("/api/recording/pages/1", data={"metadata": json.dumps(metadata)}, files={"video": ("video", b"bad")})
            self.assertEqual(response.status_code, 409, response.text)

    def test_preview_cache_failure_and_symlinks_preserve_original(self):
        take = self.save(1)
        original = recording.media_path(self.root, take)
        with patch.object(recording.shutil, 'which', return_value=None):
            self.assertEqual(recording.preview_media_path(self.root, take), original)
        with patch.object(recording.shutil, 'which', return_value='ffmpeg'), patch.object(recording, '_run_ffmpeg', side_effect=ValueError('failed')):
            with self.assertRaises(ValueError):
                recording.preview_media_path(self.root, take)
            self.assertEqual(list(original.parent.glob('.preview-*')), [])
        (original.parent / 'preview-seekable.webm').symlink_to(original)
        with self.assertRaisesRegex(ValueError, 'invalid recording preview'):
            recording.preview_media_path(self.root, take)
        self.assertEqual(original.read_bytes(), b'video')

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
    def test_streaming_preview_has_fixed_duration_ranges_and_unchanged_media(self):
        def probe(path):
            return json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_format', '-show_packets', '-show_data_hash', 'sha256', '-of', 'json', str(path)]))
        for container in ('webm', 'mp4'):
            clip = self.project / ('live.' + container)
            codecs = ['-c:v', 'libvpx', '-c:a', 'libopus', '-live', '1'] if container == 'webm' else ['-c:v', 'libx264', '-c:a', 'aac', '-movflags', 'frag_keyframe+empty_moov']
            subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'color=c=blue:s=320x180:r=30',
                            '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000', '-t', '1.2', *codecs, str(clip)], check=True)
            staged = self.root / 'staged'
            staged.write_bytes(clip.read_bytes())
            take = recording.save_take(self.root, 1, staged, {'page': 1, **self.slides[0], 'duration': 1.2, 'mime': 'video/' + container})
            original = recording.media_path(self.root, take)
            before = probe(original)
            if container == 'webm':
                self.assertNotIn('duration', before['format'])
            with recording.locked(self.root):
                preview = recording.preview_media_path(self.root, take)
            after = probe(preview)
            self.assertAlmostEqual(float(after['format']['duration']), 1.2, delta=.15)
            for stream in (0, 1):
                self.assertEqual([p['data_hash'] for p in before['packets'] if p['stream_index'] == stream],
                                 [p['data_hash'] for p in after['packets'] if p['stream_index'] == stream])
            self.assertEqual(original.read_bytes(), clip.read_bytes())
            if container == 'mp4':
                blob = preview.read_bytes()
                self.assertLess(blob.index(b'moov'), blob.index(b'mdat'))
            with patch.object(recording, '_run_ffmpeg', side_effect=AssertionError('cache must be reused')):
                self.assertEqual(recording.preview_media_path(self.root, take), preview)
                api = FastAPI()
                api.include_router(recording_routes.router(lambda _: (self.root, self.slides)))
                with TestClient(api) as client:
                    response = client.get('/api/recording/pages/1/video', headers={'Range': 'bytes=0-1023'})
                    self.assertEqual(response.status_code, 206)
                    self.assertEqual(response.headers['content-range'], f'bytes 0-1023/{preview.stat().st_size}')
                    self.assertEqual(response.content, preview.read_bytes()[:1024])

    def test_app_resolves_addressed_project_even_when_other_project_is_active(self):
        render = self.project / "render"
        render.mkdir()
        slide = render / "page-1.svg"
        slide.write_bytes(b"slide image")
        info = {"id": "recorded", "type": "typst", "main_file": "main.typ", "path": str(self.project)}
        with patch.object(workspace.projects_mod, "get_project", return_value=info), patch.object(workspace.runtime, "render_dir", return_value=render):
            root, pages = workspace._recording_target("recorded")
        self.assertEqual(root, self.root)
        self.assertEqual(pages, [{"name": slide.name, "token": hashlib.sha1(slide.read_bytes()).hexdigest()[:12], "recording_id": None, "binding_required": True}])

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
    def test_real_mp4_has_audio_correct_slide_order_and_immutable_export_snapshot(self):
        for page, color in ((1, "red"), (2, "blue")):
            clip = self.project / f"{page}.webm"
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c={color}:s=320x180:r=30",
                            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "0.6",
                            "-c:v", "libvpx", "-c:a", "libopus", str(clip)], check=True)
            recording.validate_media(clip)
            self.save(page, clip.read_bytes())
        first_take = recording._read_take(self.root, 1)["take"]
        job = recording.start_export(self.root, self.slides)
        self.save(1, (self.project / "2.webm").read_bytes())
        recording.clear_take(self.root, 2)
        self.assertIsNone(recording._read_take(self.root, 2))
        self.assertEqual(job["takes"][0], first_take)
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            job = recording.get_job(self.root, job["id"])
            if job["status"] != "running":
                break
            time.sleep(.1)
        self.assertEqual(job["status"], "complete", job)
        info = json.loads(subprocess.check_output(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", job["path"]]))
        self.assertTrue(any(row["codec_name"] == "h264" and row["width"] == 1920 and row["height"] == 1080 for row in info["streams"]))
        self.assertTrue(any(row["codec_name"] == "aac" for row in info["streams"]))
        self.assertAlmostEqual(float(info["format"]["duration"]), 1.2, delta=.15)
        for when, channel in ((.2, 0), (.9, 2)):
            pixel = subprocess.check_output(["ffmpeg", "-v", "error", "-ss", str(when), "-i", job["path"], "-frames:v", "1",
                                             "-vf", "scale=1:1", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"])
            self.assertGreater(pixel[channel], 180, (when, pixel))
        pcm = subprocess.check_output(["ffmpeg", "-v", "error", "-i", job["path"], "-vn", "-f", "s16le", "pipe:1"])
        self.assertTrue(any(pcm), "exported audio is silent")
        with self.assertRaises(ValueError):
            recording.get_job(self.project, job["id"])


if __name__ == "__main__":
    unittest.main()
