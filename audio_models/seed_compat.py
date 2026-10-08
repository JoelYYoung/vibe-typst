"""Small, checked adaptations of the pinned upstream checkout for this runtime."""
from pathlib import Path


def patch_seed(source: Path):
    replacements = {
        'inference.py': [
            ("os.environ['HF_HUB_CACHE'] = './checkpoints/hf_cache'", '# Cache paths are configured by the private worker.'),
            ('return_tensors="pt",\n                                                   return_attention_mask=True)',
             'return_tensors="pt", sampling_rate=16000,\n                                                   return_attention_mask=True)'),
            ('torchaudio.save(os.path.join(args.output, f"vc_{source_name}_{target_name}_{length_adjust}_{diffusion_steps}_{inference_cfg_rate}.wav"), vc_wave.cpu(), sr)',
             'import soundfile as sf\n    sf.write(os.path.join(args.output, f"vc_{source_name}_{target_name}_{length_adjust}_{diffusion_steps}_{inference_cfg_rate}.wav"), vc_wave.squeeze(0).cpu().numpy(), sr, subtype="FLOAT")'),
        ],
        'modules/length_regulator.py': [
            ('from dac.nn.quantize import VectorQuantize\n', '# Optional quantizer is imported only when selected.\n'),
            ('if vector_quantize:\n                self.vq', 'if vector_quantize:\n                from dac.nn.quantize import VectorQuantize\n                self.vq'),
        ],
    }
    for relative, changes in replacements.items():
        path = source / relative
        text = path.read_text()
        for before, after in changes:
            if after in text:
                continue
            if text.count(before) != 1:
                raise ValueError(f'Unexpected Seed-VC source in {relative}; preserve this checkout and use a new runtime.')
            text = text.replace(before, after)
        path.write_text(text)
