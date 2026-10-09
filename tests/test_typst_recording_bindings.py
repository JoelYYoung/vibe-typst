"""Real compilation regressions for source UUIDs independent of page counters."""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
import typst_recording
import presentation_recording as recording
import vcs

PREFIX = ('#import "@preview/touying:0.6.1": *\n#import themes.simple: *\n'
          '#show: simple-theme.with(aspect-ratio: "16-9", header: none)\n'
          '#set text(size: 21pt)\n')


def slide(name, identity, body, arguments=''):
    return f'#{name}{arguments}[{body}] // vibe-typst-recording: {identity * 32}\n'


@unittest.skipUnless(shutil.which('typst'), 'Typst required')
class SourceBindingsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name)
        self.main = self.project / 'main.typ'
        self.render = self.project / 'render'
        self.render.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def bindings(self, source):
        self.main.write_text(source)
        result = subprocess.run(['typst', 'compile', '--root', str(self.project),
                                 str(self.main), str(self.render / 'page-{p}.svg')],
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        pages = len(list(self.render.glob('page-*.svg')))
        # Remove previous output before the next compilation (decks may shrink).
        for path in self.render.glob('page-*.svg'):
            path.unlink()
        (self.render / 'render-source.json').write_text(json.dumps({'source': source, 'pages': pages}))
        with patch.object(typst_recording.runtime, 'render_dir', return_value=self.render):
            ids = typst_recording.page_bindings(self.main, self.project, pages)
        self.assertEqual(self.main.read_text(), source)
        self.assertEqual(list(self.project.glob('.recording-query-*')), [])
        self.assertEqual(len(ids), len(set(ids)))
        return ids

    def test_overflow_stays_with_its_source_when_slides_move_or_disappear(self):
        a = slide('slide', 'a', '#lorem(200)')
        b = slide('slide', 'b', 'Short final slide')
        ids = self.bindings(PREFIX + a + b)
        self.assertGreater(len(ids), 2, 'fixture must actually overflow')
        self.assertEqual(ids[-1], 'b' * 32 + '-0')
        overflow = ids[:-1]
        self.assertEqual(overflow, ['a' * 32 + '-0'] +
                         ['a' * 32 + f'-0-{n}' for n in range(1, len(overflow))])
        # Different source positions and absolute output pages must not change IDs.
        self.assertEqual(self.bindings(PREFIX + b + a), [ids[-1]] + overflow)
        self.assertEqual(self.bindings(PREFIX + a), overflow)

    def test_frozen_title_and_focus_counters_do_not_shift_other_recordings(self):
        source = (PREFIX + slide('title-slide', 'a', 'Title') +
                  slide('slide', 'b', 'First numbered slide') +
                  slide('focus-slide', 'c', 'Focus') + slide('slide', 'd', 'Last'))
        self.assertEqual(self.bindings(source), [char * 32 + '-0' for char in 'abcd'])

    def test_pauses_repeats_and_indented_openers_keep_existing_overlay_ids(self):
        source = (PREFIX + '  ' + slide('slide', 'a', 'Alpha #pause Beta') +
                  slide('slide', 'b', 'Repeated', '(repeat: 3)'))
        self.assertEqual(self.bindings(source), ['a' * 32 + f'-{n}' for n in range(2)] +
                         ['b' * 32 + f'-{n}' for n in range(3)])

    def test_existing_config_preamble_and_relative_images_are_preserved(self):
        # A nested main file must resolve an image relative to its own directory.
        nested = self.project / 'slides'
        nested.mkdir()
        self.main = nested / 'main.typ'
        (nested / 'picture.svg').write_text('<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><rect width="10" height="10" fill="red"/></svg>')
        body = '#image("picture.svg", width: 1cm)'
        config = '(config: config-common(page-preamble: self => [Preamble]))'
        self.assertEqual(self.bindings(PREFIX + slide('slide', 'a', body, config)), ['a' * 32 + '-0'])
        self.assertEqual(list(nested.glob('.recording-query-*')), [])


class BindingValidationTest(unittest.TestCase):
    def test_saving_a_version_during_query_does_not_commit_its_temporary_source(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / 'main.typ').write_text('User source')
            nested = root / 'slides'
            nested.mkdir()
            (nested / '.recording-query-test.typ').write_text('Temporary source')
            self.assertTrue(vcs.save_version(root)['ok'])
            tracked = subprocess.check_output(['git', 'ls-files'], cwd=root, text=True).splitlines()
            self.assertEqual(tracked, ['main.typ'])
            self.assertFalse((root / '.gitignore').exists())

    def test_overflow_ids_are_valid_media_keys_and_round_trip(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            identity = 'a' * 32 + '-2-1'
            staged = root / 'staged'
            staged.write_bytes(b'video')
            metadata = {'page': 1, 'name': 'page-1.svg', 'token': 'a' * 12,
                        'duration': 1, 'mime': 'video/webm', 'recording_id': identity}
            data = recording.save_take(root, 1, staged, metadata, identity)
            self.assertEqual(recording._read_take(root, 3, identity)['take'], data['take'])
            for invalid in (identity + '/x', 'a' * 32 + '-0-0', 'a' * 32 + '-0-1-2'):
                with self.assertRaises(ValueError):
                    recording._take_file(root, 1, invalid)

    def test_missing_duplicate_or_unknown_source_pages_are_not_guessed(self):
        identity = 'a' * 32
        valid = {'id': identity, 'page': 1, 'overlay': 0}
        for rows in ([], [valid, valid], [{**valid, 'id': 'b' * 32}],
                     [{**valid, 'page': True}], [{**valid, 'overlay': -1}], [None]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                typst_recording._bindings_from_pages(rows, [identity], 1)
        rows = [{**valid, 'page': 2}, valid]
        self.assertEqual(typst_recording._bindings_from_pages(rows, [identity], 2),
                         [identity + '-0', identity + '-0-1'])

    def test_query_failure_removes_temporary_source(self):
        with tempfile.TemporaryDirectory() as name:
            main = Path(name) / 'main.typ'
            source = PREFIX + slide('slide', 'a', 'Content')
            main.write_text(source)
            failure = subprocess.CompletedProcess([], 1, '', 'bad compile')
            with patch.object(typst_recording.subprocess, 'run', return_value=failure):
                with self.assertRaisesRegex(ValueError, 'could not resolve'):
                    typst_recording._query_pages(main, main.parent, source)
            self.assertEqual(main.read_text(), source)
            self.assertEqual(list(main.parent.glob('.recording-query-*')), [])


if __name__ == '__main__':
    unittest.main()
