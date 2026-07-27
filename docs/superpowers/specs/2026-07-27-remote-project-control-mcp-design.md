# Remote Project-Control MCP Design

**Status:** Approved in conversation on 2026-07-27

**Target:** Vibe Typst control plane and per-user workspace containers

**Transport:** MCP Streamable HTTP over HTTPS

## Problem

Vibe Typst currently supports two ways to change a project:

1. a human uses the web application; or
2. an agent runs in the project's terminal and uses the project-local stdio MCP server.

A remote agent has its own working directory and cannot conveniently place a deck in Vibe
Typst, show the rendered result to a human, or receive and apply the human's slide comments.
The existing project-local MCP server cannot solve this because it starts inside one already
selected project and has no authenticated project-management capability.

The new feature is a higher-level remote MCP control surface. It authenticates an agent as an
existing Vibe Typst user, lets it create or select one of that user's projects, and then exposes
safe project, file, slide, transcript, and comment operations.

## Goals

- Give remote MCP clients one HTTPS endpoint for project-level Vibe Typst operations.
- Reuse the existing user, workspace, and container isolation model.
- Let a user generate, identify, expire, and revoke Personal Access Tokens (PATs).
- Let a remote agent create, list, select, open, and rename projects.
- Let the agent upload, read, write, move, delete, and restore ordinary project files.
- Preserve the existing CRDT path for Typst document edits.
- Preserve PDF validation, version capture, and managed-file protections.
- Let a remote agent inspect rendered pages and process Typst comments.
- Return an authenticated project URL that a human can open in the web application.
- Prevent stale project state from causing an operation to affect the wrong project.
- Keep the server-side project as the single source of truth; do not synchronize the agent's
  entire working directory.

## Non-goals

- No public or anonymous presentation links in the first version.
- No bidirectional working-directory synchronization.
- No remote shell, terminal, or arbitrary command-execution MCP tool.
- No MCP tool for deleting an entire project or emptying file trash.
- No automatic wake-up of an offline agent when a human adds a comment.
- No simultaneous active projects within one user's workspace container.
- No new PDF comment system. PDF projects continue to use per-page transcripts only.
- No replacement of the existing project-local stdio Typst and PDF MCP servers.

## Existing Constraints

- The control plane owns cookie authentication, users, sessions, container lifecycle, and the
  reverse proxy.
- Each user owns one workspace directory and one isolated container.
- Each container currently has one process-global active project, document store, resolver, and
  comment store.
- Typst source is a live CRDT document. Writing its file behind the backend would lose or
  overwrite concurrent human edits.
- A PDF project's `document.pdf`, `transcript.json`, metadata, locks, and version state are
  managed paths. Generic file tools must not mutate them.
- Existing file and project APIs already enforce most path-containment and PDF-state rules.

## Chosen Architecture

The control plane hosts a new MCP Streamable HTTP endpoint:

```text
Remote MCP client
    |
    | HTTPS + Authorization: Bearer <PAT>
    v
Control plane /mcp
    |-- authenticate PAT and enforce scopes
    |-- resolve the existing Vibe Typst user
    |-- start that user's workspace container when required
    |-- validate project handles
    |-- audit the call
    v
User workspace backend on a loopback-only port
    |-- existing project/file safety functions
    |-- Typst CRDT edits and resolver
    |-- PDF service and transcript service
    `-- comment store
