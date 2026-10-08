"""Optional, pinned DPDFNet + source-F0-conditioned Seed-VC inference."""
import hashlib
import os
import sys
from pathlib import Path
from types import SimpleNamespace

DPDFNET_SHA256 = '7b3afbb260a08fe9af3d16e3bda992971be1e7e951d1dee7c2d235f5c43f5631'
SEED_REVISIONS = {
    'Plachta/Seed-VC': '257283f9f41585055e8f858fba4fd044e5caed6e',
    'lj1995/VoiceConversionWebUI': 'e6d0c1a17da07c33557852f9dfa2bd44cc75737d',
    'funasr/campplus': 'e4b6ede7ce16997aff4ae69fbca1f0175e2afede',
    'nvidia/bigvgan_v2_44khz_128band_512x': '95a9d1dcb12906c03edd938d77b9333d6ded7dfb',
    'openai/whisper-small': '973afd24965f72e36ca33b3055d56a652f456b4d',
}


def bounded_pitch(pitch):
    """Bound F0 memory to 30s, with context on the native 10ms pitch grid."""
    def infer(audio, thred=.03):
        import numpy as np
        if audio.numel() <= 30 * 16000:
            return np.asarray(pitch(audio, thred=thred), dtype=np.float32)
        total = audio.numel() // 160 + 1
        result = np.empty(total, dtype=np.float32)
        for start in range(0, total, 3000):
            end = min(start + 3000, total)
            left, right = max(0, start - 50), min(total, end + 50)
            block = pitch(audio[left * 160:min(right * 160, audio.numel())], thred=thred)
            result[start:end] = block[start - left:end - left]
        return result
    return infer


