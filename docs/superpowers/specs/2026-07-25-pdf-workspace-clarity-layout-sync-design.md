# PDF Workspace Clarity, Layout, and Page Sync Design

Date: 2026-07-25

## Goal

Bring the PDF project experience up to the visual and interaction quality of the
Typst workspace without adding Typst-only editing or comment features.

This change covers four user-visible problems:

1. PDF pages are blurry in Preview, Presenter, and Projection.
2. The PDF terminal lacks the Typst terminal's visual structure and cannot be
   resized horizontally.
3. The native PDF file input is visually inconsistent with the Projects page.
4. Preview and Presentation need Typst-style, explicit two-way page syncing.

The PDF project model remains unchanged: one immutable `document.pdf`, one
page-number transcript mapping, terminal/AI access, and no Typst comments,
editor, resolver, or speaker-note tooling.

## Current-State Findings

- PyMuPDF currently calls `page.get_pixmap()` without a transform. That renders
  at the PDF's default 72 DPI. The active 16:9 test document therefore produces
  pages of only about 454 by 256 pixels.
- A Retina browser needs roughly twice the CSS dimensions in source pixels.
  Presenter and Projection can enlarge the same image further, so all three PDF
  views expose the low-resolution source.
- The PDF workspace uses a fixed two-column CSS grid. Unlike the Typst
  workspace, it has no draggable divider.
- The PDF terminal header is a plain light header rather than the reusable dark
  terminal chrome used by the Typst workspace.
- `TermPanel` can send its initial `cd` both from the prop effect and from the
  WebSocket open handler.
- Preview and Presenter currently share one page value. This keeps them
  automatically locked together, but does not provide Typst's independent
  browsing plus explicit "follow" and "send" controls.
- PDF creation exposes the browser-native file input.

## Considered Approaches

### 1. Versioned high-resolution prerendering

Render every PDF page to a bounded high-resolution PNG when a project is
activated or its PDF is replaced. Keep the existing atomic render directory,
generation tokens, and render API.

Advantages:

- Smallest change to the proven replacement and generation protocol.
- Preview, Presenter, and Projection improve together.
- Rendering remains deterministic and available without client-side PDF
  parsing.
- Existing content tokens and browser caching continue to work.

Disadvantages:

- Project activation and replacement render every page.
- Render cache size increases.

This is the selected approach. On the current 43-page test PDF, a render close
to the selected target takes about 1.7 seconds and uses about 8 MB, which is
acceptable.

### 2. On-demand server render sizes

Add width parameters to the render endpoint and cache multiple variants per
page.

This reduces initial rendering work and can target arbitrary displays, but it
adds variant cache invalidation, concurrent generation, and replacement-race
complexity to a safety-sensitive backend path.

### 3. Client-side PDF.js rendering

Send the original PDF to the browser and use PDF.js canvases.

This provides responsive vector-quality rendering and zooming, but adds a
large frontend dependency, duplicates PDF parsing in the browser, complicates
Projection and pointer geometry, and moves farther from the existing tested
render protocol.

## Backend Rendering Design

### Resolution policy

Each page is rendered proportionally so its longest edge is approximately
2560 pixels:

```text
scale = 2560 / max(page_width_points, page_height_points)
```

PyMuPDF uses that scale through a `fitz.Matrix(scale, scale)`. PNG output is
opaque (`alpha=False`) because PDF pages are displayed on an opaque white
surface.

This gives the active 16:9 test document an output close to 2560 by 1440 and
gives portrait documents the same strictly bounded longest edge. Pages whose
point dimensions already exceed the target are downsampled to that bound. It
is sufficient for Retina Preview/Presenter views and normal 1080p/1440p
projection without allowing unusually large page dimensions to create
unbounded images.

### Render profile versioning

The render state includes a fixed profile identifier, initially
`pdf-qhd-v1`. The profile participates in render freshness and generation
identity.

An existing render that has no profile or a different profile is stale. The
next project activation regenerates it before publishing the new render
generation. This guarantees that existing PDF projects migrate away from their
72-DPI cache without modifying `document.pdf` or transcript data.

Changing the resolution policy later requires a new profile identifier.

### Atomicity and replacement

Both initial rendering and replacement rendering use the same resolution
helper. Existing staging-directory, fsync, generation matching, and atomic
publication behavior remain intact.

A failed high-resolution render must not replace the last-good live render.
Replacement rollback and candidate preservation semantics do not change.

### API compatibility

The page filenames and `/api/render/{name}` contract remain unchanged.
Frontend `pages`, `tokens`, and generation matching continue to work without a
new public rendering API.

## PDF Workspace Layout and Terminal Design

### Resizable columns

Replace the fixed PDF grid columns with a flex layout:

```text
Terminal pane | 6px draggable divider | Preview pane
```

- Initial terminal width: about 38% of the available workspace width.
- Terminal minimum width: 280 px.
- Preview minimum width: 480 px.
- Dragging clamps the terminal width so neither pane becomes unusable.
- The body cursor changes to `col-resize` during a drag and is always restored
  on mouseup/unmount.
