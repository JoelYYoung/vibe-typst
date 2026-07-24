"""Map a rendered PAGE number to its source slide + subslide, for comment context.

A touying `#slide(repeat: N)` produces N pages (subslides), and `#pause` also adds pages, so a
bare "PAGE 27" is unanchorable — the AI can't tell which slide/subslide it is. We use the
compiled document as ground truth: resolve a content point on each page to a source line, find
the enclosing column-0 slide opener, group consecutive pages by slide, and report which
subslide a page is plus the lines that drive that subslide (`sub == k` / `#only(k)`).
"""
import re

import docstore
import resolver
import runtime

# column-0 slide openers in touying decks
_OPENER = re.compile(r"^#(slide|centered-slide|focus-slide|title-slide)\b")
_SECTION = re.compile(r"^//\s*=+\s*(.*?)\s*=+\s*$")

_cache = {"version": None, "map": None, "lines": None}


def _page_dims() -> tuple[float, float]:
    try:
        svg = (runtime.render_dir() / "page-1.svg").read_text(encoding="utf-8")
        m = re.search(r'viewBox="[\d.]+ [\d.]+ ([\d.]+) ([\d.]+)"', svg)
        if m:
            return float(m.group(1)), float(m.group(2))
    except Exception:
        pass
    return 841.89, 473.56  # touying 16:9 default (pt)


# Cover the whole slide: the title band (top / top-left, where `valign: top` titles and
# bare `#pause` states sit), the centered body, and a few spread points. Ordered roughly by
# how likely a point is to land on content, so most pages resolve on the first few tries.
_PROBE_PTS = (
    (0.15, 0.11), (0.35, 0.11), (0.5, 0.12), (0.25, 0.18),  # title band
    (0.5, 0.45), (0.4, 0.4), (0.6, 0.5), (0.5, 0.6),         # centered body
    (0.5, 0.3), (0.2, 0.3), (0.7, 0.3), (0.35, 0.66),        # spread
    (0.65, 0.34), (0.5, 0.78), (0.15, 0.5), (0.85, 0.5),
)


def _probe_line(page_no: int, W: float, H: float):
    """Resolve a content point on a page to a 1-based source line, or None."""
    for fx, fy in _PROBE_PTS:
        r = resolver.resolve(page_no, W * fx, H * fy)
        if r.get("ok"):
            return r["start"][0] + 1  # resolver lines are 0-based
    return None


def _enclosing(lines: list[str], line1: int):
    """1-based content line -> (slide_open_line_1based or None, section_label or None)."""
    idx = min(max(line1 - 1, 0), len(lines) - 1)
    slide_line = None
    for i in range(idx, -1, -1):
        if _OPENER.match(lines[i]):
            slide_line = i + 1
            break
    section = None
    for i in range((slide_line - 1) if slide_line else idx, -1, -1):
        m = _SECTION.match(lines[i])
        if m:
            section = m.group(1)
            break
    return slide_line, section


def _build():
    src = docstore.get_text() or ""
    lines = src.split("\n")
    total = resolver.status().get("pages", 0)
    W, H = _page_dims()
    m: dict[int, dict] = {}
    for p in range(1, total + 1):
        ln = _probe_line(p, W, H)
        sl, sec = _enclosing(lines, ln) if ln else (None, None)
        m[p] = {"line": ln, "slide_line": sl, "section": sec,
                "slide_text": (lines[sl - 1].strip() if sl else None)}
    return m, lines


def _slide_end_line(lines: list[str], sl: int) -> int | None:
    """1-based line of the `]` that closes the `#slide[ ... ]` opening at 1-based line `sl`,
    by matching bracket depth while ignoring strings, line/block comments, raw code, and escaped
    markup. None if unbalanced. This is exact where the "next column-0 #slide opener" heuristic
    is not — nested/`#let` bodies, a stray column-0 `#slide` in literal content, and the last
    slide (which over-includes any trailing top-level content)."""
    depth = 0
    started = False
    in_str = False
    escaped = False
    block_comment_depth = 0
    raw_ticks = 0
    for li in range(sl - 1, len(lines)):
        line = lines[li]
        j = 0
        while j < len(line):
            ch = line[j]
            if raw_ticks:
                fence = "`" * raw_ticks
                if line.startswith(fence, j):
                    raw_ticks = 0
                    j += len(fence)
                    continue
            elif block_comment_depth:
                if line.startswith("/*", j):
                    block_comment_depth += 1
                    j += 2
                    continue
                if line.startswith("*/", j):
                    block_comment_depth -= 1
                    j += 2
                    continue
            elif in_str:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_str = False
            elif ch == "/" and j + 1 < len(line) and line[j + 1] == "/":
                break                      # rest of the line is a comment
            elif ch == "/" and j + 1 < len(line) and line[j + 1] == "*":
                block_comment_depth = 1
                j += 2
                continue
            elif ch == '"':
                in_str = True
            elif ch == "`":
                end = j + 1
                while end < len(line) and line[end] == "`":
                    end += 1
                raw_ticks = end - j
                j = end
                continue
            elif ch == "\\" and j + 1 < len(line):
                j += 2                    # escaped markup delimiter, e.g. \]
                continue
            elif ch == "[":
                depth += 1
                started = True
            elif ch == "]":
                depth -= 1
                if started and depth == 0:
                    return li + 1
            j += 1
    return None


def slide_info(page_no) -> dict | None:
    """For a 1-based page number, return slide-open line, section label, subslide index/total,
    the slide's source line span, and the source lines that drive THIS subslide."""
    try:
        page_no = int(page_no)
    except (TypeError, ValueError):
        return None
    ver = resolver.version()
    if _cache["version"] != ver or _cache["map"] is None:
        _cache["map"], _cache["lines"] = _build()
        _cache["version"] = ver
    m, lines = _cache["map"], _cache["lines"]
    info = m.get(page_no)
    if not info or not info.get("slide_line"):
        return None
    sl = info["slide_line"]
    first = last = page_no
    while first - 1 >= 1 and m.get(first - 1, {}).get("slide_line") == sl:
        first -= 1
    while m.get(last + 1, {}).get("slide_line") == sl:
        last += 1
    sub_index, sub_total = page_no - first + 1, last - first + 1
    # slide source span: prefer bracket-matching the `#slide[...]` body; fall back to the
    # "next column-0 opener (or EOF)" heuristic only if the brackets don't balance.
    end = _slide_end_line(lines, sl)
    if end is None:
        end = len(lines)
        for i in range(sl, len(lines)):
            if _OPENER.match(lines[i]):
                end = i
                break
    # lines that drive THIS subslide (sub == k / #only(k) / #uncover(k))
    k = sub_index
    sub_re = re.compile(rf"(sub\s*==\s*{k}\b|only\(\s*{k}\b|uncover\(\s*{k}\b)")
    sub_lines = [(i + 1, lines[i].strip()) for i in range(sl - 1, end) if sub_re.search(lines[i])]
    return {
        "slide_line": sl,
        "slide_text": info["slide_text"],
        "section": info["section"],
        "sub_index": sub_index,
        "sub_total": sub_total,
        "slide_end": end,
        "content_line": info["line"],
        "sub_lines": sub_lines,
    }
