# Optional audio models for video export

The export dialog supports light noise reduction (no models), DPDFNet noise
reduction, and DPDFNet followed by Seed-VC voice conversion. Loudness balancing
runs last in all modes. Original takes and page previews remain unchanged.
Choose one recorded page or upload a reference voice; the first 25 seconds are
used for every page. Use a sample with speech, at least one second long and under
20 MB. References and source takes are pinned when export starts.

The browser uses the export **server's** hardware. Model processing is optional. Docker images include an isolated
model environment and weights; original-voice export does not load them. DPDFNet works on CPU; Seed-VC can use CUDA, Apple Silicon/MPS,
or CPU (slower). Unsupported or disconnected models appear disabled in the dialog.
Model failures stop that export with an error; audio is never silently substituted.

## Background exports

A submitted export continues when you exit the presenter or close the webpage.
Reopen the same project to restore its progress or download. The compact task bar
shows the stage, page count and percentage; its stop icon explicitly cancels the
export. Temporary connection errors retry without changing the server's job state.
Active exports keep their workspace and account session from idle shutdown.
Completed status and MP4 files survive workspace restarts. An unexpected server
restart during an unfinished export reports a failure that can be retried.

Cancel requests stop FFmpeg and cooperatively stop DPDFNet frames/Seed-VC model
steps. The task shows Cancelling until processing and cleanup finish. A model call
already executing on the GPU finishes its current operation before stopping.
Cancel is scoped to that private model request; the shared model service stays up.
Update a standalone worker together with the application to use cancellation.

## Docker defaults

Both `Containerfile.native` and `Containerfile` bundle the pinned model source,
CPU dependencies and all weights by default. No model installation is needed after
starting a workspace. Builds download the selected assets; runtime is offline.
The model environment is isolated under `/opt/vibe-audio`; its bundle contains no
service token. The backend starts a private worker on first capability check and
generates temporary credentials under `/tmp/tcb-audio-models`.

The dialog reports each model as Checking, Ready or Unavailable. Ready requires
an actual inference test, not just installed files. A failed/unsupported model is
disabled. Noise reduction remains available if only voice conversion fails, and
ordinary export remains available throughout. Hover the status for the reason;
use the refresh icon to retry failed checks after freeing resources.

The CPU voice check requires 4 GB of available system/cgroup memory before loading.
The verified ARM64 container used about 4.1 GiB after both checks. CPU conversion
is slower than the host GPU. Reserve additional memory for normal workspace use,
longer recordings and simultaneous users. Apple Docker does not expose MPS; the
existing private host worker can provide MPS acceleration.

## Standalone host worker

For a local backend or host GPU acceleration, install Python 3,
[uv](https://docs.astral.sh/uv/) and FFmpeg, then run from this repository:

```sh
python3 scripts/audio-models.py install --dpdfnet --seed-vc \
  --models-dir /path/to/model-storage/vibe-typst \
  --runtime-dir /path/to/output-storage/vibe-typst/audio-models
python3 scripts/audio-models.py prepare \
  --runtime-dir /path/to/output-storage/vibe-typst/audio-models
python3 scripts/audio-models.py serve \
  --runtime-dir /path/to/output-storage/vibe-typst/audio-models
```

For CPU noise reduction alone, omit `--seed-vc`. `prepare` downloads/loads the
selected pinned models; `serve` runs offline and checks real inference in the
background. To reuse weights, pass `--hf-home`, `--hf-cache` and/or
`--dpdfnet-model`; the selected ONNX checksum is checked. Existing caches remain
unchanged. The installer checks each selected storage volume; permission denial
uses project-local `models/audio-models` or `outputs/audio-models`, while missing
mounts/links or insufficient space stop installation.

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
