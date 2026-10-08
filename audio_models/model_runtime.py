"""Publish model capabilities only after real, independent inference checks."""
import logging
import tempfile
import threading
from pathlib import Path


def available_memory():
    """Linux system/cgroup headroom; other platforms rely on actual inference."""
    values = []
    try:
        for line in Path('/proc/meminfo').read_text().splitlines():
            if line.startswith('MemAvailable:'):
                values.append(int(line.split()[1]) * 1024)
        maximum = Path('/sys/fs/cgroup/memory.max').read_text().strip()
        if maximum != 'max':
            values.append(int(maximum) - int(Path('/sys/fs/cgroup/memory.current').read_text()))
    except (OSError, ValueError):
        pass
    return min(values) if values else None


class ValidatedRuntime:
    def __init__(self, config, engine_factory=None):
        self.config = config
        self.engine = None
        self.lock = threading.Lock()
        self.inference_lock = threading.Lock()
        self.checks = {key: {'state': 'checking' if config.get(flag) else 'unavailable', 'message': ''}
                       for key, flag in [('denoise', 'dpdfnet'), ('seed_vc', 'seed_vc')]}
        self.engine_factory = engine_factory

    def start(self):
        threading.Thread(target=self.check, daemon=True, name='audio-model-check').start()

    def retry(self):
        with self.lock:
            if any(check['state'] == 'checking' for check in self.checks.values()):
                return
            pending = [key for key, flag in [('denoise', 'dpdfnet'), ('seed_vc', 'seed_vc')]
                       if self.config.get(flag) and self.checks[key]['state'] == 'unavailable']
            if not pending:
                return
            for key in pending:
                self.checks[key] = {'state': 'checking', 'message': ''}
        target = self.check if 'denoise' in pending else self.check_seed
        threading.Thread(target=target, daemon=True, name='audio-model-recheck').start()

    def update(self, key, state, message=''):
        with self.lock:
            self.checks[key] = {'state': state, 'message': message}

    def status(self):
        with self.lock:
            checks = {key: dict(value) for key, value in self.checks.items()}
        return {'denoise': checks['denoise']['state'] == 'ready',
                'seed_vc': checks['seed_vc']['state'] == 'ready',
                'denoiser': 'DPDFNet8 48 kHz HR',
                'device': self.engine.device if self.engine else None, 'checks': checks}

    def check(self):
        from engine import Engine
        factory = self.engine_factory or Engine
        try:
            self.engine = factory(dict(self.config, seed_vc=False))
            if self.config.get('dpdfnet'):
                self.probe(voice=False)
                self.update('denoise', 'ready')
        except Exception:
            logging.exception('Noise reduction inference check failed')
            self.update('denoise', 'unavailable', 'Noise reduction could not run on this server.')
            self.update('seed_vc', 'unavailable', 'Noise reduction is required.')
            return
        if self.config.get('seed_vc'):
            self.check_seed()

    def check_seed(self):
        try:
            headroom = available_memory()
            if headroom is not None and headroom < 4 * 1024 ** 3:
                raise MemoryError('Not enough available memory (4 GB required).')
            self.engine.config = self.config
            self.engine.load_seed()
            self.probe(voice=True)
            self.update('seed_vc', 'ready')
        except Exception as exc:
            logging.exception('Voice conversion inference check failed')
            if hasattr(self.engine, 'release_seed'):
                self.engine.release_seed()
            self.update('seed_vc', 'unavailable', str(exc) if isinstance(exc, MemoryError)
                        else 'Voice conversion could not run on this server.')

    def probe(self, voice):
        import numpy as np
        import soundfile as sf
        from scipy.signal import resample_poly
        from math import gcd
        with tempfile.TemporaryDirectory(prefix='check-', dir=self.config['work_dir']) as temp:
            work = Path(temp)
            source, target = work / 'source.wav', work / 'target.wav'
            if voice:
                example = Path(self.config['seed_source']) / 'examples/source/source_s1.wav'
                signal, rate = sf.read(example, dtype='float32')
                if signal.ndim == 2:
                    signal = signal.mean(axis=1)
                # Select voiced material so the silence shortcut cannot pass this check.
                blocks = [signal[i:i + rate] for i in range(0, min(len(signal), 10 * rate), rate)
                          if len(signal[i:i + rate]) == rate]
                signal = max(blocks, key=lambda block: float(np.mean(block ** 2)))
                if np.sqrt(np.mean(signal ** 2)) < 1e-4:
                    raise ValueError('Missing voiced inference sample.')
                divisor = gcd(rate, 48000)
                signal = resample_poly(signal, 48000 // divisor, rate // divisor)
            else:
                signal = (.03 * np.sin(2 * np.pi * 220 * np.arange(48000) / 48000)).astype('float32')
            sf.write(source, signal, 48000, subtype='PCM_16')
            with self.inference_lock:
                self.engine.process(source, target, denoise=True, reference=source if voice else None)
            output, rate = sf.read(target, dtype='float32')
            if rate != 48000 or output.ndim != 1 or len(output) != len(signal) or not np.isfinite(output).all():
                raise ValueError('Inference output failed format/timing validation.')

    def process(self, source, output, *, denoise=False, reference=None, control=None):
        status = self.status()
        if denoise and not status['denoise'] or reference and not status['seed_vc']:
            raise ValueError('The selected audio model is not ready on this server.')
        with self.inference_lock:
            if control:
                control.check()
                return self.engine.process(source, output, denoise=denoise, reference=reference, control=control)
            return self.engine.process(source, output, denoise=denoise, reference=reference)
