#!/usr/bin/env python3
"""Explicit installation/launch of the optional, private audio-model worker."""
import argparse
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEED_REVISION = '51383efd921027683c89e5348211d93ff12ac2a8'
DPDFNET_REVISION = 'dd6818d00f50c836fed43a6243ebe49116de5964'
DPDFNET_SHA256 = '7b3afbb260a08fe9af3d16e3bda992971be1e7e951d1dee7c2d235f5c43f5631'


def storage(path, required_gb, fallback_name):
    path = path.expanduser().absolute()
    try:
        # Resolve existing parents before mkdir: a disconnected storage link is a blocker.
        for parent in (path, *path.parents):
            if parent.is_symlink() and not parent.exists():
                raise RuntimeError(f'Storage link is unavailable: {parent}')
        resolved = path.resolve()
        if sys.platform == 'darwin' and len(resolved.parts) > 2 and resolved.parts[1] == 'Volumes':
            volume = Path(*resolved.parts[:3])
            if not volume.is_mount():
                raise RuntimeError(f'Storage volume is not mounted: {volume}')
        existing = resolved
        while not existing.exists():
            existing = existing.parent
        if shutil.disk_usage(existing).free < required_gb * 1024 ** 3:
            raise RuntimeError(f'Need at least {required_gb} GB free for {path}')
        path.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=path):
            pass
        return path
    except PermissionError:
        fallback = ROOT / fallback_name / 'audio-models'
        if path == fallback:
            raise
        print(f'Permission denied for {path}; using {fallback}', file=sys.stderr)
        return storage(fallback, required_gb, fallback_name)


