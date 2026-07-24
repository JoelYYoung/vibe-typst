"""MCP tools for the immutable PDF project workflow.

The running web backend owns PDF validation, rendered pages, transcript sidecars, and
version capture.  This process only forwards tool calls to that backend.
"""
import json
import os
import urllib.error
import urllib.parse
import urllib.request

from mcp.server.fastmcp import FastMCP


mcp = FastMCP("vibe-typst")
BACKEND = os.environ.get("TCB_BACKEND_URL", "http://127.0.0.1:8787").rstrip("/")


def _backend(method: str, path: str, payload: dict | None = None) -> dict:
    """Call the active backend and return a usable error object on failure."""
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        f"{BACKEND}{path}", data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        return {"ok": False, "error": f"backend {exc.code}: {exc.read().decode()[:300]}"}
    except urllib.error.URLError as exc:
        return {
            "ok": False,
            "error": f"PDF backend unreachable at {BACKEND} ({exc.reason}). Is the server running?",
        }


@mcp.tool()
def get_pdf_info() -> dict:
    """Return the active PDF project's identity, rendered pages, and current version."""
    return _backend("GET", "/api/state")


@mcp.tool()
def get_pdf_text(page: int) -> dict:
    """Return embedded text for one 1-based PDF page. No OCR is performed."""
    return _backend("GET", "/api/pdf/text?" + urllib.parse.urlencode({"page": page}))


@mcp.tool()
def get_transcripts() -> dict:
    """Return the page-number transcript sidecar, including any orphaned entries."""
    return _backend("GET", "/api/pdf/transcripts")


@mcp.tool()
def set_transcript(page: int, text: str) -> dict:
    """Set narration text for one 1-based PDF page."""
    encoded_page = urllib.parse.quote(str(page), safe="")
    return _backend("PATCH", f"/api/pdf/transcripts/{encoded_page}", {"text": text})


@mcp.tool()
def set_transcripts(updates: list[dict]) -> dict:
    """Atomically set multiple transcript entries: [{"page": 1, "text": "..."}]."""
    return _backend("POST", "/api/pdf/transcripts/batch", {"updates": updates})


@mcp.tool()
def list_orphan_transcripts() -> dict:
    """List transcript entries left behind when a replacement PDF lost pages."""
    result = _backend("GET", "/api/pdf/transcripts")
    if result.get("ok") is False:
        return result
    return {"orphans": result.get("orphans", {})}


@mcp.tool()
def restore_orphan_transcript(orphan_page: str, target_page: int) -> dict:
    """Move an orphan transcript to a current 1-based target page without discarding text."""
    return _backend(
        "POST", "/api/pdf/transcripts/restore",
        {"orphan_page": orphan_page, "target_page": target_page},
    )


@mcp.tool()
def replace_pdf(candidate: str, message: str = "") -> dict:
    """Install a separately generated candidate PDF through validation and version capture.

    Create the candidate with native PDF utilities, then call this tool. Never overwrite
    ``document.pdf`` directly: that bypasses validation and versioning.
    """
    return _backend("POST", "/api/pdf/replace", {"candidate": candidate, "message": message})


if __name__ == "__main__":
    mcp.run()
