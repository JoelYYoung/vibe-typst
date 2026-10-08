"""Run in the optional environment; standard app tests need no NumPy/PyTorch."""
import hashlib
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'audio_models'))
from engine import Engine, bounded_pitch


@unittest.skipUnless(importlib.util.find_spec('numpy') and importlib.util.find_spec('soundfile'), 'optional audio runtime required')
class AudioRuntimeTest(unittest.TestCase):
    def test_pitch_windows_preserve_frame_positions_and_float32_on_short_and_long_inputs(self):
        import numpy as np
        class Audio:
            def __init__(self, size, start=0):
                self.size, self.start = size, start
            def numel(self):
                return self.size
            def __getitem__(self, span):
                return Audio(span.stop - span.start, self.start + span.start)
        def pitch(audio, thred):
            return np.arange(audio.numel() // 160 + 1, dtype=np.float64) + audio.start // 160
        for seconds in (1, 31, 61):
            result = bounded_pitch(pitch)(Audio(seconds * 16000))
            self.assertEqual(result.dtype, np.float32)
            np.testing.assert_array_equal(result, np.arange(seconds * 100 + 1))

    def test_silent_and_short_takes_do_not_generate_voice_and_retain_exact_sample_count(self):
        import numpy as np
        import soundfile as sf
        class Seed:
            def main(self, args):
                raise AssertionError('silence/short takes must not generate a voice')
        with patch.object(Engine, 'load'):
            engine = Engine({})
        engine.seed = Seed()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            reference = root / 'reference.wav'
            sf.write(reference, .1 * np.sin(np.arange(48000) * .05), 48000)
            for index, signal in enumerate((np.zeros(48000), np.ones(4800) * .1)):
                source, output = root / f'source-{index}.wav', root / f'output-{index}.wav'
                sf.write(source, signal, 48000)
                engine.process(source, output, reference=reference)
                result, rate = sf.read(output)
                self.assertEqual(rate, 48000)
                self.assertEqual(len(result), len(signal))
                np.testing.assert_allclose(result, signal, atol=1/32768)

    def test_silent_reference_and_large_generation_drift_fail_without_changing_source(self):
        import numpy as np
        import soundfile as sf
        class Seed:
            def main(self, args):
                sf.write(Path(args.output) / 'generated.wav', np.ones(72000) * .1, 48000)
        with patch.object(Engine, 'load'):
            engine = Engine({})
        engine.seed = Seed()
        engine.seed_args = {}
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, reference = root / 'source.wav', root / 'reference.wav'
            sf.write(source, np.ones(48000) * .1, 48000)
            original = hashlib.sha256(source.read_bytes()).hexdigest()
            sf.write(reference, np.zeros(48000), 48000)
            with self.assertRaisesRegex(ValueError, 'silent'):
                engine.process(source, root / 'output.wav', reference=reference)
            sf.write(reference, np.ones(48000) * .1, 48000)
            with self.assertRaisesRegex(ValueError, 'changed duration'):
                engine.process(source, root / 'output.wav', reference=reference)
            self.assertEqual(original, hashlib.sha256(source.read_bytes()).hexdigest())


if __name__ == '__main__':
    unittest.main()
