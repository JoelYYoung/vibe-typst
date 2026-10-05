"""Isolated real recording API + deterministic two-page decks for browser smoke tests.

Run with backend/.venv/bin/python tests/recording_e2e_server.py; no user project is touched.
"""
import hashlib
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

import fitz
import uvicorn
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi import Body
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "backend"))
_temporary = tempfile.TemporaryDirectory(prefix="vibe-recording-e2e-")
ROOT = Path(_temporary.name).resolve()
os.environ["RENDER_DIR"] = str(ROOT / "renders")
import app as workspace
import recording_routes
import docstore
import resolver
import time
docstore.CACHE_DIR = ROOT / "crdt"
docstore.CACHE_DIR.mkdir()

projects = {}
for project_id, kind in (("recording-typst", "typst"), ("recording-pdf", "pdf")):
    project = ROOT / project_id
    project.mkdir()
    main = project / ("document.pdf" if kind == "pdf" else "main.typ")
    if kind == "pdf":
        doc = fitz.open()
        for color in ((.95, .9, .7), (.7, .85, .98)):
            page = doc.new_page(width=640, height=360)
            page.draw_rect(page.rect, color=color, fill=color)
        doc.save(main)
        doc.close()
    else:
        main.write_text('#import "@preview/touying:0.6.1": *\n#import themes.simple: *\n#show: simple-theme.with(aspect-ratio: "16-9")\n#slide[\nFirst page\n]\n#slide[\nSecond page\n]\n')
    info = {"id": project_id, "name": f"Recording {kind}", "path": str(project), "type": kind, "main_file": main.name}
    projects[project_id] = info
    render = workspace.runtime.render_dir(main)
    render.mkdir(parents=True)
    if kind == "pdf":
        workspace.pdf_service.render_pdf(main, render)
        workspace._record_pdf_render_version(workspace._pdf_pages(main), main, workspace._pdf_identity(main, info))
    else:
        resolver.start(main)
        deadline = time.monotonic() + 25
        while resolver.status(main)["pages"] != 2:
            if time.monotonic() > deadline:
                raise RuntimeError("fixture Typst renderer did not start")
            time.sleep(.05)

workspace.projects_mod.get_project = lambda project_id: projects[project_id]
app = FastAPI()
app.include_router(recording_routes.router(workspace._recording_target, workspace._prepare_recording))


def info_for(project_id):
    return projects[project_id or "recording-typst"]


def rendered(project_id):
    info = info_for(project_id)
    main = Path(info["path"]) / info["main_file"]
    if info["type"] == "pdf":
        # Exercise real PDF generation tokens (not content hashes).
        return {"pages": workspace._pdf_pages(main), "tokens": {}, "version": 1, "generation": workspace._pdf_render_generation(main, workspace._pdf_identity(main, info))}
    names = workspace.typst_service.list_pages(main)
    return {"pages": names, "tokens": workspace.typst_service.page_tokens(main), "version": resolver.status(main)["version"]}


@app.get("/api/app/state")
def app_state():
    return {"configured": True, "mode": "local"}


@app.post("/api/projects/{project_id}/open")
def open_project(project_id: str):
    return {"project": info_for(project_id)}


@app.get("/api/state")
def state(project_id: str | None = None):
    info = info_for(project_id)
    return {**rendered(project_id), "project": info["path"], "project_name": info["name"], "file": str(Path(info["path"]) / info["main_file"]),
            "main": info["main_file"], "room": None, "workdir_ready": True, "mode": "local", "project_type": info["type"], "page_count": 2}


@app.get("/api/render-version")
def version(project_id: str | None = None):
    return rendered(project_id)


@app.get("/api/slide-map")
def slide_map(project_id: str | None = None):
    kind = info_for(project_id)["type"]
    return {"pages": [{"page": page, "project_type": kind, "slide_line": page, "section": f"Test page {page}", "note": "Private speaker notes\n" * 60} for page in (1, 2)],
            "orphans": [], "generation": rendered(project_id).get("generation")}


@app.get("/api/render/{name}")
def render(name: str, project_id: str | None = None):
    info = info_for(project_id)
    if name not in rendered(project_id)["pages"]:
        raise HTTPException(404)
    return FileResponse(workspace.runtime.render_dir(Path(info["path"]) / info["main_file"]) / name)


@app.get("/api/comments")
def comments():
    return []


@app.websocket("/pty")
async def terminal(websocket: WebSocket):
    # The PDF workspace mounts its existing terminal; no shell is launched by this fixture.
    await websocket.accept()
    try:
        while True:
            await websocket.receive()
    except (WebSocketDisconnect, RuntimeError):
        pass


@app.get("/api/projects")
def project_list():
    return {"projects": list(projects.values())}


@app.get("/test/takes")
def debug_takes(project_id: str):
    import presentation_recording as recording
    root, slides = workspace._recording_target(project_id)
    takes = []
    for page in range(1, len(slides) + 1):
        data = recording._read_take(root, page, slides[page - 1].get("recording_id"))
        if data:
            takes.append({**data, "page": page, "sha256": hashlib.sha256(recording.media_path(root, data).read_bytes()).hexdigest()})
    return takes


@app.post('/test/typst/order')
async def reorder_slides(order: list[int] = Body()):
    main = Path(projects['recording-typst']['path']) / 'main.typ'
    source = docstore.get_text(main) or main.read_text()
    starts = [offset for offset, _ in workspace.typst_recording._openers(source)]
    slides = [source[start:end] for start, end in zip(starts, starts[1:] + [len(source)])]
    if not order or len(set(order)) != len(order) or any(not 1 <= value <= len(slides) for value in order):
        raise HTTPException(400)
    updated = source[:starts[0]] + ''.join(slides[value - 1] for value in order)
    result = await docstore.replace_anchor(source, updated, main)
    if not result.get('ok'):
        raise HTTPException(409)
    await docstore.flush_now(main)
    for _ in range(100):
        snapshot = workspace.runtime.render_dir(main) / 'render-source.json'
        if snapshot.exists() and json.loads(snapshot.read_text()).get('source') == updated:
            return {'ok': True}
        await asyncio.sleep(.05)
    raise HTTPException(500, 'test compiler did not catch up')


@app.get("/api/{remaining:path}")
def other(remaining: str):
    return {}


app.mount("/", StaticFiles(directory=REPO / "frontend" / "dist", html=True))

if __name__ == "__main__":
    try:
        uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("RECORDING_E2E_PORT", "9017")), log_level="warning")
    finally:
        _temporary.cleanup()
