import importlib.util
import json
import os
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
sys.path.insert(0, str(ROOT / 'audio_models'))
import bundled_audio
from model_runtime import ValidatedRuntime


class BundledAudioTest(unittest.TestCase):
    def test_private_worker_starts_once_and_runs_offline_without_mutating_bundle(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'bundle'
            root.mkdir()
            manifest = {'hf_home': '/models/home', 'hf_cache': '/models/cache', 'seed_source': '/seed'}
            path = root / 'bundle.json'
            path.write_text(json.dumps(manifest))
            runtime = Path(temp) / 'run'
            worker = MagicMock()
            worker.poll.return_value = None
            with patch.dict(os.environ, {'TCB_BUNDLED_AUDIO_ROOT': str(root), 'TCB_AUDIO_RUNTIME_DIR': str(runtime)}), \
                    patch.object(bundled_audio, '_worker', None), patch.object(bundled_audio.subprocess, 'Popen', return_value=worker) as spawn:
                with ThreadPoolExecutor(max_workers=8) as pool:
                    settings = list(pool.map(lambda _: bundled_audio.settings(), range(16)))
                self.assertEqual(len(set(settings)), 1)
                self.assertEqual(len(settings[0][1]), 64)
                self.assertEqual(spawn.call_count, 1)
                self.assertEqual(spawn.call_args.kwargs['env']['HF_HUB_OFFLINE'], '1')
                self.assertEqual(spawn.call_args.kwargs['env']['TRANSFORMERS_OFFLINE'], '1')
                self.assertEqual((runtime / 'config.json').stat().st_mode & 0o777, 0o600)
                self.assertEqual(json.loads(path.read_text()), manifest)

    def test_capabilities_require_successful_inference_and_keep_denoiser_if_voice_fails(self):
        class Engine:
            device = 'cpu'
            def __init__(self, config):
                self.seed = None
            def load_seed(self):
                self.seed = object()
            def release_seed(self):
                self.seed = None
        runtime = ValidatedRuntime({'dpdfnet': True, 'seed_vc': True}, engine_factory=Engine)
        self.assertFalse(runtime.status()['denoise'])
        self.assertEqual(runtime.status()['checks']['seed_vc']['state'], 'checking')
        probes = []
        def probe(voice):
            probes.append(voice)
            if voice:
                raise RuntimeError('unsupported voice kernel')
        with patch.object(runtime, 'probe', side_effect=probe), patch('model_runtime.available_memory', return_value=5 * 1024 ** 3), patch('model_runtime.logging.exception'):
            runtime.check()
        self.assertEqual(probes, [False, True])
        self.assertTrue(runtime.status()['denoise'])
        self.assertFalse(runtime.status()['seed_vc'])
        self.assertIsNone(runtime.engine.seed)
        self.assertEqual(runtime.status()['checks']['seed_vc']['state'], 'unavailable')
        with self.assertRaisesRegex(ValueError, 'not ready'):
            runtime.process(Path('input'), Path('output'), reference=Path('reference'))

    def test_memory_or_denoising_failure_disables_only_supported_features(self):
        factory = MagicMock()
        for failing_denoise in (False, True):
            runtime = ValidatedRuntime({'dpdfnet': True, 'seed_vc': True}, engine_factory=factory)
            with patch.object(runtime, 'probe', side_effect=RuntimeError('no runtime') if failing_denoise else None), \
                    patch('model_runtime.available_memory', return_value=1024), patch('model_runtime.logging.exception'):
                runtime.check()
            self.assertEqual(runtime.status()['denoise'], not failing_denoise)
            self.assertFalse(runtime.status()['seed_vc'])
            self.assertEqual(runtime.status()['checks']['seed_vc']['state'], 'unavailable')
        factory.return_value.load_seed.assert_not_called()

    def test_refresh_retries_failed_voice_without_reloading_working_denoiser(self):
        engine = MagicMock()
        engine.device = 'cpu'
        factory = MagicMock(return_value=engine)
        runtime = ValidatedRuntime({'dpdfnet': True, 'seed_vc': True}, engine_factory=factory)
        with patch.object(runtime, 'probe'), patch('model_runtime.available_memory', return_value=1024), patch('model_runtime.logging.exception'):
            runtime.check()
        self.assertTrue(runtime.status()['denoise'])
        self.assertFalse(runtime.status()['seed_vc'])
        def thread(*, target, **kwargs):
            self.assertEqual(runtime.status()['checks']['seed_vc']['state'], 'checking')
            result = MagicMock()
            result.start.side_effect = target
            return result
        with patch.object(runtime, 'probe'), patch('model_runtime.available_memory', return_value=5 * 1024 ** 3), patch('model_runtime.threading.Thread', side_effect=thread) as spawn:
            runtime.retry()
            self.assertTrue(runtime.status()['seed_vc'])
            runtime.retry()
            self.assertEqual(spawn.call_count, 1)
        self.assertEqual(factory.call_count, 1)
        engine.load_seed.assert_called_once()


if __name__ == '__main__':
    unittest.main()
