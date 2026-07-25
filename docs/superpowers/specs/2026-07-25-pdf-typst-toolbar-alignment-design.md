# PDF and Typst Toolbar Alignment Design

**Date:** 2026-07-25

## Goal

Make the PDF project workspace use the same top-bar layout and interaction
language as the Typst workspace while preserving the functional differences
between the two project types.

## Shared visual structure

The PDF workspace will reuse the existing Typst toolbar classes and markup
patterns:

- `bar` for the 48-pixel workspace toolbar;
- `back-btn` for the Projects navigation;
- `bar-title` for the project name centered across the full toolbar;
- `openbtn present` for the Presenter action; and
- `actions` plus `status-chip live` for presentation status on the right.

The PDF project title will no longer use the PDF-only chip styling. Presenter
status will follow the Typst wording and placement: `no presentation` while
inactive and `live · N` while the Presenter or Projection is active.

## Intentional PDF differences

Visual alignment does not make the two workspaces functionally identical.

- PDF projects do not show Files, PDF export, Files & versions, or pending
  comment controls.
- The PDF Files & versions drawer and its API-loading state are removed.
- The PDF terminal remains permanently visible in the left resizable pane.
- PDF transcript download, visibility, editing, saving, and page navigation
  remain unchanged.
- Preview and Presentation retain independent page cursors.
- Opening Presenter retains the existing PDF rule: start from the Preview page
  when no presentation is active, otherwise keep the live Presentation page.

## Page synchronization controls

The PDF Preview header will use the same icon-only synchronization controls as
the Typst Preview header:

- `⇤` follows the current Presentation page in Preview;
- `⇥` sends the current Preview page to Presentation.

The controls reuse the Typst `pb-btn icon` styling and remain disabled unless a
Presenter or Projection is active. Their visible labels are removed. Each
button keeps a descriptive `title` tooltip and receives an `aria-label` so the
meaning remains available to mouse and assistive-technology users.

## Scope and implementation

This change will modify `PdfWorkspace.jsx`, `PdfPreviewPane.jsx`, and the
focused PDF workspace tests. PDF-only drawer CSS that becomes unreachable will
be removed, along with obsolete mobile rules for the project-name chip.

No backend, storage, transcript format, terminal session, rendering, or project
creation behavior changes are required.

## Verification

Automated checks will establish that:

- the PDF toolbar uses the shared centered title and presentation status;
- Files & versions is absent and its drawer cannot be opened;
- synchronization buttons are icon-only, retain tooltips and accessible
  labels, and preserve their existing click behavior;
- the PDF-specific terminal, transcript controls, page navigation, and
  presentation synchronization still work; and
- the production frontend builds successfully.

A browser check will compare the deployed PDF toolbar against the existing
Typst toolbar at desktop width and confirm hover tooltips for both sync icons.