class Engine:
    def __init__(self, config):
        self.config = config
        self.device = 'cpu'
        self.seed = None
        self.denoiser = None
        self.load()

    def load(self):
        if self.config.get('dpdfnet'):
            model = Path(self.config['dpdfnet_model'])
            if hashlib.sha256(model.read_bytes()).hexdigest() != DPDFNET_SHA256:
                raise ValueError('DPDFNet weight checksum differs from the selected 48 kHz HR model.')
            from dpdfnet.onnx_backend import build_runtime_model
            self.denoiser = build_runtime_model(model)
        if self.config.get('seed_vc'):
            self.load_seed()

    def load_seed(self):
        import torch
        import yaml
        from huggingface_hub import hf_hub_download
        torch.set_num_threads(2)
        auto = 'cuda' if torch.cuda.is_available() else 'mps' if torch.backends.mps.is_available() else 'cpu'
        self.device = self.config.get('device', 'auto')
        if self.device == 'auto':
            self.device = auto
        source = Path(self.config['seed_source'])
        sys.path.insert(0, str(source))
        os.chdir(source)
        def weight(repo_id, filename):
            return Path(hf_hub_download(repo_id=repo_id, filename=filename,
                                       revision=SEED_REVISIONS[repo_id], cache_dir=self.config['hf_cache']))
        seed_config = weight('Plachta/Seed-VC', 'config_dit_mel_seed_uvit_whisper_base_f0_44k.yml')
        checkpoint = weight('Plachta/Seed-VC', 'DiT_seed_v2_uvit_whisper_base_f0_44k_bigvgan_pruned_ft_ema_v2.pth')
        cfg = yaml.safe_load(seed_config.read_text())
        for key, repo, names in (
            ('vocoder', 'nvidia/bigvgan_v2_44khz_128band_512x', ('config.json', 'bigvgan_generator.pt')),
            ('speech_tokenizer', 'openai/whisper-small', ('config.json', 'model.safetensors', 'preprocessor_config.json')),
        ):
            paths = [weight(repo, name) for name in names]
            cfg['model_params'][key]['name'] = str(paths[0].parent)
        local_config = Path(self.config['runtime_dir']) / 'inference-config.yml'
        local_config.write_text(yaml.safe_dump(cfg))
        import inference
        # Upstream changes HF_HUB_CACHE during import; restore the explicit storage.
        os.environ['HF_HUB_CACHE'] = self.config['hf_cache']
        inference.device = torch.device(self.device)
        def custom(repo_id, model_filename='pytorch_model.bin', config_filename=None):
            model = str(weight(repo_id, model_filename))
            return (model, str(weight(repo_id, config_filename))) if config_filename else model
        inference.load_custom_model_from_hf = custom
        self.seed_args = dict(diffusion_steps=30, length_adjust=1.0, inference_cfg_rate=.7,
                              f0_condition=True, auto_f0_adjust=False, semi_tone_shift=0,
                              checkpoint=str(checkpoint), config=str(local_config), fp16=False)
        loaded = list(inference.load_models(SimpleNamespace(**self.seed_args)))
        loaded[2] = bounded_pitch(loaded[2])
        # Worker serializes jobs: retain models between pages without reloading weights.
        self.original_loader = inference.load_models
        inference.load_models = lambda _: tuple(loaded)
        self.seed = inference

    def release_seed(self):
        if self.seed:
            self.seed.load_models = self.original_loader
            self.seed = None
            import gc
            import torch
            gc.collect()
            if self.device == 'cuda':
                torch.cuda.empty_cache()
            elif self.device == 'mps':
                torch.mps.empty_cache()

    def status(self):
        return {'seed_vc': self.seed is not None, 'denoise': self.denoiser is not None,
                'denoiser': 'DPDFNet8 48 kHz HR' if self.denoiser else None, 'device': self.device}

    def process(self, source: Path, output: Path, *, denoise=False, reference=None):
        import numpy as np
        import soundfile as sf
        from scipy.signal import resample_poly
        signal, rate = sf.read(source, dtype='float32')
        if signal.ndim != 1 or rate != 48000 or not len(signal) or not np.isfinite(signal).all():
            raise ValueError('Expected a finite mono 48 kHz WAV.')
        cleaned = source
        if denoise:
            if self.denoiser is None:
                raise ValueError('DPDFNet is not installed.')
            from dpdfnet.api import _enhance_with_runtime
            clean_signal = _enhance_with_runtime(signal, sample_rate=rate, runtime=self.denoiser,
                                                model_sample_rate=48000, attn_limit_db=12)
            if len(clean_signal) != len(signal) or not np.isfinite(clean_signal).all():
                raise ValueError('Noise reduction changed timing or produced invalid audio.')
            cleaned = output.parent / 'cleaned.wav'
            sf.write(cleaned, clean_signal, rate, subtype='FLOAT')
        if reference:
            if self.seed is None:
                raise ValueError('Seed-VC is not installed.')
            ref_signal, ref_rate = sf.read(reference, dtype='float32')
            if ref_signal.ndim != 1 or not 1 <= len(ref_signal) / ref_rate <= 25.1 or not np.isfinite(ref_signal).all():
                raise ValueError('Choose a reference voice of 1–25 seconds.')
            if np.sqrt(np.mean(ref_signal ** 2)) < 1e-4:
                raise ValueError('Reference audio is silent. Choose a sample with speech.')
            clean_signal, clean_rate = sf.read(cleaned, dtype='float32')
            # Short/silent takes have no usable voice features; retain their timeline.
            if len(clean_signal) / clean_rate >= .3 and np.sqrt(np.mean(clean_signal ** 2)) >= 1e-4:
                converted = output.parent / 'converted'
                converted.mkdir()
                args = SimpleNamespace(**self.seed_args, source=str(cleaned), target=str(reference), output=str(converted))
                self.seed.main(args)
                files = list(converted.glob('*.wav'))
                if len(files) != 1:
                    raise ValueError('Seed-VC did not return one audio recording.')
                wave, generated_rate = sf.read(files[0], dtype='float32')
                if wave.ndim != 1 or abs(len(wave) / generated_rate - len(signal) / rate) > .25:
                    raise ValueError('Converted audio changed duration; retry with another reference.')
                from math import gcd
                divisor = gcd(rate, generated_rate)
                result = resample_poly(wave, rate // divisor, generated_rate // divisor)
            else:
                result = clean_signal
        else:
            result, clean_rate = sf.read(cleaned, dtype='float32')
            if clean_rate != rate:
                raise ValueError('Noise reduction changed the audio sample rate.')
        if not np.isfinite(result).all():
            raise ValueError('Audio model produced invalid samples.')
        # Frame quantization may leave a few ms; do not stretch or re-time speech.
        result = np.pad(result[:len(signal)], (0, max(0, len(signal) - len(result))))
        sf.write(output, result, rate, subtype='PCM_16')
