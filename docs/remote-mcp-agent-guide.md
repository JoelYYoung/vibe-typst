# Vibe Typst Remote MCP Agent Setup and Operating Contract

Give this entire document to the AI agent that will configure and use Vibe Typst. The agent must
configure the remote MCP connection, create a reusable skill from the template below when its
platform supports skills, and follow the operating contract for every Typst presentation task.

## Non-negotiable presentation contract

These rules override preferences for multi-file project organization:

1. `main.typ` is the only permitted Typst source file and the only authoring entry point.
2. Keep **all** presentation source in `main.typ`: Touying imports and setup, themes, styles,
   helper functions, reusable components, slide content, references, and speaker notes.
3. Never create, upload, generate, or edit auxiliary `.typ` files. Never split a deck into slide,
   theme, component, or section `.typ` files.
4. Never add a local `.typ` dependency through `#include` or `#import`. Package imports such as
   `#import "@preview/touying:..."` are allowed and do not violate the single-file rule.
5. Every presentation must use Touying. Do not replace Touying with a plain Typst page/document
   layout. Preserve the deck's working Touying version and theme unless the task requires a
   change.
6. Keep Touying speaker notes/transcripts inline in `main.typ`.
7. Use remote MCP `get_document` or `find_in_document` to read `main.typ`, and use `apply_edits`
   with the latest available `rev` to change it. Never replace `main.typ` with a generic file
   write or upload. The remote edit policy validates the complete post-edit document and rejects
   local `.typ` imports/includes before committing the CRDT transaction.
8. Generic file tools are only for non-Typst assets such as images, fonts, CSV, JSON, or other
   data files. The remote MCP rejects ordinary write, upload, move, staged-install, or restore
   operations targeting `.typ` paths in a Typst project.

If an existing project uses auxiliary `.typ` files, read them only to consolidate the required
source into `main.typ`. Remove every local `.typ` import/include, verify the consolidated deck,
then call `delete_file` for obsolete sidecar `.typ` files so they enter the recoverable trash. Do
not report completion while the rendered presentation still depends on any `.typ` file other than
`main.typ`.

## Configure the remote MCP connection

Use MCP Streamable HTTP:

- Endpoint: `https://vibetypst.yjwspace.win/mcp`
- Authentication: `Authorization: Bearer <VIBE_TYPST_TOKEN>`
- Suggested connector name: `vibe-typst`

Store the token in the agent platform's secret store or environment-variable facility. Never put
it in a prompt, `SKILL.md`, project file, repository, URL, console transcript, or ordinary log.
Use `VIBE_TYPST_TOKEN` as the secret variable name where the client supports environment
expansion.

Translate this client-neutral example into the platform's native MCP configuration format:

```json
{
  "type": "streamable-http",
  "url": "https://vibetypst.yjwspace.win/mcp",
  "headers": {
    "Authorization": "Bearer ${VIBE_TYPST_TOKEN}"
  }
}
```

After configuring it:

1. Initialize the connection and inspect the server instructions.
2. Confirm the instructions say that all Typst source must be in `main.typ` and that every
   presentation must remain in Touying form. If those instructions are absent, retain this
   document as the authoritative contract and warn the user that the server may be outdated.
3. Run MCP tool discovery. Do not assume the tool list is identical across deployments.
4. Call `list_projects` to verify authentication without changing a project.

Do not claim that the connection works until initialization, tool discovery, and `list_projects`
all succeed.

## Create the reusable skill

If the agent platform supports reusable skills, create a skill folder named
`vibe-typst-remote` in the platform's normal user skill directory. At minimum it must contain the
following `SKILL.md`. Do not add the bearer token or a project handle to the skill.

