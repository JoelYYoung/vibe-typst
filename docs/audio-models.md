# Optional audio models for video export

The export dialog supports light noise reduction (no models), DPDFNet noise
reduction, and DPDFNet followed by Seed-VC voice conversion. Loudness balancing
runs last in all modes. Original takes and page previews remain unchanged.
Choose one recorded page or upload a reference voice; the first 25 seconds are
used for every page. Use a sample with speech, at least one second long and under
20 MB. References and source takes are pinned when export starts.

The browser uses the export **server's** hardware. Models are optional: ordinary
installation, container builds, and original-voice export do not download weights
or install PyTorch. DPDFNet works on CPU; Seed-VC can use CUDA, Apple Silicon/MPS,
or CPU (slower). Unsupported or disconnected models appear disabled in the dialog.
Model failures stop that export with an error; audio is never silently substituted.

## Install explicitly

Install Python 3, [uv](https://docs.astral.sh/uv/) and FFmpeg. Run from this repository:

```sh
python3 scripts/audio-models.py install --dpdfnet --seed-vc \
  --models-dir /path/to/model-storage/vibe-typst \
  --runtime-dir /path/to/output-storage/vibe-typst/audio-models
python3 scripts/audio-models.py prepare \
  --runtime-dir /path/to/output-storage/vibe-typst/audio-models
python3 scripts/audio-models.py serve \
  --runtime-dir /path/to/output-storage/vibe-typst/audio-models
```

For CPU noise reduction alone, omit `--seed-vc`. A separate Python 3.12 environment
is created under the runtime directory. `prepare` explicitly downloads/loads the
selected models; `serve` runs offline. Leave the worker running while exporting.
Re-run `install` with both flags to add Seed-VC to a denoise-only installation.
Model/source revisions and dependency versions are fixed in the installer/runtime.
Existing files with a different source revision are preserved; choose another
runtime directory to install a different revision.

On this Mac, stable storage roots are `/Users/xavier/External/Models/` and
`/Users/xavier/External/Outputs/`; check their current disk targets before installing.
The installer checks the selected destinations separately. Permission denial uses
project-local `models/audio-models` or `outputs/audio-models` and reports the actual
path. A missing link, unmounted volume or insufficient space stops installation.
The runtime directory contains the isolated environment, source checkout, temporary
audio and caches. Weights and model download caches use model storage.

To reuse an existing Hugging Face cache, pass `--hf-home /existing/hf-home` and
`--hf-cache /existing/hub-cache` to `install`. To reuse the selected DPDFNet ONNX
weight, pass `--dpdfnet-model /existing/dpdfnet8_48khz_hr.onnx`; its SHA-256 is checked.
No existing storage links or cached models are moved or deleted.

## Connect the application

The worker listens on `127.0.0.1:8840` by default. Its generated bearer token lives
in `<runtime-dir>/config.json` (mode 0600). Configure the application backend:

```sh
export TCB_AUDIO_MODELS_URL=http://127.0.0.1:8840
# Read the token from the private runtime config into this environment variable.
export TCB_AUDIO_MODELS_TOKEN=your-private-worker-token
```

Restart the backend with these settings, then use the refresh icon in the export
dialog. Keep the token server-side; it is never sent to the browser or included in
an image. The authenticated worker serves only `/health` and `/process`, accepts
bounded WAV uploads, processes one request at a time, and deletes temporary audio
after download. Concurrent requests receive a busy error and can be retried.

For the multi-user control plane, use the same two environment variables or save
`{"url":"http://host.docker.internal:8840","token":"..."}` in
`$CONTROL_DATA/audio-models.json` with mode 0600. Restart control after upgrading
its code. Newly created user/project containers inherit these private settings;
existing containers need the settings injected when recreated. Keep this file
out of source control and container images.

For Docker/Podman, run the model worker on the host to use its GPU (Mac Docker
containers cannot use MPS). Configure a host address reachable from the container
and inject both settings at container creation. If a loopback listener is not
reachable, choose the worker's bind address explicitly with `install --host ...`.
Do not route the worker through the public Vibe Typst web proxy. For a worker on
another machine, use a private connection with HTTPS. Browser uploads always go
through the authenticated workspace, never directly to the worker.

For macOS auto-start, a LaunchAgent can run the same `serve --runtime-dir ...`
command with explicit Python/repository paths and a PATH containing FFmpeg. Use
writable log paths; the token stays in the private runtime configuration. macOS
may require removable-volume permission for a background process even when a
terminal can read the disk. Do not alter those permissions automatically: use
an internal storage fallback if background access is restricted.

The current Mac deployment runs `com.vibe-typst.audio-models` as a LaunchAgent.
Its internal fallback is `models/audio-models` for weights and
`outputs/audio-models` for the runtime; the external originals are preserved.
Both directories are ignored by Git and excluded from container builds. Internal
service logs are under `control/data/audio-model-launchd.*.log`. This worker uses
MPS; user/project containers connect through the private host bridge.

## Selected processing

[DPDFNet8 48 kHz HR](https://github.com/ceva-ip/DPDFNet) uses its pinned 0.6.0 ONNX
runtime with a 12 dB attenuation limit. Each page resets recurrent state; offline
padding/alignment preserves its sample count. It reduces noise; it is not a
separate dereverberation model.

[Seed-VC](https://github.com/Plachtaa/seed-vc) uses the 44.1 kHz source-F0-conditioned
checkpoint selected in the voice-models research case, with 30 steps, CFG 0.7,
FP32, length adjustment 1.0, no pitch shift and no automatic F0 adjustment.
This is the v1-family model; the `v2` in its checkpoint name does not denote the
separate accent-conversion V2 model. Long F0 extraction uses bounded windows.
Generated audio is resampled to 48 kHz, with only small endpoint padding/trimming;
large duration drift fails the export. Silence and very short takes retain their
timeline. No tempo stretching is applied.

Voice conversion generates a new waveform. Preserved sample counts/page timing do
not guarantee every phoneme or emphasis is unchanged; review the exported audio.
The previous original-voice export remains available by exporting with that option.
