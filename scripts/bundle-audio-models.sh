#!/usr/bin/env bash
set -euo pipefail
python3 /app/scripts/audio-models.py install --dpdfnet --seed-vc \
  --torch-index https://download.pytorch.org/whl/cpu \
  --models-dir /opt/vibe-audio/models --runtime-dir /opt/vibe-audio
# Builds may reuse an existing pinned weight archive instead of downloading again.
if [ -n "${TCB_AUDIO_WEIGHTS_URL:-}" ]; then
  curl -fsSL "$TCB_AUDIO_WEIGHTS_URL" | tar -xf - -C /opt/vibe-audio/models --no-same-owner
fi
python3 /app/scripts/audio-models.py prepare --runtime-dir /opt/vibe-audio
python3 /app/audio_models/bundle.py /opt/vibe-audio
rm -rf /opt/vibe-audio/cache /opt/vibe-audio/tmp /opt/vibe-audio/work