```

The `/mcp` route is registered before the control plane's catch-all browser proxy. Workspace
container ports are bound to `127.0.0.1`, not all host interfaces. The PAT is consumed only by
the control plane and is never forwarded to the workspace container.

The first implementation uses the repository's locked MCP Python SDK 1.x API and pins the new
control-plane dependency to `mcp>=1.28,<2`. Moving to the SDK's incompatible 2.x API is a
separate migration, not part of this feature.

The first version deliberately retains one active project per user. It makes that state safe
with an explicit project handle and context version. A later per-project runtime refactor can
support concurrent projects without changing the external MCP tool signatures.

## Authentication and Token Management

### User relationship

A PAT is another credential for an existing Vibe Typst user, not a second kind of account. The
authenticated MCP client receives only that user's workspace. Account locking, account deletion,
or PAT revocation immediately prevents new MCP calls.

If a dedicated agent identity is needed, an administrator can create a normal Vibe Typst user
and generate a PAT for it.

### Token creation

The account UI gains a **Personal Access Tokens** section. A user supplies:

- a human-readable name, such as `remote-codex`;
- a permission preset;
- an optional expiry time.

The server generates a high-entropy token with a recognizable prefix. The complete token is
shown once. The database stores its identifier, a cryptographic hash, short display prefix,
owner, scopes, timestamps, and revocation state; it never stores the bearer secret.

Suggested token format:

```text
vbt_<public-id>_<random-256-bit-secret>
```

The account UI lists token name, prefix, preset, creation time, expiry, last-used time, and
revocation state. Revocation requires the user's normal cookie-authenticated account.

### Permission presets and scopes

The initial UI exposes two presets:

- **Viewer:** project metadata, file contents, slides, PDFs, transcripts, and comments are
  readable.
- **Editor:** Viewer plus project creation/rename, file mutations, Typst edits, PDF replacement,
  transcript edits, and comment status changes.

The database stores fine-grained scopes even though the first UI uses presets:

```text
projects:read
projects:write
files:read
files:write
slides:read
documents:write
transcripts:read
transcripts:write
comments:read
comments:write
```

There is no `projects:delete`, `terminal`, or `trash:purge` scope in the first version.

### MCP authentication behavior

Every `/mcp` request requires:

```http
Authorization: Bearer <PAT>
```

Cookie authentication is not accepted on this endpoint. Missing, invalid, expired, revoked, or
locked-account credentials produce an MCP authorization error. Successful use updates
`last_used_at` with write throttling so ordinary calls do not cause excessive database writes.

The first version uses explicitly configured bearer tokens rather than an interactive OAuth
authorization-code flow. This matches the intended machine configuration while keeping token
issuance and revocation under the existing authenticated web account.

## Project Selection and Leases

### Active context

The workspace backend exposes an opaque `context_version` alongside the active project's stable
ID. The version changes when:

- a different project is opened;
- the active project is closed;
- the workspace backend restarts.

Opening the already active project is idempotent and retains the current context version. This
allows a human and one or more MCP clients to collaborate on the same project. Typst revisions
and file hashes still detect content-level conflicts.

### Opening a project

`open_project(project_id)`:

1. authenticates the PAT and checks project scopes;
2. starts and waits for the user's container if necessary;
3. validates that `project_id` belongs to the user;
4. opens the project through its existing type-aware backend flow;
5. records an opaque project lease bound to user, token, project ID, and `context_version`;
6. returns a random `project_handle`, project metadata, capabilities, and authenticated web URL.

The project handle is not a substitute for the PAT. Both are required. Handles are stored as
hashes, expire after 12 hours without use, and are invalidated when their PAT is revoked.

### Validating calls

Every project-scoped tool receives `project_handle`. Before forwarding a call, the control plane
checks:

- the handle belongs to the authenticated user and PAT;
- the handle has not expired;
- the project still exists;
- the backend still reports the same project ID and context version.

If the human opens another project, all handles for the previous active context fail with:

```json
{
  "code": "PROJECT_CONTEXT_CHANGED",
  "message": "The active project changed. Call open_project again."
}
```

The gateway never silently reopens a stale handle's project because that could unexpectedly take
control away from the human.

## MCP Tool Surface

Tool results use structured content. Tools return stable machine codes in addition to concise
human-readable messages.

### Project tools

| Tool | Required scope | Behavior |
|---|---|---|
| `list_projects` | `projects:read` | List the authenticated user's projects and types. |
| `create_typst_project` | `projects:write` | Create a starter Typst project. |
| `begin_pdf_project_upload` | `projects:write` | Create a bounded one-time PDF upload session. |
| `finish_pdf_project_upload` | `projects:write` | Verify and atomically publish the uploaded PDF project. |
| `open_project` | `projects:read` | Open/select a project and return a handle and capabilities. |
| `get_project` | `projects:read` | Return metadata for the handled project. |
| `rename_project` | `projects:write` | Rename the handled project without changing its stable ID. |
| `close_project` | `projects:read` | Close the active project and invalidate its handles. |

There is no whole-project deletion tool.

### File tools

| Tool | Required scope | Behavior |
|---|---|---|
| `list_files` | `files:read` | List non-hidden ordinary files for the handled project. |
| `read_text_file` | `files:read` | Return a bounded, line-numbered text window and SHA-256. |
| `write_text_file` | `files:write` | Write ordinary text using `expected_sha256`. |
| `create_directory` | `files:write` | Create a path-contained directory. |
| `move_file` | `files:write` | Rename or move an ordinary file/directory. |
| `upload_file` | `files:write` | Upload a small inline file with an explicit overwrite policy. |
| `begin_file_upload` | `files:write` | Issue a one-time upload URL for a larger file. |
| `finish_file_upload` | `files:write` | Verify size/hash and atomically install a staged upload. |
| `delete_file` | `files:write` | Move an ordinary file/directory into recoverable trash. |
| `list_deleted_files` | `files:read` | List recoverable entries and original paths. |
| `restore_deleted_file` | `files:write` | Restore one entry, rejecting destination collisions. |

`read_text_file` is paged to avoid flooding an agent context. Binary downloads use a short-lived,
authenticated download URL. Inline MCP uploads are capped at a small limit; larger binaries use
the staged HTTP upload flow.

Generic file tools cannot write the active Typst main document or any PDF-managed path.

### Typst tools

The remote tools adapt the existing project-local MCP behavior rather than reimplementing it:

| Tool | Required scope |
|---|---|
| `get_document` | `files:read` |
| `find_in_document` | `files:read` |
| `locate` | `slides:read` |
| `apply_edits` | `documents:write` |
| `get_transcripts` | `transcripts:read` |
| `get_pending_comments` | `comments:read` |
| `get_comment` | `comments:read` |
| `mark_comment_done` | `comments:write` |
| `mark_comment_dismissed` | `comments:write` |
| `get_slide_preview` | `slides:read` |
| `export_pdf` | `slides:read` |

`apply_edits` is the only general way to mutate the live main Typst document. It routes through
the CRDT backend, broadcasts to the browser, persists to disk, and honors `base_rev`.

`get_slide_preview` returns MCP image content for one rendered page. `export_pdf` creates a
short-lived authenticated download URL rather than embedding a large PDF in a tool response.

### PDF tools

| Tool | Required scope |
|---|---|
| `get_pdf_info` | `projects:read` |
| `get_pdf_text` | `files:read` |
| `get_transcripts` | `transcripts:read` |
| `set_transcript` | `transcripts:write` |
| `set_transcripts` | `transcripts:write` |
| `get_page_preview` | `slides:read` |
| `begin_pdf_replacement` | `documents:write` |
| `finish_pdf_replacement` | `documents:write` |

PDF replacement stages a complete candidate, verifies its declared size and SHA-256, delegates
validation and version capture to the existing PDF service, and only then installs it as
`document.pdf`. Generic uploads and writes cannot replace the primary PDF.

PDF projects do not expose comment tools. Transcript tools retain the existing simple
page-number mapping.

### Human-facing URL

`open_project` returns an HTTPS URL containing the stable project ID. After normal web login, the
SPA opens that project directly. The URL does not contain the PAT or project handle and grants no
anonymous access.

## File Transfer

### Small inline uploads

`upload_file` accepts bounded base64 content, destination path, overwrite flag, byte length, and
SHA-256. It is intended for small generated sources or assets and is limited to 1 MiB of decoded
content.

### Large staged uploads

Large files use a three-step process:

1. a `begin_*_upload` tool validates metadata and returns an `upload_id`, one-time URL, required
   headers, expiry, and maximum size;
2. the remote agent performs an HTTP `PUT` from its own working directory;
3. the matching `finish_*_upload` tool verifies byte length and SHA-256, then delegates an atomic
   install to the type-aware backend operation.

An upload session is bound to user, PAT, project or project-creation intent, destination, size,
hash, and a 15-minute expiry. It cannot be redirected to another path. Generic staged files and
PDFs are limited to 100 MiB in the first version, matching the existing PDF ingress ceiling.
Partial and expired staging files are periodically removed. A failed finish never changes the
visible project. Download URLs expire after five minutes.

## File Trash

Deleted ordinary files move outside the visible project tree to:

```text
/workspace/.tcb/trash/<project-id>/<trash-id>/
```

Each entry stores the payload plus metadata containing original relative path, deletion time,
token ID, and object type. This prevents trash from appearing in project file lists or dirtying
the project's Git history.

Entries expire after 30 days. Expiry cleanup is server-controlled. MCP can list and restore
entries but cannot purge them. Restore refuses to overwrite an existing destination.

PDF-managed paths and the active Typst main file are never eligible for generic trash operations.

## Concurrency and Conflict Handling

- Typst edits use CRDT operations and `base_rev`.
- Ordinary text writes require the SHA-256 observed by the reader. A mismatch returns the current
  hash without writing.
- Uploads default to no overwrite. Explicit overwrite still checks the expected destination hash
  when the destination already exists.
- Project handles prevent calls from crossing an active-project change.
- Staged installs use same-filesystem atomic replacement after validation.
- Comment completion remains an explicit agent action.
- The MCP server does not automatically wake or resume an offline agent. An active agent checks
  `get_pending_comments` at useful work checkpoints.

## Audit and Operational Controls

Every mutating MCP call records:

- user ID;
- PAT ID, never the bearer secret;
- MCP tool name;
- project ID;
- affected relative paths or comment IDs;
- start time, completion time, outcome, and stable error code;
- request correlation ID.

Audit records never contain file bodies, transcript text, comment bodies, or tokens. The default
retention is 90 days.

The control plane initially permits 120 MCP calls per minute per token, at most four concurrent
tool calls, and at most two live upload sessions per token. Upload limits remain at least as
strict as the current PDF and multipart limits. Rate-limited calls return a retry delay.
Sensitive token and upload endpoints set `Cache-Control: no-store`.

## Error Model

Expected failures return structured MCP tool errors with one of these stable codes:

```text
AUTH_REQUIRED
TOKEN_INVALID
TOKEN_EXPIRED
TOKEN_REVOKED
ACCOUNT_LOCKED
SCOPE_DENIED
WORKSPACE_STARTING
WORKSPACE_UNAVAILABLE
PROJECT_NOT_FOUND
PROJECT_CONTEXT_CHANGED
PROJECT_HANDLE_EXPIRED
REVISION_CONFLICT
PATH_NOT_ALLOWED
FILE_NOT_FOUND
DESTINATION_EXISTS
FILE_TOO_LARGE
CHECKSUM_MISMATCH
UPLOAD_EXPIRED
CAPABILITY_NOT_AVAILABLE
BACKEND_ERROR
```

Retryable errors state that explicitly and include a recommended retry delay when useful. Errors
do not reveal other users' project IDs, paths, tokens, or container information.

## Persistence

The control database gains:

- `api_tokens` for PAT metadata and hashes;
- `project_leases` for opaque handle hashes and active-context bindings;
- `upload_sessions` for staged-transfer state;
- `mcp_audit_log` for sanitized operation records.

Database initialization uses additive, idempotent migrations consistent with the existing control
plane. Workspace file trash and staged payloads remain on the user's mounted workspace so a
container replacement does not lose them.

## Testing Strategy

### Unit tests

- PAT generation, hashing, expiry, revocation, and preset scopes.
- Account lock and delete effects on PATs and handles.
- Project-handle ownership, expiry, same-project idempotency, context changes, and backend restart.
- Tool-to-scope mapping.
- Structured error conversion and secret redaction.
- Upload size/hash/expiry validation.
- Trash metadata, collision-safe restore, and expiry.

### Backend safety tests

- Path traversal and symlink escape attempts.
- Typst main-file generic write/upload/delete rejection.
- PDF primary and managed-state generic mutation rejection.
- Atomic upload visibility and interrupted upload cleanup.
- `expected_sha256` and CRDT `base_rev` conflicts.

### Integration tests

- A real Streamable HTTP MCP client authenticates, lists projects, opens one, and calls tools.
- A stopped workspace starts through an MCP call and becomes usable.
- Two users cannot observe or mutate each other's projects, leases, uploads, or trash.
- Browser project switching invalidates an MCP handle.
- Browser and MCP edit the same Typst project through the shared CRDT.
- Human-created Typst comments are fetched and marked complete by MCP.
- PDF transcript and replacement operations preserve existing safety guarantees.
- PAT revocation takes effect on the next request.

### Regression tests

Existing browser, terminal, project-local Typst MCP, project-local PDF MCP, project management,
file management, comments, preview/presentation, PDF workspace, and deployment tests continue to
pass.

## Deployment

1. Apply the additive control-database migration.
2. Deploy workspace backend support for `context_version`, staged operations, trash, and preview
   adapters.
3. Deploy the control-plane MCP endpoint and PAT account APIs.
4. Deploy the account-token UI and direct authenticated project URL.
5. Bind newly created workspace ports to loopback only.
6. Verify MCP protocol behavior locally before exposing `/mcp` through the existing Cloudflare
   HTTPS tunnel.

Existing cookie sessions, projects, workspace mounts, and project-local MCP configuration do not
change. Existing containers may be hot-updated for code, but environment or port-binding changes
require controlled recreation with the same workspace mount after data-hash verification.

## Future Extensions

The external tool signatures deliberately require `project_handle`, so the implementation can
later replace the single active context with true per-project runtimes. Other future additions
may include interactive OAuth, project-scoped share tokens, public read-only presentations,
server-to-agent event delivery where clients support it, and whole-project deletion with a
separate high-risk approval flow.
