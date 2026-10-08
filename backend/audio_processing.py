"""Opt-in audio-model client. Ordinary exports never contact/download models."""
import os
import wave
import threading
import uuid
from pathlib import Path

import httpx
import bundled_audio
import export_control

MAX_REFERENCE_BYTES = 20 * 1024 * 1024
MAX_AUDIO_BYTES = 384 * 1024 * 1024


def _settings():
    url = os.environ.get('TCB_AUDIO_MODELS_URL', '').rstrip('/')
    token = os.environ.get('TCB_AUDIO_MODELS_TOKEN', '')
    if not url:
        url, token = bundled_audio.settings()
    return url, {'Authorization': f'Bearer {token}'}


def capabilities(refresh=False):
    url, headers = _settings()
    unavailable = {'denoise': False, 'seed_vc': False, 'denoiser': None,
                   'message': 'Audio model runtime unavailable.'}
    if not url:
        return unavailable
    try:
        response = httpx.get(url + '/health' + ('?refresh=true' if refresh else ''), headers=headers, timeout=2, follow_redirects=False)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            return unavailable
        return {'denoise': data.get('denoise') is True, 'seed_vc': data.get('seed_vc') is True,
                'denoiser': data.get('denoiser') if isinstance(data.get('denoiser'), str) else None,
                'message': data.get('message', ''), 'device': data.get('device'),
                'checks': data.get('checks', {})}
    except (httpx.HTTPError, ValueError):
        if not os.environ.get('TCB_AUDIO_MODELS_URL') and bundled_audio.starting():
            return dict(unavailable, checks={key: {'state': 'checking', 'message': ''}
                        for key in ('denoise', 'seed_vc')})
        return unavailable


def validate_options(value):
    if value is None:
        value = {}
    if not isinstance(value, dict) or set(value) - {'denoise', 'voice', 'reference_page', 'reference_take'}:
        raise ValueError('invalid audio processing options')
    denoise, voice = value.get('denoise', 'basic'), value.get('voice', 'original')
    if denoise not in ('basic', 'model') or voice not in ('original', 'seed-vc'):
        raise ValueError('invalid audio processing options')
    result = {'denoise': denoise, 'voice': voice}
    if voice == 'seed-vc':
        page = value.get('reference_page')
        if page is not None:
            if type(page) is not int or page < 1:
                raise ValueError('invalid reference page')
            take = value.get('reference_take')
            if not isinstance(take, str) or len(take) != 32 or any(c not in '0123456789abcdef' for c in take):
                raise ValueError('reload recordings before choosing the reference voice')
            result.update(reference_page=page, reference_take=take)
    elif 'reference_page' in value or 'reference_take' in value:
        raise ValueError('reference voice requires Seed-VC')
    if denoise == 'model' or voice == 'seed-vc':
        available = capabilities()
        if denoise == 'model' and not available['denoise']:
            raise ValueError('Model noise reduction is unavailable on this server.')
        if voice == 'seed-vc' and not available['seed_vc']:
            raise ValueError('Voice conversion is unavailable on this server.')
        if voice == 'seed-vc' and denoise != 'model':
            raise ValueError('Select model noise reduction before using Seed-VC.')
    return result


def process(source: Path, output: Path, *, denoise=False, reference=None):
    url, headers = _settings()
    if not url:
        raise ValueError('Optional audio models are not connected.')
    control = export_control.current()
    done = threading.Event()
    job_id = uuid.uuid4().hex
    if control:
        control.check()
        headers = dict(headers, **{'X-Audio-Job-Id': job_id})
        def monitor():
            with httpx.Client(timeout=2, follow_redirects=False) as client:
                while not done.wait(.5):
                    try:
                        if control.event.is_set():
                            client.post(url + f'/process/{job_id}/cancel', headers=headers)
                        else:
                            response = client.get(url + f'/process/{job_id}', headers=headers)
                            if response.is_success and hasattr(control, 'model_progress'):
                                control.model_progress(response.json()['progress'])
                    except (httpx.HTTPError, ValueError, KeyError):
                        continue
        watcher = threading.Thread(target=monitor, daemon=True, name='audio-export-progress')
        watcher.start()
    try:
        with source.open('rb') as audio:
            files = {'audio': ('source.wav', audio, 'audio/wav')}
            ref = reference.open('rb') if reference else None
            try:
                if ref:
                    files['reference'] = ('reference.wav', ref, 'audio/wav')
                with httpx.Client(timeout=httpx.Timeout(7200, connect=5), follow_redirects=False) as client:
                    with client.stream('POST', url + '/process', headers=headers, files=files,
                                       data={'denoise': str(bool(denoise)).lower()}) as response:
                        response.raise_for_status()
                        size = 0
                        with output.open('wb') as stream:
                            for chunk in response.iter_bytes():
                                size += len(chunk)
                                if size > MAX_AUDIO_BYTES:
                                    raise ValueError('Audio model output exceeds the supported size.')
                                stream.write(chunk)
                        if not size:
                            raise ValueError('The audio model returned an empty recording.')
                        try:
                            with wave.open(str(source), 'rb') as original, wave.open(str(output), 'rb') as converted:
                                if converted.getnchannels() != 1 or converted.getframerate() != 48000 or converted.getnframes() != original.getnframes():
                                    raise ValueError('Audio model output changed recording timing or format.')
                        except (wave.Error, EOFError) as exc:
                            raise ValueError('The audio model returned an invalid WAV recording.') from exc
            finally:
                if ref:
                    ref.close()
    except httpx.HTTPStatusError as exc:
        output.unlink(missing_ok=True)
        if control:
            control.check()
        if exc.response.status_code == 409:
            raise ValueError('The optional audio model service is busy. Wait for the current export and retry.') from exc
        if exc.response.status_code == 422:
            detail = None
            try:
                data = exc.response.read()
                if len(data) <= 16384:
                    detail = exc.response.json().get('detail')
            except (ValueError, AttributeError):
                pass
            if isinstance(detail, str) and len(detail) <= 500:
                raise ValueError(detail) from exc
        raise ValueError('Audio model processing failed. Check the optional model service and retry.') from exc
    except httpx.HTTPError as exc:
        output.unlink(missing_ok=True)
        if control:
            control.check()
        raise ValueError('Audio model processing failed or timed out. Check the optional model service and retry, or export with the original voice.') from exc
    except BaseException:
        output.unlink(missing_ok=True)
        raise
    finally:
        done.set()
        if control:
            watcher.join(timeout=3)
    if control:
        control.check()
