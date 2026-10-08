"""Optional private worker settings for newly created workspace containers."""
import json
import os
from pathlib import Path
from urllib.parse import urlsplit


def container_env(data_dir: Path):
    url = os.environ.get('TCB_AUDIO_MODELS_URL', '')
    token = os.environ.get('TCB_AUDIO_MODELS_TOKEN', '')
    if not url:
        path = data_dir / 'audio-models.json'
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text())
            url, token = data['url'], data['token']
        except (ValueError, TypeError, KeyError) as exc:
            raise ValueError('Invalid private audio-model configuration.') from exc
    if not isinstance(url, str) or not isinstance(token, str) or len(token) < 32:
        raise ValueError('Configure both the private audio-model URL and token.')
    parsed = urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.query or parsed.fragment:
        raise ValueError('Invalid private audio-model URL.')
    return ['-e', 'TCB_AUDIO_MODELS_URL=' + url.rstrip('/'), '-e', 'TCB_AUDIO_MODELS_TOKEN=' + token]
