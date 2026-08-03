# Remote MCP Server Instructions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish the project workflow in the MCP initialization response and deploy it with a control-only restart.

**Architecture:** Define one static `REMOTE_MCP_INSTRUCTIONS` contract beside the remote MCP server and pass it to `FastMCP`. Verify the contract through the official MCP client, then deploy the host control process without touching workspace containers.

**Tech Stack:** Python 3.11, FastMCP/MCP Streamable HTTP, `unittest`, macOS LaunchDaemon.

## Global Constraints

- The authoritative Typst target is the current active shared `.typ`, normally `main.typ` after `open_project`.
- Live Typst edits use `get_document`/`find_in_document` plus `apply_edits`; generic writes may
  manage only non-Typst assets and may not replace the active document.
- All Typst presentation source must remain in `main.typ`; auxiliary `.typ` files and local
  `.typ` imports/includes are forbidden.
- Typst comments are read, applied, preview-verified, then marked done.
- PDF projects have transcripts but no comment workflow.
- Initialization and verification must not open or start a workspace.
- Deployment restarts only the control plane.

---

### Task 1: Publish and test initialization instructions

**Files:**
- Modify: `control/remote_mcp.py`
- Modify: `tests/test_control_mcp_foundation.py`

**Interfaces:**
- Consumes: `FastMCP(name, instructions=...)` and the existing official MCP client fixture.
- Produces: `REMOTE_MCP_INSTRUCTIONS: str`, returned as `InitializeResult.instructions`.

- [ ] **Step 1: Write the failing protocol test**

Import `REMOTE_MCP_INSTRUCTIONS` with the existing remote MCP imports. In
`RemoteMcpProtocolTest.test_official_client_exercises_project_tools_handles_and_scopes`, capture
the initialization result and verify both exact transmission and required workflow language:

```python
initialized = await session.initialize()
self.assertEqual(
    initialized.instructions,
    REMOTE_MCP_INSTRUCTIONS,
)
for required in (
    "MUST be main.typ",
    "ALL Typst presentation source in main.typ",
    "NEVER create, upload, generate, import, include",
    "Every presentation MUST remain in Touying form",
    "apply_edits",
    "get_slide_preview",
    "get_pending_comments",
    "mark_comment_done",
    "PDF projects",
    "open_project again",
):
    self.assertIn(required, initialized.instructions)
```

- [ ] **Step 2: Run the test and confirm the red state**

Run:

```bash
PYTHONPATH=tests control/.venv/bin/python -m unittest -v \
  test_control_mcp_foundation.RemoteMcpProtocolTest.test_official_client_exercises_project_tools_handles_and_scopes
```

Expected: import failure because `REMOTE_MCP_INSTRUCTIONS` does not exist.

- [ ] **Step 3: Add the minimal server instruction contract**

Define `REMOTE_MCP_INSTRUCTIONS` in `control/remote_mcp.py` as a static multiline string that
implements every global constraint. Pass it into the existing constructor:

```python
server = FastMCP(
    "vibe-typst-projects",
    instructions=REMOTE_MCP_INSTRUCTIONS,
    token_verifier=PatTokenVerifier(db_path),
    # existing arguments remain unchanged
)
```

Do not add a guide tool/resource or change tool parameters or permissions. Keep the single-file
contract in the initialization instructions and reject generic `.typ` mutations with one shared
remote-service path guard.

- [ ] **Step 4: Run the targeted and complete control tests**

Run:

```bash
PYTHONPATH=tests control/.venv/bin/python -m unittest -v \
  test_control_mcp_foundation.RemoteMcpProtocolTest.test_official_client_exercises_project_tools_handles_and_scopes
```

Expected: one passing test.

Then run all `test_control_*.py` files with an isolated `CONTROL_DATA` directory using the
project's existing control virtual environment. Expected: all tests pass with zero failures and
zero errors.

- [ ] **Step 5: Commit**

```bash
git add control/remote_mcp.py tests/test_control_mcp_foundation.py
git commit -m "feat: teach remote agents the project workflow"
```

### Task 2: Merge, deploy, and verify the public MCP

**Files:**
- No source changes.
- Runtime reads: `control/data/control.db`, `control/data/public-base-url`.

**Interfaces:**
- Consumes: `REMOTE_MCP_INSTRUCTIONS` from Task 1 and the existing LaunchDaemon.
- Produces: a public MCP initialization result containing the instructions.

- [ ] **Step 1: Verify the merged result and push `main`**

Fast-forward `main`, rerun the complete control suite, run `git diff --check`, push, and verify
local and remote `main` SHAs are identical.

- [ ] **Step 2: Record data and process baselines**

Record control PID, logical users/sessions/token counts, session-secret hash, workspace container
states/mounts, and the aggregate workspace file count, byte count, and SHA-256.

- [ ] **Step 3: Restart only the control plane**

Send `SIGTERM` to the exact PID reported by
`launchctl print system/com.vibe-typst.control.daemon`. Poll `/login` until the LaunchDaemon
reports a new healthy PID. Do not start, stop, or restart any workspace container.

- [ ] **Step 4: Verify authenticated public initialization**

Create a five-minute Viewer token for an unlocked account, connect the official MCP client to
`https://vibetypst.yjwspace.win/mcp`, call only `initialize` and `tools/list`, and assert:

```python
initialized.instructions == REMOTE_MCP_INSTRUCTIONS
{"get_pending_comments", "get_comment", "mark_comment_done"} <= tool_names
```

Delete only that exact temporary token in `finally`. Do not call a project tool.

- [ ] **Step 5: Verify state preservation and clean up**

Confirm no temporary token remains, logical DB counts and session-secret hash are unchanged,
workspace container states/mounts and aggregate hash match the baseline, and only the control PID
changed. Remove the merged worktree and branch according to the finishing workflow.