```markdown
---
name: vibe-typst-remote
description: Operate Vibe Typst projects through the authenticated remote MCP. Use for creating, opening, editing, reviewing, previewing, commenting on, or exporting Vibe Typst presentations and for remote Vibe Typst PDF-project tasks. Enforce a Touying-only, single-file main.typ workflow for every Typst presentation.
---

# Vibe Typst remote workflow

Use the `vibe-typst` remote MCP connector and discover its current tools before work.

## Hard Typst invariants

- Keep every piece of Typst presentation source in `main.typ`, including Touying setup, theme,
  styles, helpers, components, slides, references, and inline speaker notes.
- Never create, upload, generate, edit, import, include, or depend on an auxiliary `.typ` file.
- Allow package imports such as `@preview/touying`; forbid local `.typ` imports/includes.
- Keep every presentation in Touying form.
- Read with `get_document`/`find_in_document`; edit only with `apply_edits` and the latest `rev`.
  Every edit is `{"selector": {"by": "anchor", "text": "<exact snippet>"}, "text": "<replacement>"}`
  — `{"by": "lines", ...}` and `{"by": "range", ...}` are the other selectors, and there is no
  `old_text`/`new_text` form.
- A human may be editing the same document in the browser at the same time. Both writers are
  merged, so prefer anchor selectors over line or range numbers and add `expect` when a span must
  not have changed under you.
- Use generic file tools only for non-Typst assets.

When a deck already has local `.typ` dependencies, consolidate them into `main.typ`, remove the
dependencies, verify the rendered result, and call `delete_file` to recoverably trash the obsolete
`.typ` sidecars.

## Project workflow

1. Call `list_projects`, create a Typst project only when requested, then call `open_project`.
2. Keep the returned opaque `project_handle` and pass it to project-scoped tools. Share only the
   returned authenticated `web_url` with the human; never share the handle or bearer token.
3. Read enough of `main.typ` to understand the existing Touying structure before editing.
4. Apply the smallest coherent edit batch with the latest revision.
5. Inspect every affected rendered page. Fix compilation, overflow, clipping, contrast, and
   layout problems before completion.
6. For pending comments, read the live location, make the change, verify the preview, and only
   then mark the comment done. Dismiss only if unclear, obsolete, or already resolved.
7. Export only when requested and report the project `web_url` for human review.

On `EDIT_REJECTED`, the edit itself was wrong: read `error.details` for which edit failed and the
live text around it, then fix the edit — re-sending it unchanged fails again. On
`REVISION_CONFLICT`, the document really moved: re-read `main.typ` and re-aim the edit. On
`PROJECT_CONTEXT_CHANGED` or `PROJECT_HANDLE_EXPIRED`, call `open_project` again and re-read.
Never force or blindly retry an overwrite.

PDF projects are separate: use PDF information, text, preview, transcript, and staged replacement
tools. They have no Typst comment workflow; never overwrite `document.pdf` with generic tools.
```

For Codex-compatible skills, keep the YAML frontmatter limited to `name` and `description`, and
validate the finished skill with the platform's skill validator when available. Platform-specific
UI metadata may be generated separately; it must not weaken or duplicate the hard invariants.

## Operate a Typst project

1. Call `list_projects`.
2. Select the requested project, or call `create_typst_project` only when a new project was
   requested.
3. Call `open_project(project_id)` and retain `project_handle` for the current context.
4. Check that the project type is `typst` and that `get_document.file` is `main.typ`. Stop and
   report a server/project configuration problem if the active Typst document is not `main.typ`.
5. Read `main.typ` in bounded windows with `get_document`; use `find_in_document` for targeted
   searches. Do not rely on stale line numbers.
6. Preserve or add the Touying package import and actual Touying slide/theme structure. Keep all
   new definitions and slide source inline in `main.typ`.
7. Call `apply_edits` with a coherent batch and the latest `rev` when available.
8. Inspect affected pages with `get_slide_preview` or the available Typst preview tool. Use
   `locate` when a rendered page must be mapped back to source.
9. Read speaker transcripts and resolve pending comments when the task requires it.
10. Call `export_pdf` only when the user asks for an export.

When creating a deck, a successful source edit is not enough. Confirm that it compiles, uses
Touying, renders the expected pages, has no unintended overflow or clipping, and contains no local
`.typ` imports/includes.

## Human collaboration and recovery

The live server project is the source of truth. Return the `web_url` from `open_project` when a
human needs to review the result; never expose the token or opaque `project_handle`.

- `EDIT_REJECTED`: the batch was refused because the edit was malformed or its selector no longer
  matches. `error.details` carries the failing `index`, the live `context`, and — for a malformed
  edit — the `expected_shape`. Fix the edit; do not retry it unchanged.
- `REVISION_CONFLICT`: re-read `main.typ`, obtain the new `rev`, and recompute the edit.
- `PROJECT_CONTEXT_CHANGED` or `PROJECT_HANDLE_EXPIRED`: call `open_project` again, retain the new
  handle, and re-read before changing anything.
- Authorization or scope error: do not print the token or repeatedly retry. Ask the human to
  check that the token is current and has Editor access.
- Transfer expiry or checksum error: start a fresh bounded transfer using the server's current
  tool description and verify the declared size and SHA-256.
- File conflict: re-read the file and use its current SHA-256. Never bypass compare-and-swap
  protection.

## Completion checklist

Before reporting success, verify all of the following:

- MCP initialization, tool discovery, and authentication succeeded.
- The intended project is open and the handle is current.
- `main.typ` is the sole Typst source and contains the complete presentation.
- No local auxiliary `.typ` import/include remains.
- The presentation imports and actually uses Touying.
- All affected pages compile and render correctly.
- Speaker notes remain inline in `main.typ`.
- Addressed comments were preview-verified before being marked done.
- The human received only the safe `web_url`, never a token or project handle.
