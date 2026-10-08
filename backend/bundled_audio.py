"""Lazy private worker for image-bundled models; no heavy imports in the app."""
import json
import os
import secrets
import subprocess
import threading
from pathlib import Path

_lock = threading.Lock()
_worker = None


def settings():
    global _worker
    root = Path(os.environ.get('TCB_BUNDLED_AUDIO_ROOT', '/opt/vibe-audio'))
    manifest = root / 'bundle.json'
    if not manifest.is_file():
        return '', ''
    with _lock:
        runtime = Path(os.environ.get('TCB_AUDIO_RUNTIME_DIR', '/tmp/tcb-audio-models'))
        runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = runtime / 'config.json'
        if _worker is None:
            config = json.loads(manifest.read_text())
            config.update(runtime_dir=str(runtime), work_dir=str(runtime / 'work'),
                          token=secrets.token_hex(32), host='127.0.0.1', port=8840)
            for name in ('work', 'cache', 'tmp'):
                (runtime / name).mkdir(exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w') as stream:
                json.dump(config, stream)
            env = dict(os.environ, HF_HOME=config['hf_home'], HF_HUB_CACHE=config['hf_cache'],
                       HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1',
                       TORCH_HOME=str(root / 'models/torch'), XDG_CACHE_HOME=str(runtime / 'cache'),
                       MPLCONFIGDIR=str(runtime / 'cache/matplotlib'),
                       NUMBA_CACHE_DIR=str(runtime / 'cache/numba'), TMPDIR=str(runtime / 'tmp'))
            server = Path(__file__).resolve().parents[1] / 'audio_models/server.py'
            with (runtime / 'worker.log').open('ab') as log:
                _worker = subprocess.Popen([str(root / '.venv/bin/python'), str(server), str(path)],
                                           env=env, stdout=log, stderr=log, start_new_session=True)
        config = json.loads(path.read_text())
        return 'http://127.0.0.1:8840', config['token']


def starting():
    return _worker is not None and _worker.poll() is None
