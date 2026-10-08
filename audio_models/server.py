"""Private model worker. Configure the workspace URL/token server-side only."""
import hmac
import json
import os
import shutil
import tempfile
import threading
import asyncio
import re
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
from starlette.datastructures import UploadFile
from starlette.requests import Request as StarletteRequest

MAX_AUDIO_BYTES = 384 * 1024 * 1024


class InferenceControl:
    def __init__(self):
        self.cancelled = threading.Event()
        self.progress = 0

    def check(self):
        if self.cancelled.is_set():
            raise ValueError('Audio processing cancelled.')

    def report(self, progress):
        self.check()
        self.progress = max(self.progress, min(99, int(progress)))


def create_app(engine, token: str, work_dir: Path):
    if len(token) < 32:
        raise ValueError('Configure a private audio-model token of at least 32 characters.')
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    slot = threading.BoundedSemaphore(1)
    jobs = {}
    jobs_lock = threading.Lock()

    @app.middleware('http')
    async def authorize(request, call_next):
        from fastapi.responses import JSONResponse
        if not hmac.compare_digest(request.headers.get('authorization', ''), 'Bearer ' + token):
            return JSONResponse({'detail': 'Unauthorized'}, status_code=401)
        return await call_next(request)

    @app.get('/health')
    def health(refresh: bool = False):
        if refresh and hasattr(engine, 'retry'):
            engine.retry()
        return dict(engine.status(), cancellable=True)

    @app.get('/process/{job_id}')
    def progress(job_id: str):
        with jobs_lock:
            control = jobs.get(job_id)
        if not control:
            raise HTTPException(404, 'Audio job not found.')
        return {'progress': control.progress}

    @app.post('/process/{job_id}/cancel')
    def cancel(job_id: str):
        with jobs_lock:
            control = jobs.get(job_id)
            if not control:
                raise HTTPException(404, 'Audio job not found.')
            control.cancelled.set()
        return {'cancelled': True}

    @app.post('/process')
    async def process(request: Request):
        job_id = request.headers.get('x-audio-job-id')
        if job_id and not re.fullmatch('[a-f0-9]{32}', job_id):
            raise HTTPException(400, 'Invalid audio job identity.')
        if not slot.acquire(blocking=False):
            raise HTTPException(409, 'Another audio model job is running. Retry after it finishes.')
        work = None
        control = InferenceControl()
        if job_id:
            with jobs_lock:
                jobs[job_id] = control
        try:
            work = Path(tempfile.mkdtemp(prefix='audio-', dir=work_dir))
            received = 0
            async def receive():
                nonlocal received
                message = await request.receive()
                received += len(message.get('body', b''))
                if received > MAX_AUDIO_BYTES + 3 * 1024 * 1024:
                    raise HTTPException(413, 'Audio is too large.')
                return message
            bounded = StarletteRequest(request.scope, receive=receive)
            async with bounded.form(max_files=2, max_fields=1) as form:
                denoise = form.get('denoise', 'false')
                if denoise not in ('true', 'false') or set(form) - {'denoise', 'audio', 'reference'} or len(form.multi_items()) != len(form):
                    raise HTTPException(400, 'Invalid processing request.')
                paths = {}
                for field, limit in (('audio', MAX_AUDIO_BYTES), ('reference', 3 * 1024 * 1024)):
                    upload = form.get(field)
                    if upload is None and field == 'reference':
                        continue
                    if not isinstance(upload, UploadFile):
                        raise HTTPException(400, 'Expected a WAV upload.')
                    path = work / (field + '.wav')
                    size = 0
                    with path.open('wb') as stream:
                        while chunk := await upload.read(1024 * 1024):
                            size += len(chunk)
                            if size > limit:
                                raise HTTPException(413, 'Audio is too large.')
                            stream.write(chunk)
                    paths[field] = path
            output = work / 'output.wav'
            kwargs = {'denoise': denoise == 'true', 'reference': paths.get('reference')}
            if job_id:
                kwargs['control'] = control
            inference = asyncio.create_task(asyncio.to_thread(engine.process, paths['audio'], output, **kwargs))
            try:
                await asyncio.shield(inference)
            except asyncio.CancelledError:
                # A dropped HTTP connection does not own the submitted computation.
                # Retain its files and slot until inference exits.
                try:
                    await inference
                finally:
                    raise
            control.check()
            # Release memory/GPU slot before the caller downloads the completed immutable file.
            response = FileResponse(output, media_type='audio/wav', background=BackgroundTask(shutil.rmtree, work, True))
            work = None
            return response
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        finally:
            if work:
                shutil.rmtree(work, ignore_errors=True)
            if job_id:
                with jobs_lock:
                    jobs.pop(job_id, None)
            slot.release()

    return app


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('config', type=Path)
    parser.add_argument('--prepare', action='store_true')
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    if args.prepare:
        from engine import Engine
        engine = Engine(config)
        print(json.dumps(engine.status()))
        return
    from model_runtime import ValidatedRuntime
    engine = ValidatedRuntime(config)
    engine.start()
    import uvicorn
    uvicorn.run(create_app(engine, config['token'], Path(config['work_dir'])),
                host=config.get('host', '127.0.0.1'), port=config.get('port', 8840))


if __name__ == '__main__':
    main()
