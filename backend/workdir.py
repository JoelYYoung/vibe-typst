"""Initialize the opened file's directory as a Claude working directory (VS Code-like):
write/merge a `.mcp.json` (the web-typst MCP server) and an auto-managed section in
`CLAUDE.md` instructing Claude to edit ONLY through our MCP tools (never Read/Edit/Write),
so human + Claude edits stay merged in the one live document.
"""
import json
import os
from pathlib import Path

import runtime

_HERE = Path(__file__).resolve().parent
_VENV_PY = _HERE / ".venv" / "bin" / "python"
_MCP_SERVER = _HERE / "mcp_server.py"

MCP_NAME = "web-typst"           # the MCP server name Claude sees
_OLD_MCP_NAMES = ("slide-comments",)  # migrate away from these old names

_BEGIN = "<!-- TYPST-COMMENT-BRIDGE:BEGIN (auto-managed — edits here will be overwritten) -->"
_END = "<!-- TYPST-COMMENT-BRIDGE:END -->"


def _section(main: str) -> str:
    return f"""{_BEGIN}
## Editing `{main}` (Typst Comment Bridge)

This `.typ` is open in the Typst Comment Bridge app as a **live shared document** — the
human edits it in the browser while you edit it. To keep both in sync and never clobber
each other's work:

- **Do NOT** use `Read` / `Edit` / `Write` / `MultiEdit` on this `.typ`. Writing the file
  directly bypasses the shared document and gets overwritten.
- **Read** it only with the MCP tools, and read WINDOWS, not the whole file:
  - `find_in_document("text")` → line numbers of matches (use this to locate an anchor).
  - `get_document(offset, limit)` → a line-numbered slice starting at `offset` (default a
    120-line head, capped at 400). Do not try to dump the whole deck at once.
- **Edit** it only with `replace_anchor`, `insert_before_anchor`, `insert_after_anchor`,
  `replace_range`. These are content-anchored (exact text snippets, never line numbers) and
  apply to the shared doc, so the human sees your change live. After editing, the preview
  shows a compile error if the source no longer parses — fix it before moving on.
- **Comments** (the human's edit requests): `get_pending_comments` → for each, read its
  `anchor_text` + `comment`, locate it via `find_in_document(anchor_text)`, apply the change
  with the edit tools above, recompile-check, then `mark_comment_done` (or
  `mark_comment_dismissed` if unclear/obsolete). Comments live in a separate store (the
  `web-typst` MCP), NOT in the `.typ`; you never edit them by hand.

### This is a TOUYING deck — always write touying
This `.typ` is a **touying** presentation (`#import "@preview/touying:0.6.1": *` + a theme via
`#show: …-theme.with(...)`). When you add, split, or restructure slides you MUST use touying
constructs — otherwise the live preview, the per-page transcript mapping, and `#speaker-note`
all break:
- A new slide is `#slide[ ... ]` (or `#centered-slide[...]`, `#focus-slide[...]`, `#title-slide[...]`).
- A multi-step / animated slide is `#slide(repeat: N, self => {{ let sub = self.subslide`
  `  ...; if sub >= k [ revealed content ] }})`.
- NEVER turn it into plain Typst — no bare `#pagebreak()` slideshows, no replacing the touying
  setup with `#set page(...)`. If the human asks for a brand-new deck, still scaffold it as a
  touying deck (import touying + a theme + `#slide[...]`).
Put every slide's narration in `#speaker-note(...)` (see below) so transcripts keep working.

### Speaker notes / transcripts (the per-slide & per-page "script")
The narration script lives in the `.typ` as touying `speaker-note(...)` calls (edit them with the
same anchor tools). They export to a `.pdfpc` file the human downloads for the pdfpc presenter.
- **Whole-slide note** — put one note inside the slide; it applies to every subslide:
  `#slide[ #speaker-note("spoken script for this slide") ... ]`
  (inside a `#slide(.., self => {{ ... }})` closure use `speaker-note("...")` with no `#`).
- **Per-subslide (per-page) note** — make it conditional on the subslide index, so each page gets
  its OWN transcript:
  `#slide(repeat: 3, self => {{ let sub = self.subslide`
  `  if sub == 1 {{ speaker-note("script for page 1") }}`
  `  if sub == 2 {{ speaker-note("script for page 2") }}`
  `  ... }})`
- To CHANGE a note, `find_in_document` its current text and `replace_anchor` it. To ADD one,
  `insert_after_anchor` on the slide's opening line. Keep notes plain spoken prose (they feed
  text-to-speech and the pdfpc presenter).

### How the tools load
- **MCP** (`web-typst` server): defined in `.mcp.json` and auto-enabled in
  `.claude/settings.local.json` (`enabledMcpjsonServers`), so `claude` run from this
  directory loads `get_document`, `find_in_document`, the edit tools, and the comment tools
  with no extra step. The web backend must be running (its URL is set in `.mcp.json`).
- **Skills / slash-commands**: none are defined for this project. If you add any, put
  Skills under `.claude/skills/<name>/SKILL.md` and commands under `.claude/commands/`;
  they load automatically from this working directory.
{_END}"""


