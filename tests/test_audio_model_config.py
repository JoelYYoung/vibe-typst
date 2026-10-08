import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('audio_config', ROOT / 'control/audio_model_config.py')
config = importlib.util.module_from_spec(spec)
spec.loader.exec_module(config)


class AudioModelConfigurationTest(unittest.TestCase):
    def test_unconfigured_file_config_and_explicit_environment_precedence(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'TCB_AUDIO_MODELS_URL': '', 'TCB_AUDIO_MODELS_TOKEN': ''}):
            root = Path(temp)
            self.assertEqual(config.container_env(root), [])
            (root / 'audio-models.json').write_text(json.dumps({'url': 'http://host.docker.internal:8840/', 'token': 'a' * 64}))
            self.assertEqual(config.container_env(root), ['-e', 'TCB_AUDIO_MODELS_URL=http://host.docker.internal:8840', '-e', 'TCB_AUDIO_MODELS_TOKEN=' + 'a' * 64])
            with patch.dict(os.environ, {'TCB_AUDIO_MODELS_URL': 'https://models.internal', 'TCB_AUDIO_MODELS_TOKEN': 'b' * 64}):
                self.assertIn('TCB_AUDIO_MODELS_URL=https://models.internal', config.container_env(root))
                self.assertIn('TCB_AUDIO_MODELS_TOKEN=' + 'b' * 64, config.container_env(root))

    def test_invalid_config_does_not_echo_secrets(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'TCB_AUDIO_MODELS_URL': '', 'TCB_AUDIO_MODELS_TOKEN': ''}):
            root = Path(temp)
            for data in ({'url': 'file:///secret', 'token': 's' * 64}, {'url': 'http://user:password@host', 'token': 's' * 64}, {'url': 'http://host', 'token': 'short'}, {}):
                (root / 'audio-models.json').write_text(json.dumps(data))
                with self.assertRaises(ValueError) as caught:
                    config.container_env(root)
                self.assertNotIn('s' * 64, str(caught.exception))
                self.assertNotIn('password', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
