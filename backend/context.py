"""Build the raw context blob that Claude sees for a comment.

Stored on the comment at creation (R5) so the UI can show exactly what will be handed to
Claude, and so it stays stable even as the file changes afterward. A comment may carry
several selections (multi-select). Surrounding source is **deduplicated and merged**:
overlapping/adjacent line ranges become one block, grouped by page (different pages stay
separate), so the same code is never shown twice.
"""

_PAD = 2  # lines of surrounding source around each element (keep tight to save context)


def _merge(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out: list[list[int]] = []
    for lo, hi in sorted(ranges):
        if out and lo <= out[-1][1] + 1:
            out[-1][1] = max(out[-1][1], hi)
        else:
            out.append([lo, hi])
    return [(a, b) for a, b in out]


def build_raw_context(payload: dict, source: str) -> str:
    file = payload.get("file") or "(active file)"
    body = (payload.get("body") or "").strip()
    sels = payload.get("selections") or payload.get("selection")
    lines = source.splitlines()
    parts = [f"file: {file}"]

    if isinstance(sels, list) and sels:
        parts.append(f"{len(sels)} selection(s):")
        elem_ranges: dict[object, list[tuple[int, int]]] = {}
        for i, s in enumerate(sels, 1):
            if not isinstance(s, dict):
                continue
            if s.get("kind") == "page":
                pn = s.get("page_no")
                info = s.get("slide")
                if info and info.get("slide_line"):
                    if info.get("sub_total", 1) > 1:
                        parts.append(f"  [{i}] PAGE {pn} = subslide {info['sub_index']} of "
                                     f"{info['sub_total']} of the slide opening at line "
                                     f"{info['slide_line']}: {info.get('slide_text')!r}")
                    else:
                        parts.append(f"  [{i}] PAGE {pn} = the single-page slide opening at line "
                                     f"{info['slide_line']}: {info.get('slide_text')!r}")
                    if info.get("section"):
                        parts.append(f"        section: {info['section']}")
                    parts.append(f"        slide source spans lines {info['slide_line']}-{info.get('slide_end', '?')}")
                    if info.get("sub_lines"):
                        parts.append(f"        lines that drive subslide {info['sub_index']}:")
                        for ln, txt in info["sub_lines"]:
                            parts.append(f"          {ln} | {txt}")
                else:
                    parts.append(f"  [{i}] PAGE {pn} (could not map to a slide; recompile and retry)")
            else:
                text = (s.get("text") or "").strip()
                ln = s.get("line")
                pg = s.get("page")
                # page is the slide number (meaningful for preview-clicked elements); it is
                # absent for editor-drag selections. Show each locator only when real, so we
                # never emit a literal "page None" / "line None".
                loc = ", ".join(p for p in (
                    f"page {pg}" if pg is not None else "",
                    f"line {ln}" if ln is not None else "",
                ) if p)
                where = f" ({loc})" if loc else ""
                parts.append(f"  [{i}] element{where}: {text!r}")
                if isinstance(ln, int) and lines:
                    # Span the WHOLE selection (start line .. end line), not just ±_PAD around the
                    # start — a multi-line element would otherwise be truncated after 2 lines.
                    to_off = s.get("to")
                    end_ln = (source.count("\n", 0, to_off) + 1) if isinstance(to_off, int) else ln
                    lo = max(1, ln - _PAD)
                    hi = min(len(lines), max(ln, end_ln) + _PAD)
                    elem_ranges.setdefault(pg, []).append((lo, hi))

        if elem_ranges:
            parts.append("")
            parts.append("relevant source (deduplicated, merged; line numbers are a SNAPSHOT at "
                         "capture time — for the CURRENT position use the comment's live `location`, "
                         "and copy exact edit anchors from `location.current_text` / get_document, not "
                         "from the display-escaped quotes above):")
            for pg in sorted(elem_ranges, key=lambda x: (x is None, x)):
                for lo, hi in _merge(elem_ranges[pg]):
                    where = f"page {pg}, " if pg is not None else ""
                    parts.append(f"  --- {where}lines {lo}-{hi} ---")
                    for n in range(lo, hi + 1):
                        parts.append(f"  {n:>4} | {lines[n - 1]}")
    else:
        # single-anchor fallback
        anchor = (payload.get("anchor_text") or "").strip()
        page = payload.get("page")
        if page is not None:
            parts.append(f"page: {page}")
        if anchor:
            parts.append("anchor_text:")
            parts.append("    " + anchor.replace("\n", "\n    "))
            pos = source.find(anchor.split("\n")[0])
            if pos >= 0 and lines:
                idx = source.count("\n", 0, pos)
                lo, hi = max(1, idx - _PAD + 1), min(len(lines), idx + _PAD + 1)
                parts.append(f"surrounding source (lines {lo}-{hi}):")
                for n in range(lo, hi + 1):
                    parts.append(f"  {n:>4} | {lines[n - 1]}")

    parts.append("")
    parts.append("instruction:")
    parts.append("    " + body.replace("\n", "\n    "))
    return "\n".join(parts)


_INSTR_MARKER = "\ninstruction:\n"


def replace_instruction(raw_context: str, body: str) -> str:
    """Swap ONLY the trailing instruction block of an existing raw_context with a new body,
    leaving the captured source snapshot untouched. Used when the user edits a comment's text
    so the context Claude reads stays in sync with the visible comment."""
    indented = "    " + (body or "").strip().replace("\n", "\n    ")
    idx = raw_context.rfind(_INSTR_MARKER)
    if idx < 0:  # malformed/old context — append a fresh instruction block
        return raw_context.rstrip() + "\n\ninstruction:\n" + indented
    return raw_context[:idx] + _INSTR_MARKER + indented