def is_ready() -> bool:
    """True if the working dir already has our .mcp.json + CLAUDE.md section."""
    d = runtime.project_dir()
    mcp = d / ".mcp.json"
    cmd = d / "CLAUDE.md"
    has_mcp = False
    if mcp.exists():
        try:
            has_mcp = MCP_NAME in json.loads(mcp.read_text(encoding="utf-8")).get("mcpServers", {})
        except Exception:
            has_mcp = False
    has_cmd = cmd.exists() and _BEGIN in cmd.read_text(encoding="utf-8") if cmd.exists() else False
    return has_mcp and has_cmd


def setup(backend_port: int | None = None) -> dict:
    if backend_port is None:
        backend_port = int(os.environ.get("PORT", "8787"))
    d = runtime.project_dir()
    store = str(runtime.store_path())
    main = runtime.current_main()

    # --- .mcp.json (merge, don't clobber other servers) ---
    mcp_path = d / ".mcp.json"
    cfg: dict = {}
    if mcp_path.exists():
        try:
            cfg = json.loads(mcp_path.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
    cfg.setdefault("mcpServers", {})
    for old in _OLD_MCP_NAMES:        # migrate the old server name away
        cfg["mcpServers"].pop(old, None)
    cfg["mcpServers"][MCP_NAME] = {
        "command": str(_VENV_PY) if _VENV_PY.exists() else "python3",
        "args": [str(_MCP_SERVER)],
        "env": {
            "COMMENT_STORE_PATH": store,
            "TCB_BACKEND_URL": f"http://127.0.0.1:{backend_port}",
        },
    }
    try:
        mcp_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    except Exception:
        pass

    # --- .claude/settings.local.json: auto-enable the server (and drop the old name) ---
    try:
        sl_path = d / ".claude" / "settings.local.json"
        sl: dict = {}
        if sl_path.exists():
            try:
                sl = json.loads(sl_path.read_text(encoding="utf-8"))
            except Exception:
                sl = {}
        enabled = [s for s in sl.get("enabledMcpjsonServers", []) if s not in _OLD_MCP_NAMES]
        if MCP_NAME not in enabled:
            enabled.append(MCP_NAME)
        sl["enabledMcpjsonServers"] = enabled
        sl_path.parent.mkdir(parents=True, exist_ok=True)
        sl_path.write_text(json.dumps(sl, indent=2) + "\n", encoding="utf-8")
    except Exception:
        pass

    # --- CLAUDE.md (idempotent auto-managed section) ---
    section = _section(main)
    cmd_path = d / "CLAUDE.md"
    existing = cmd_path.read_text(encoding="utf-8") if cmd_path.exists() else ""
    if _BEGIN in existing and _END in existing:
        pre = existing.split(_BEGIN)[0]
        post = existing.split(_END, 1)[1]
        new = pre + section + post
    elif existing.strip():
        new = existing.rstrip() + "\n\n" + section + "\n"
    else:
        new = section + "\n"
    try:
        cmd_path.write_text(new, encoding="utf-8")
    except Exception:
        pass

    return {"mcp": str(mcp_path), "claude_md": str(cmd_path)}