def environment(config, downloads=False):
    env = dict(os.environ)
    env.update(HF_HOME=config['hf_home'], HF_HUB_CACHE=config['hf_cache'],
               TORCH_HOME=str(Path(config['models_dir']) / 'torch'),
               XDG_CACHE_HOME=str(Path(config['runtime_dir']) / 'cache'),
               UV_CACHE_DIR=str(Path(config['runtime_dir']) / 'cache/uv'),
               UV_PYTHON_INSTALL_DIR=str(Path(config['runtime_dir']) / 'python'),
               TMPDIR=str(Path(config['runtime_dir']) / 'tmp'),
               NUMBA_CACHE_DIR=str(Path(config['runtime_dir']) / 'cache/numba'),
               MPLCONFIGDIR=str(Path(config['runtime_dir']) / 'cache/matplotlib'),
               DPDFNET_MODEL_DIR=str(Path(config['dpdfnet_model']).parent),
               DPDFNET_CACHE_DIR=str(Path(config['models_dir']) / 'dpdfnet-cache'),
               PYTORCH_ENABLE_MPS_FALLBACK='1', HF_HUB_OFFLINE='0' if downloads else '1',
               TRANSFORMERS_OFFLINE='0' if downloads else '1')
    return env


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['install', 'prepare', 'serve'])
    parser.add_argument('--runtime-dir', type=Path, required=True)
    parser.add_argument('--models-dir', type=Path)
    parser.add_argument('--hf-home', type=Path)
    parser.add_argument('--hf-cache', type=Path)
    parser.add_argument('--dpdfnet', action='store_true')
    parser.add_argument('--dpdfnet-model', type=Path)
    parser.add_argument('--seed-vc', action='store_true')
    parser.add_argument('--device', choices=['auto', 'cpu', 'cuda', 'mps'], default='auto')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8840)
    args = parser.parse_args()
    runtime = args.runtime_dir.expanduser().absolute()
    if args.action == 'install':
        if not args.models_dir or not (args.seed_vc or args.dpdfnet):
            parser.error('install requires --models-dir and --dpdfnet and/or --seed-vc')
        if not shutil.which('uv') or not shutil.which('ffmpeg'):
            parser.error('install uv and FFmpeg first')
        runtime = storage(runtime, 3 if args.seed_vc else 1, 'outputs')
        models = storage(args.models_dir, 10 if args.seed_vc else 1, 'models')
        hf_home = storage(args.hf_home, 1, 'models') if args.hf_home else models / 'huggingface'
        hf_cache = storage(args.hf_cache, 8, 'models') if args.hf_cache else hf_home / 'hub'
        dpdfnet_model = args.dpdfnet_model.expanduser().absolute() if args.dpdfnet_model else models / 'dpdfnet/dpdfnet8_48khz_hr.onnx'
        if args.dpdfnet_model and not dpdfnet_model.is_file():
            raise RuntimeError('The supplied DPDFNet model is unavailable.')
        config_path = runtime / 'config.json'
        previous = json.loads(config_path.read_text()) if config_path.exists() else {}
        config = {'runtime_dir': str(runtime), 'models_dir': str(models), 'hf_home': str(hf_home),
                  'hf_cache': str(hf_cache), 'dpdfnet_model': str(dpdfnet_model),
                  'seed_source': str(runtime / 'seed-vc'), 'work_dir': str(runtime / 'work'),
                  'seed_vc': args.seed_vc or previous.get('seed_vc', False), 'dpdfnet': args.dpdfnet or previous.get('dpdfnet', False),
                  'device': args.device, 'host': args.host, 'port': args.port,
                  'token': previous.get('token') or secrets.token_hex(32), 'seed_revision': SEED_REVISION}
        for name in ('tmp', 'work', 'cache'):
            (runtime / name).mkdir(exist_ok=True)
        env = environment(config, downloads=True)
        python = runtime / '.venv/bin/python'
        if not python.exists():
            subprocess.run(['uv', 'venv', '--python', '3.12', str(runtime / '.venv')], env=env, check=True)
        if subprocess.run([str(python), '-c', 'import sys; sys.exit(sys.version_info < (3, 11))']).returncode:
            raise RuntimeError('The existing runtime uses Python <3.11. Preserve it and select a new runtime directory.')
        requirements = ['-r', str(ROOT / 'audio_models/requirements-base.txt')]
        if config['dpdfnet']:
            requirements += ['-r', str(ROOT / 'audio_models/requirements-dpdfnet.txt')]
        if config['seed_vc']:
            requirements += ['-r', str(ROOT / 'audio_models/requirements-seed.txt')]
        subprocess.run(['uv', 'pip', 'install', '--python', str(python), *requirements], env=env, check=True)
        source = runtime / 'seed-vc'
        marker = source / '.vibe-typst-revision'
        if config['seed_vc'] and source.exists() and (not marker.exists() or marker.read_text().strip() != SEED_REVISION):
            raise RuntimeError('Existing Seed-VC checkout differs; select a new runtime directory.')
        if config['seed_vc'] and not source.exists():
            with tempfile.TemporaryDirectory(dir=runtime / 'tmp') as temp:
                archive = Path(temp) / 'seed.tar.gz'
                urllib.request.urlretrieve('https://codeload.github.com/Plachtaa/seed-vc/tar.gz/' + SEED_REVISION, archive)
                with tarfile.open(archive) as tar:
                    # Only plain files/directories in the pinned checkout; reject links/traversal.
                    for member in tar.getmembers():
                        path = Path(member.name)
                        if path.is_absolute() or '..' in path.parts or not (member.isfile() or member.isdir()):
                            raise RuntimeError('Unexpected archive entry')
                    tar.extractall(temp)
                shutil.move(str(Path(temp) / ('seed-vc-' + SEED_REVISION)), source)
                marker.write_text(SEED_REVISION + '\n')
        if config['seed_vc']:
            sys.path.insert(0, str(ROOT / 'audio_models'))
            from seed_compat import patch_seed
            patch_seed(source)
        fd = os.open(config_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(config, stream, indent=2)
        os.chmod(config_path, 0o600)
        print(f'Optional runtime installed. Run prepare with --runtime-dir {runtime} to download and verify models.')
        return
    config_path = runtime / 'config.json'
    config = json.loads(config_path.read_text())
    # Re-check each volume; never let model libraries download into a disconnected path.
    for key in ('runtime_dir', 'models_dir', 'hf_home', 'hf_cache'):
        checked = storage(Path(config[key]), 1, 'outputs' if key == 'runtime_dir' else 'models')
        if checked != Path(config[key]):
            raise RuntimeError('Storage permission changed; reinstall using the reported fallback location.')
    command = [str(runtime / '.venv/bin/python'), str(ROOT / 'audio_models/server.py'), str(config_path)]
    if args.action == 'prepare':
        if config.get('dpdfnet'):
            model = Path(config['dpdfnet_model'])
            if not model.exists():
                model.parent.mkdir(parents=True, exist_ok=True)
                fd, name = tempfile.mkstemp(prefix='.dpdfnet-', dir=model.parent)
                os.close(fd)
                try:
                    urllib.request.urlretrieve('https://huggingface.co/Ceva-IP/DPDFNet/resolve/' + DPDFNET_REVISION + '/onnx/dpdfnet8_48khz_hr.onnx', name)
                    if hashlib.sha256(Path(name).read_bytes()).hexdigest() != DPDFNET_SHA256:
                        raise RuntimeError('Downloaded DPDFNet model checksum differs.')
                    os.replace(name, model)
                finally:
                    Path(name).unlink(missing_ok=True)
        command.append('--prepare')
    subprocess.run(command, env=environment(config, downloads=args.action == 'prepare'), check=True)


if __name__ == '__main__':
    try:
        main()
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        raise SystemExit(str(exc))