- The divider receives separator semantics and an accessible label.

`TermPanel` already observes its host with `ResizeObserver`. Column movement
therefore calls the existing fit path and sends the new PTY rows/columns
without reconnecting the WebSocket or terminating a running shell/agent.

### Terminal chrome

The PDF terminal uses the same visual language as the Typst terminal:

- dark terminal header;
- terminal icon;
- shortened current project path in the monospace path treatment;
- matching background, spacing, scrollbar, and divider hover state.

PDF-specific actions are not added to the header. The PDF workspace remains
terminal-first and does not gain Typst comment or editor actions.

### Initial working directory

`TermPanel` records the working directory before sending the initial `cd`.
The prop effect and WebSocket open handler share the same guard, so each fresh
shell receives the project `cd` exactly once.

Reconnection still applies the project directory once to the new shell.

## Preview and Presentation Page Model

### Independent page cursors

The PDF workspace owns two separate 1-based page values:

- `previewPage`: the page shown in `PdfPreviewPane`;
- `presentPage`: the page used by `Presenter` and `Projection`.

Both values are clamped whenever the rendered page list changes. Empty page
sets fall back to page 1 internally while displaying page 0 of 0.

### Starting a presentation

When the user starts a new Presenter session and no presentation/projection is
currently active, `presentPage` is initialized from `previewPage`. Closing
Presenter preserves both cursors.

Presenter navigation and keyboard controls update only `presentPage`.
Projection broadcasts and pointer state also use `presentPage`.

### Typst-style sync controls

The PDF Preview header shows both page positions and two controls:

- `⇤ Follow presentation`: set `previewPage` to `presentPage`.
- `⇥ Send preview to presentation`: set `presentPage` to `previewPage`.

These controls are enabled only when there are pages and Presenter or
Projection is active. Their labels and tooltips use the same terminology and
intent as the Typst workspace.

Normal Previous/Next controls in PDF Preview update only `previewPage`.

### Replacement and restore

If replacement or restore reduces the page count, both cursors clamp to the
new final page. Transcript reconciliation and orphan handling remain keyed to
the actual PDF page number and do not depend on either UI cursor.

## PDF Selection and Drop Design

The native file input remains in the DOM for browser file selection but is
visually hidden and controlled through a styled component.

The PDF creation form provides:

- a drop target integrated into the existing form;
- a `Select PDF` button;
- selected filename and human-readable size;
- `Change` and `Remove` actions;
- a distinct drag-over state;
- keyboard activation through the visible button.

Only one `.pdf` file is accepted. Dropping multiple files or a non-PDF file
does not clear an existing valid selection and displays a toast error.
Switching back to Typst or cancelling creation resets the hidden input and
selected file.

Frontend validation remains advisory. The backend continues to enforce the
single-file multipart contract, size limit, filename suffix, and actual PDF
validity.

## Error Handling

- High-resolution render failures preserve the previous live render and return
  the existing activation/replacement error path.
- Divider pointer/mouse cleanup runs even if the component unmounts during a
  drag.
- Page sync controls are disabled when they cannot identify a valid target.
- File-drop errors do not submit the form and do not silently discard a
  previously valid selection.
- Terminal refit failures remain non-fatal and do not reconnect the shell.

## Testing

### Backend

- Verify rendered output reaches the expected bounded longest edge for
  landscape and portrait pages.
- Verify both normal and replacement rendering use the same resolution policy.
- Verify a legacy/mismatched render profile forces one regeneration.
- Verify a matching profile reuses the current generation.
- Re-run replacement, rollback, inode-preservation, and render atomicity tests.

### Frontend unit tests

- Clamp `previewPage` and `presentPage` independently after page-count changes.
- Follow Presentation copies `presentPage` to `previewPage`.
- Send Preview copies `previewPage` to `presentPage`.
- Starting Presenter initializes from Preview only when no live presentation
  is already active.
- Divider width clamping preserves both minimum pane widths.
- PDF selection accepts one PDF and rejects multiple/non-PDF drops without
  losing an existing valid file.
- A fresh terminal socket sends exactly one initial `cd`.

### Browser integration

- Drag the PDF divider in both directions and confirm the xterm grid refits.
- Confirm Preview navigation does not move a live presentation until the send
  control is used.
- Confirm follow control brings Preview to the current Presenter page.
- Confirm Presenter and Projection use the same `presentPage`.
- Confirm Preview, Presenter, and Projection consume the high-resolution
  generation and no smoke project remains after testing.

## Deployment

1. Run backend, frontend, build, and browser integration verification.
2. Rebuild the Linux/amd64 workspace image.
3. Update the existing user container in place so its login and mounted
   workspace are preserved.
4. Open the existing PDF project; profile migration regenerates its pages.
5. Verify terminal resizing, upload styling, page sync, and image dimensions
   against the running service.

No container mount migration or user project data change is required.
