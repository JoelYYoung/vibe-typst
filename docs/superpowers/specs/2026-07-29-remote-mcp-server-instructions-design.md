# Remote MCP Server Instructions Design

## Goal

Make a newly connected AI understand the Vibe Typst project workflow from the MCP
initialization response, without requiring repository access or a separate guide tool.

## Approach

Pass one concise, static instruction block to `FastMCP(instructions=...)`. MCP clients receive
this text during `initialize`, before choosing a tool. Keep detailed parameter rules in the
existing tool descriptions and use the server instructions only for cross-tool workflow and
project invariants.

Alternatives rejected:

- A guide resource or guide tool is less reliable because the agent must decide to open it.
- Repeating the workflow in all 37 tool descriptions creates drift and makes the tool list noisy.

## Instruction Contract

The server instructions must tell the agent to:

1. Call `list_projects`, then `open_project`, and use the returned `project_handle`.
2. Treat the active shared Typst document reported by `get_document.file` as authoritative.
   Opening a project selects its primary document, normally `main.typ`; if a human changes the
   active project/file context and the handle is rejected, call `open_project` again.
3. Read the active document with `get_document` or `find_in_document` and edit it only with
   `apply_edits`. Never use generic file writes to bypass the live CRDT document.
4. Require every part of the Typst presentation to remain in `main.typ`, including themes,
   components, helpers, slides, and inline speaker notes. Forbid auxiliary `.typ` files and local
   `.typ` imports/includes; ordinary mutation tools reject `.typ` paths and are only for
   non-Typst assets.
5. Require Typst presentations to use Touying, preserve speaker transcripts inline in `main.typ`,
   and verify rendered output with preview tools after meaningful changes.
6. For Typst comments, read pending comments, apply and verify the requested change, then call
   `mark_comment_done`; dismiss only when the request is unclear, obsolete, or already resolved.
7. Keep PDF behavior separate: PDF projects use page previews and per-page transcripts, expose
   no comment workflow, and replace the managed PDF only through the staged replacement tools.
8. Treat context and revision conflicts as a signal to re-read or reopen, never to force an
   overwrite.

## Data and Security

The instructions are static and contain no tokens, usernames, project paths, or user content.
They do not change authorization, scopes, project handles, or workspace lifecycle. A separate
remote-service path guard rejects ordinary `.typ` writes, uploads, moves, staged installs, and
restores for Typst projects. Remote CRDT edits validate the complete post-edit source and reject
local `.typ` imports/includes before mutation. MCP initialization and `tools/list` must not open
or start a workspace.

## Verification and Deployment

- A protocol-level test must initialize through the official MCP client and assert the returned
  instructions contain the main-only Typst, Touying, `apply_edits`, preview, comment, PDF, and
  reopen guidance.
- An operations test must prove generic Typst-project writes, uploads, moves, and restores reject
  auxiliary `.typ` paths with `PATH_NOT_ALLOWED`.
- A document-store test must prove local `.typ` imports/includes are rejected atomically while
  Touying package imports and consolidation edits remain allowed.
- The complete control-plane test suite must remain green.
- Deploy by restarting only the control plane after merging and pushing.
- Verify the public authenticated MCP initialization returns the instructions and comment tools
  without calling any project tool.
- Confirm workspace container states, mounts, and aggregate workspace hash remain unchanged.
