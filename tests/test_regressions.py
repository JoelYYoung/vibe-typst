import asyncio
import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


class DocstoreExternalEditGuardTest(unittest.TestCase):
    def setUp(self):
        import docstore

        self.docstore = docstore
        self.loop = asyncio.new_event_loop()
        docstore.set_loop(self.loop)

    def tearDown(self):
        self.loop.close()
        self.docstore.set_loop(None)

    def _state(self, text_value):
        from pycrdt import Doc, Text

        doc = Doc()
        text = doc.get("source", type=Text)
        text.insert(0, text_value)
        return {
            "key": "test-room",
            "base": None,
            "doc": doc,
            "text": text,
            "path": Path(tempfile.gettempdir()) / "vibe-typst-test.typ",
            "timer": None,
            "writeback": False,
            "external_guard_old": None,
            "external_guard_new": None,
            "external_guard_until": 0,
        }

    def test_stale_client_replay_cannot_undo_external_mcp_edit(self):
        old = "#slide[old title]"
        new = "#slide[new title]"
        st = self._state(old)

        self.docstore._replace_text(st, new)
        self.docstore._guard_external_edit(st, old)

        self.docstore._replace_text(st, old)
        self.docstore._sync(st)

        self.assertEqual(str(st["text"]), new)
        self.assertEqual(self.docstore._latest[st["key"]], new)
        if st["timer"]:
            st["timer"].cancel()

    def test_guard_does_not_clobber_non_exact_concurrent_edit(self):
        old = "#slide[old title]"
        new = "#slide[new title]"
        concurrent = old + "\n// human typed"
        st = self._state(old)

        self.docstore._replace_text(st, new)
        self.docstore._guard_external_edit(st, old)

        self.docstore._replace_text(st, concurrent)
        self.docstore._sync(st)

        self.assertEqual(str(st["text"]), concurrent)
        if st["timer"]:
            st["timer"].cancel()


class SplitBrainRoomKeyTest(unittest.TestCase):
    """The "split-brain room" revert bug: an MCP edit passing a RELATIVE file ("main.typ")
    must land in the SAME CRDT room as the browser, which connects using the ABSOLUTE file
    path. Before the fix the room key resolved relative paths against the process CWD while
    the disk path resolved them against the project dir, so the two "rooms" wrote the same
    file and overwrote each other (new slide flashes, then reverts to the old version)."""

    def setUp(self):
        import docstore
        import runtime

        self.docstore = docstore
        self.runtime = runtime
        self._prev_cwd = os.getcwd()
        self._prev_file = runtime._state.get("file")

    def tearDown(self):
        os.chdir(self._prev_cwd)
        self.runtime._state["file"] = self._prev_file

    def test_relative_and_absolute_file_share_one_room_key(self):
        with tempfile.TemporaryDirectory() as proj, tempfile.TemporaryDirectory() as cwd:
            main = Path(proj) / "main.typ"
            main.write_text("#slide[hi]", encoding="utf-8")
            # Force the failing condition: process CWD is NOT the project dir.
            os.chdir(cwd)
            self.runtime._state["file"] = str(main.resolve())

            browser_key = self.docstore.room_name(None)          # abs path -> browser room
            mcp_key = self.docstore.room_name("main.typ")        # MCP's relative arg
            abs_key = self.docstore.room_name(str(main.resolve()))

            self.assertEqual(browser_key, mcp_key)
            self.assertEqual(browser_key, abs_key)


class _Req:
    """A stand-in for a Starlette Request that carries a JSON body — enough to drive the
    /api/edit and /api/comments endpoint functions exactly the way an MCP call does."""

    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        return self._payload


class McpEditFlowTest(unittest.IsolatedAsyncioTestCase):
    """Simulate the real path: a Codex MCP tool call -> HTTP /api/edit -> backend CRDT room.
    Edits are submitted through the actual endpoint function against a live pycrdt room
    seeded from a temp .typ, with the process CWD deliberately != the project dir (so the
    room-unify fix is exercised too). Covers line-addressed edits, consecutive-edit
    ordering, and drift-proof StickyIndex anchors."""

    DECK = "apple\nbanana\ncherry\ndate\n"

    async def asyncSetUp(self):
        import app
        import docstore
        import runtime
        from pycrdt import Doc, Text

        self.app = app
        self.docstore = docstore
        self.runtime = runtime
        self._prev_cwd = os.getcwd()
        self._prev_file = runtime._state.get("file")

        self._tmp = tempfile.TemporaryDirectory()
        self._cwd = tempfile.TemporaryDirectory()
        self.main = Path(self._tmp.name) / "main.typ"
        self.main.write_text(self.DECK, encoding="utf-8")
        os.chdir(self._cwd.name)                    # CWD != project dir (the failing case)
        runtime._state["file"] = str(self.main.resolve())
        docstore.set_loop(asyncio.get_running_loop())

        # Pre-seed the live room exactly as the WebSocket handler would: a real pycrdt
        # Doc/Text under the BROWSER's key (absolute path). ensure_room() then returns THIS
        # room for the MCP call's relative "main.typ" — but only because the unify fix makes
        # both resolve to the same key; without it, ensure_room would look up a different key
        # and miss this room. So this setup also guards the room-unify fix.
        doc = Doc()
        text = doc.get("source", type=Text)
        with doc.transaction():
            text.insert(0, self.DECK)
        self.key = docstore.room_name(None)
        docstore._rooms[self.key] = {
            "key": self.key, "base": docstore._base_key(None), "doc": doc, "text": text,
            "path": self.main, "timer": None, "writeback": False, "poisoned": False,
            "last_mtime": self.main.stat().st_mtime,
            "external_guard_old": None, "external_guard_new": None, "external_guard_until": 0,
        }
        docstore._latest[self.key] = str(text)

    async def asyncTearDown(self):
        st = self.docstore._rooms.pop(self.key, None)
        if st and st.get("timer"):
            st["timer"].cancel()
        self.docstore._latest.pop(self.key, None)
        os.chdir(self._prev_cwd)
        self.runtime._state["file"] = self._prev_file
        self._tmp.cleanup()
        self._cwd.cleanup()

    async def _edit(self, **op):
        """Route an op through the real /api/edit endpoint, MCP-style (relative file)."""
        op.setdefault("file", "main.typ")
        return await self.app.edit(_Req(op))

    def _live(self):
        return self.docstore.get_text("main.typ")

    async def test_line_edit_goes_to_the_browser_room_and_hits_disk(self):
        # MCP passes the RELATIVE "main.typ"; the browser would connect to the absolute-path
        # room. After the fix they are one room, so the edit is visible to both and on disk.
        r = await self._edit(op="replace_lines", start=2, end=2, new_text="BANANA")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["room"], self.docstore.room_name(None))   # == browser's room key
        self.assertEqual(self._live(), "apple\nBANANA\ncherry\ndate\n")
        await asyncio.sleep(0)                                       # let the debounced flush settle
        self.docstore._flush(self.docstore._rooms[r["room"]])
        self.assertEqual(self.main.read_text(encoding="utf-8"), "apple\nBANANA\ncherry\ndate\n")

    async def test_insert_at_line_pushes_following_lines_down(self):
        r = await self._edit(op="insert_at_line", line=2, text="apricot")
        self.assertTrue(r["ok"], r)
        self.assertEqual(self._live(), "apple\napricot\nbanana\ncherry\ndate\n")

    async def test_two_consecutive_edits_second_sees_the_first(self):
        # First edit removes "banana".
        r1 = await self._edit(op="replace_anchor", anchor="banana", new_text="BANANA")
        self.assertTrue(r1["ok"], r1)
        # A STALE anchor built before edit 1 (still says "banana") must FAIL, not corrupt —
        # this is the "first edit breaks the second's anchor" case, handled cleanly.
        r2 = await self._edit(op="replace_anchor", anchor="banana", new_text="XXX")
        self.assertFalse(r2["ok"])
        self.assertEqual(r2["error"], "anchor not found")
        # A still-valid anchor operates on the POST-edit-1 text (edits are serialized).
        r3 = await self._edit(op="replace_anchor", anchor="cherry", new_text="CHERRY")
        self.assertTrue(r3["ok"], r3)
        self.assertEqual(self._live(), "apple\nBANANA\nCHERRY\ndate\n")

    async def test_sticky_anchor_does_not_drift_when_a_line_is_inserted_above(self):
        # Anchor the word "banana" (line 2, code-point span 6..12).
        rel = await self.docstore.make_rel_anchors([[6, 12]], "main.typ")
        self.assertTrue(rel)
        # Another party inserts a line ABOVE it (the exact scenario from the CRDT discussion).
        await self._edit(op="insert_at_line", line=2, text="apricot")
        spans = await self.docstore.resolve_rel_anchors(rel, "main.typ")
        live = self._live()
        self.assertEqual([live[a:b] for a, b in spans], ["banana"])   # followed the text, no drift

    async def test_sticky_anchor_after_target_deletion_is_deterministic(self):
        rel = await self.docstore.make_rel_anchors([[6, 12]], "main.typ")   # "banana"
        await self._edit(op="replace_lines", start=2, end=2, new_text="")   # delete the whole line
        spans = await self.docstore.resolve_rel_anchors(rel, "main.typ")
        # Deleting the anchored text can't be resurrected, but resolution stays in-bounds and
        # deterministic (no crash, converges) — the anchor lands at the surviving boundary.
        self.assertEqual(len(spans), 1)
        a, b = spans[0]
        self.assertLessEqual(0, a)
        self.assertLessEqual(a, b)
        self.assertLessEqual(b, len(self._live()))


class McpFullCoverageTest(unittest.TestCase):
    """Exercise EVERY MCP tool the way the agent (Codex/Claude) does: call the tool function,
    let it route through the REAL FastAPI app (real routing + JSON, via httpx ASGITransport)
    into the REAL docstore room and SQLite store, and MONITOR the resulting document text /
    comment state after each op. This is the "real environment" harness — a hermetic stand-in
    for `codex -> MCP -> HTTP -> backend room` so a break is caught here, not in production.

    CWD != project dir throughout, so the room-unify fix is under test on every routed call.
    """

    DECK = (
        '#import "@preview/touying:0.6.1": *\n'
        '#show: slides\n'
        '\n'
        '#slide[\n'
        '  #speaker-note("Intro note.")\n'
        '  = Title\n'
        '  Alpha\n'
        ']\n'
        '\n'
        '#slide[\n'
        '  #speaker-note("Second note.")\n'
        '  = Contents\n'
        '  Beta\n'
        '  Gamma\n'
        ']\n'
    )

    def setUp(self):
        import httpx
        import app
        import docstore
        import mcp_server
        import runtime
        import store
        from pycrdt import Doc, Text

        self.httpx = httpx
        self.app_mod = app
        self.docstore = docstore
        self.mcp = mcp_server
        self.runtime = runtime
        self.store = store

        self._prev_cwd = os.getcwd()
        self._prev_file = runtime._state.get("file")
        self.loop = asyncio.new_event_loop()
        docstore.set_loop(self.loop)

        self._tmp = tempfile.TemporaryDirectory()
        self._cwd = tempfile.TemporaryDirectory()
        self.main = Path(self._tmp.name) / "main.typ"
        self.main.write_text(self.DECK, encoding="utf-8")
        os.chdir(self._cwd.name)                          # CWD != project dir
        runtime._state["file"] = str(self.main.resolve())
        store.set_path(str(Path(self._tmp.name) / ".slide-comments.db"))

        doc = Doc()
        text = doc.get("source", type=Text)
        with doc.transaction():
            text.insert(0, self.DECK)
        self.key = docstore.room_name(None)
        docstore._rooms[self.key] = {
            "key": self.key, "base": docstore._base_key(None), "doc": doc, "text": text,
            "path": self.main, "timer": None, "writeback": False, "poisoned": False,
            "last_mtime": self.main.stat().st_mtime,
            "external_guard_old": None, "external_guard_new": None, "external_guard_until": 0,
        }
        docstore._latest[self.key] = str(text)

        # Route the MCP server's HTTP calls into the in-process ASGI app (real endpoints).
        self._orig_backend = mcp_server._backend
        mcp_server._backend = self._backend_shim

    def tearDown(self):
        self.mcp._backend = self._orig_backend
        st = self.docstore._rooms.pop(self.key, None)
        if st and st.get("timer"):
            st["timer"].cancel()
        self.docstore._latest.pop(self.key, None)
        try:
            self.store.close()
        except Exception:
            pass
        self.loop.close()
        os.chdir(self._prev_cwd)
        self.runtime._state["file"] = self._prev_file
        self._tmp.cleanup()
        self._cwd.cleanup()

    # --- the agent-side stub: MCP _backend -> real ASGI app -------------------------------
    def _backend_shim(self, method, path, payload=None):
        if path.startswith("/api/slide-map"):
            # The resolver/typst subprocess isn't run in tests; feed get_transcripts a
            # realistic slide-map (with the locating fields it must pass through) so its
            # reshaping logic is exercised end-to-end.
            return {"pages": [{"page": 1, "slide_no": 1, "slide_line": 4, "sub_index": 1,
                               "sub_total": 1, "section": "Intro", "note": "Intro note.",
                               "note_raw": "Intro note.", "note_line": 5}],
                    "total": 1, "orphans": [{"text": "stale", "slide_line": 9}]}
        if path.startswith("/api/locate"):     # /api/locate calls the resolver; stub it here
            if "slide=1" in path:
                return {"ok": True, "kind": "slide", "slide_no": 1, "pages": [1],
                        "slide_line": 4, "slide_end": 8, "section": "Intro", "sub_total": 1,
                        "note_lines": [5]}
            if "page=1" in path:
                return {"ok": True, "kind": "page", "page": 1, "slide_no": 1, "slide_line": 4,
                        "slide_end": 8, "section": "Intro", "sub_index": 1, "sub_total": 1,
                        "sub_lines": [], "note_line": 5, "note_raw": "Intro note."}
            return {"ok": False, "error": "not found"}
        return self.loop.run_until_complete(self._route(method, path, payload))

    async def _route(self, method, path, payload):
        transport = self.httpx.ASGITransport(app=self.app_mod.app)
        async with self.httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.request(method, path, json=payload)
            return r.json()

    # --- helpers --------------------------------------------------------------------------
    def _doc(self):
        return self.docstore.get_text("main.typ")

    def _line_of(self, needle):
        return self.mcp.find_in_document(needle)["hits"][0]["line"]

    def _make_comment(self, anchor, body):
        frm = self.DECK.index(anchor)
        payload = {
            "kind": "element",
            "anchor_text": anchor,                       # the browser sends this (App.jsx)
            "selections": [{"kind": "element", "text": anchor, "from": frm, "to": frm + len(anchor),
                            "line": self.DECK[:frm].count("\n") + 1}],
            "body": body,
        }
        created = self.loop.run_until_complete(self._route("POST", "/api/comments", [payload]))
        return created[0]

    # --- the test: walk every tool --------------------------------------------------------
    def test_all_mcp_tools_end_to_end(self):
        m = self.mcp

        # ---- comment read tools ----
        c1 = self._make_comment("Alpha", "make Alpha bold")
        c2 = self._make_comment("Gamma", "drop this one")

        pending = m.get_pending_comments()
        self.assertEqual(len(pending), 2)
        self.assertEqual({p["anchor_text"] for p in pending}, {"Alpha", "Gamma"})
        self.assertTrue(all(p["status"] == "pending" for p in pending))
        # live location resolved from the drift-proof anchor (not raw_context's frozen lines)
        loc = {p["anchor_text"]: p["location"] for p in pending}
        self.assertEqual(loc["Alpha"]["current_text"], ["Alpha"])
        self.assertEqual(loc["Alpha"]["lines"], [self.DECK[:self.DECK.index("Alpha")].count("\n") + 1])

        got = m.get_comment(c1["id"])
        self.assertEqual(got["id"], c1["id"])
        self.assertEqual(got["comment"], "make Alpha bold")
        self.assertIn("error", m.get_comment("nope-zzz"))

        self.assertEqual(len(m.list_all_comments()), 2)

        # ---- rel_anchor was captured at create time and resolves live ----
        anchor = self.loop.run_until_complete(self._route("GET", f"/api/comments/{c1['id']}/anchor", None))
        self.assertEqual(anchor["texts"], ["Alpha"])

        # ---- document read tools ----
        gd = m.get_document(offset=1, limit=200)
        self.assertEqual(gd["total_lines"], self.DECK.count("\n") + 1)
        self.assertIn("Alpha", gd["text"])
        past = m.get_document(offset=9999)
        self.assertIsNone(past["shown"])

        fd = m.find_in_document("slide[")
        self.assertEqual(fd["matches"], 2)                        # two #slide[ blocks

        # ---- edit tools: monitor the live text after each ----
        r = m.replace_anchor("Alpha", "*Alpha*")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["room"], self.key)                    # unified room
        self.assertIn("*Alpha*", self._doc())

        # ambiguous + not-found anchors must refuse WITHOUT mutating
        before = self._doc()
        self.assertFalse(m.replace_anchor("#slide[", "X")["ok"])         # 2 matches -> ambiguous
        self.assertEqual(m.replace_anchor("zzz-absent", "X")["error"], "anchor not found")
        self.assertEqual(self._doc(), before)

        # occurrence disambiguation edits the 2nd #slide[
        r = m.replace_anchor("#slide[", "#slide(background: navy)[", occurrence=2)
        self.assertTrue(r["ok"], r)
        self.assertIn("#slide(background: navy)[", self._doc())

        r = m.insert_after_anchor("= Title", '\n  #speaker-note("added")')
        self.assertTrue(r["ok"], r)
        self.assertIn('= Title\n  #speaker-note("added")', self._doc())

        r = m.insert_before_anchor("  Beta", "  Alpha-2\n")
        self.assertTrue(r["ok"], r)
        self.assertIn("Alpha-2\n  Beta", self._doc())

        # line-addressed edits, with line numbers read live (as an agent would)
        r = m.replace_lines(self._line_of("  Beta"), self._line_of("  Beta"), "  BETA!")
        self.assertTrue(r["ok"], r)
        self.assertIn("BETA!", self._doc())
        self.assertNotIn("  Beta\n", self._doc())

        gline = self._line_of("  Gamma")
        r = m.insert_at_line(gline, "  Gamma-0")
        self.assertTrue(r["ok"], r)
        self.assertIn("Gamma-0\n  Gamma", self._doc())

        # replace_range on offsets derived from the CURRENT doc, and via a RELATIVE file arg
        cur = self._doc()
        i = cur.index("Contents")
        r = m.replace_range(i, i + len("Contents"), "CONTENTS", file="main.typ")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["room"], self.key)                    # relative file -> same room
        self.assertIn("= CONTENTS", self._doc())

        # ---- transcripts: get_transcripts must pass through the LOCATING fields, not just note ----
        tr = m.get_transcripts()
        self.assertEqual(tr["total"], 1)
        p0 = tr["pages"][0]
        self.assertEqual(p0["note"], "Intro note.")            # display text
        self.assertEqual(p0["note_raw"], "Intro note.")        # exact source literal (edit anchor)
        self.assertEqual(p0["note_line"], 5)                   # source line of the #speaker-note
        self.assertEqual(p0["slide_line"], 4)                  # opener line (for slide↔note mapping)
        self.assertEqual(tr["orphans"], [{"text": "stale", "slide_line": 9}])

        # ---- comment status tools ----
        done = m.mark_comment_done(c1["id"], "made it bold")
        self.assertEqual(done["status"], "done")
        dis = m.mark_comment_dismissed(c2["id"], "not needed")
        self.assertEqual(dis["status"], "dismissed")

        self.assertEqual(m.get_pending_comments(), [])           # none left pending
        statuses = {c["id"]: c["status"] for c in m.list_all_comments()}
        self.assertEqual(statuses, {c1["id"]: "done", c2["id"]: "dismissed"})

    # ---- the unified apply_edits primitive ------------------------------------------------
    def test_apply_edits_atomic_batch_multiple_selectors(self):
        m = self.mcp
        gd = m.get_document(offset=1, limit=200)
        rev0 = gd["rev"]
        r = m.apply_edits([
            {"selector": {"by": "anchor", "text": "Alpha"}, "text": "ALPHA"},
            {"selector": {"by": "anchor", "text": "Beta"}, "text": "BETA"},
            {"selector": {"by": "lines", "start": self._line_of("  Gamma")}, "text": "  Gamma-0"},
        ], base_rev=rev0)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["applied"], 3)
        self.assertEqual(r["rev"], rev0 + 1)                     # ONE revision for the whole batch
        doc = self._doc()
        self.assertIn("ALPHA", doc)
        self.assertIn("BETA", doc)
        self.assertIn("Gamma-0\n  Gamma", doc)

    def test_apply_edits_expect_cas_refuses_stale_and_applies_nothing(self):
        m = self.mcp
        before = self._doc()
        # stale precondition: the selected span isn't what we claim -> whole batch refused
        r = m.apply_edits([
            {"selector": {"by": "anchor", "text": "= Title"}, "text": "= TITLE", "expect": "= Title"},
            {"selector": {"by": "anchor", "text": "= Contents"}, "text": "X", "expect": "= WRONG"},
        ])
        self.assertFalse(r["ok"])
        self.assertTrue(r["conflict"])
        self.assertEqual(r["index"], 1)
        self.assertIn("context", r)
        self.assertEqual(self._doc(), before)                   # atomic: nothing applied
        # correct precondition -> applies
        r = m.apply_edits([{"selector": {"by": "anchor", "text": "= Title"},
                            "text": "= TITLE", "expect": "= Title"}])
        self.assertTrue(r["ok"], r)
        self.assertIn("= TITLE", self._doc())

    def test_apply_edits_consecutive_calls_second_sees_first(self):
        m = self.mcp
        r1 = m.apply_edits([{"selector": {"by": "anchor", "text": "Alpha"}, "text": "Zeta"}])
        self.assertTrue(r1["ok"], r1)
        r2 = m.apply_edits([{"selector": {"by": "anchor", "text": "Zeta"}, "text": "Omega"}])
        self.assertTrue(r2["ok"], r2)
        self.assertEqual(r2["rev"], r1["rev"] + 1)
        self.assertIn("Omega", self._doc())
        self.assertNotIn("Zeta", self._doc())
        # a stale selector from before r1 fails cleanly, doesn't corrupt
        r3 = m.apply_edits([{"selector": {"by": "anchor", "text": "Alpha"}, "text": "X"}])
        self.assertFalse(r3["ok"])
        self.assertEqual(r3["error"], "anchor not found")

    def test_apply_edits_rejects_overlapping_edits(self):
        m = self.mcp
        before = self._doc()
        i = before.index("Contents")
        r = m.apply_edits([
            {"selector": {"by": "range", "from": i, "to": i + 8}, "text": "A"},
            {"selector": {"by": "range", "from": i + 4, "to": i + 12}, "text": "B"},
        ])
        self.assertFalse(r["ok"])
        self.assertIn("overlap", r["error"])
        self.assertEqual(self._doc(), before)

    def test_comment_sticky_anchor_survives_apply_edits(self):
        m = self.mcp
        c = self._make_comment("Beta", "keep me anchored")
        # a batch that inserts a line ABOVE the anchored word must not drift the comment
        m.apply_edits([{"selector": {"by": "lines", "start": self._line_of("  Beta")},
                        "text": "  Inserted line"}])
        anchor = self.loop.run_until_complete(
            self._route("GET", f"/api/comments/{c['id']}/anchor", None))
        self.assertEqual(anchor["texts"], ["Beta"])

    def test_locate_distinguishes_slide_from_page(self):
        m = self.mcp
        self.assertFalse(m.locate()["ok"])                       # must pass exactly one
        self.assertFalse(m.locate(page=1, slide=1)["ok"])        # not both
        r = m.locate(slide=1)
        self.assertEqual(r["kind"], "slide")
        self.assertEqual((r["slide_line"], r["slide_end"], r["pages"]), (4, 8, [1]))
        p = m.locate(page=1)
        self.assertEqual(p["kind"], "page")
        self.assertEqual((p["slide_line"], p["sub_index"], p["note_line"]), (4, 1, 5))

    def test_page_comment_gets_live_location_from_slide_opener(self):
        # A page comment carries no from/to; the backend anchors the slide OPENER line so it
        # still gets a live `location` (DECK line 4 is "#slide[").
        payload = {"kind": "page",
                   "selections": [{"kind": "page", "page_no": 1,
                                   "slide": {"slide_line": 4, "slide_end": 8, "sub_total": 1}}],
                   "body": "split this slide"}
        created = self.loop.run_until_complete(self._route("POST", "/api/comments", [payload]))
        cid = created[0]["id"]
        anchor = self.loop.run_until_complete(self._route("GET", f"/api/comments/{cid}/anchor", None))
        self.assertEqual(anchor["lines"], [4])
        self.assertEqual(anchor["texts"], ["#slide["])
        # ...and it drifts correctly: insert a line above, the opener anchor follows to line 5
        m = self.mcp
        m.apply_edits([{"selector": {"by": "lines", "start": 1}, "text": "// header"}])
        anchor2 = self.loop.run_until_complete(self._route("GET", f"/api/comments/{cid}/anchor", None))
        self.assertEqual(anchor2["texts"], ["#slide["])
        self.assertEqual(anchor2["lines"], [5])


class SlideEndBracketTest(unittest.TestCase):
    """Bracket-aware `slide_end` — exact where the old 'next column-0 #slide opener' heuristic
    was wrong (nested brackets, `]` in strings/comments, and the last slide over-including
    trailing top-level content)."""

    def _end(self, text, sl):
        import slidemap
        return slidemap._slide_end_line(text.split("\n"), sl)

    def test_simple_two_slides(self):
        s = "#slide[\n  body\n]\n#slide[\n x\n]\n"
        self.assertEqual(self._end(s, 1), 3)
        self.assertEqual(self._end(s, 4), 6)

    def test_nested_brackets(self):
        self.assertEqual(self._end("#slide[\n  #grid[a][b]\n  = T\n]\nafter\n", 1), 4)

    def test_bracket_in_string_is_ignored(self):
        self.assertEqual(self._end('#slide[\n  = "a ] b"\n  x\n]\n', 1), 4)

    def test_bracket_in_comment_is_ignored(self):
        self.assertEqual(self._end('#slide[\n  // a ] comment\n  x\n]\n', 1), 4)

    def test_last_slide_stops_at_closing_bracket_not_eof(self):
        # old heuristic (no following opener) returned EOF and ate the appendix; bracket match = line 3
        self.assertEqual(self._end("#slide[\n  x\n]\n\n#pagebreak()\n= Appendix\n", 1), 3)

    def test_unbalanced_returns_none_for_fallback(self):
        self.assertIsNone(self._end("#slide[\n  x\n", 1))


class RawContextWindowTest(unittest.TestCase):
    def test_multiline_element_window_spans_to_end_and_has_caveat(self):
        import context
        source = "\n".join(f"line{i}" for i in range(1, 11))       # 10 lines
        to_off = len("\n".join(source.split("\n")[:8]))            # end of line 8
        payload = {"file": "main.typ", "body": "fix",
                   "selections": [{"kind": "element", "text": "blah", "line": 3,
                                   "from": 0, "to": to_off}]}
        rc = context.build_raw_context(payload, source)
        self.assertIn("SNAPSHOT at", rc)                           # freshness/escaping caveat present
        self.assertIn("line8", rc)                                 # window reached the selection END line
        self.assertIn("line10", rc)                                # end line + pad, not just start+pad


class ApplyEditsEdgeCaseTest(unittest.IsolatedAsyncioTestCase):
    """Edge cases of the unified apply_edits primitive at the backend-room level (the faithful
    'MCP -> HTTP -> room' text path is covered by McpFullCoverageTest; here we stress the
    primitive itself): multibyte offsets, newline handling, empty/EOF, batch adjacency, and
    stale-base_rev rebasing."""

    async def asyncSetUp(self):
        import docstore
        import runtime

        self.docstore = docstore
        self.runtime = runtime
        self._prev_cwd = os.getcwd()
        self._prev_file = runtime._state.get("file")
        self._tmp = tempfile.TemporaryDirectory()
        self._cwd = tempfile.TemporaryDirectory()
        os.chdir(self._cwd.name)
        docstore.set_loop(asyncio.get_running_loop())
        self.main = Path(self._tmp.name) / "main.typ"
        self.key = None

    def _seed(self, content):
        from pycrdt import Doc, Text
        self.main.write_text(content, encoding="utf-8")
        self.runtime._state["file"] = str(self.main.resolve())
        doc = Doc()
        text = doc.get("source", type=Text)
        with doc.transaction():
            text.insert(0, content)
        self.key = self.docstore.room_name(None)
        self.docstore._rooms[self.key] = {
            "key": self.key, "base": self.docstore._base_key(None), "doc": doc, "text": text,
            "path": self.main, "timer": None, "writeback": False, "poisoned": False, "rev": 0,
            "last_mtime": self.main.stat().st_mtime,
            "external_guard_old": None, "external_guard_new": None, "external_guard_until": 0,
        }
        self.docstore._latest[self.key] = str(text)

    async def asyncTearDown(self):
        if self.key:
            st = self.docstore._rooms.pop(self.key, None)
            if st and st.get("timer"):
                st["timer"].cancel()
            self.docstore._latest.pop(self.key, None)
        os.chdir(self._prev_cwd)
        self.runtime._state["file"] = self._prev_file
        self._tmp.cleanup()
        self._cwd.cleanup()

    def _doc(self):
        return self.docstore.get_text("main.typ")

    async def _edits(self, edits, base_rev=None):
        return await self.docstore.apply_edits(edits, "main.typ", base_rev)

    async def test_multibyte_offsets_do_not_corrupt(self):
        # é=2 bytes, —=3, CJK=3 each, 🎉=4 — anchors after them must still land byte-correct.
        self._seed("café — 日本語 🎉 tail\n")
        r = await self._edits([
            {"selector": {"by": "anchor", "text": "日本語"}, "text": "JP"},
            {"selector": {"by": "anchor", "text": "tail"}, "text": "TAIL"},
        ])
        self.assertTrue(r["ok"], r)
        self.assertEqual(self._doc(), "café — JP 🎉 TAIL\n")

    async def test_replace_last_line_without_trailing_newline(self):
        self._seed("a\nb\nc")                      # no trailing newline
        r = await self._edits([{"selector": {"by": "lines", "start": 3, "end": 3}, "text": "C"}])
        self.assertTrue(r["ok"], r)
        self.assertEqual(self._doc(), "a\nb\nC")   # no spurious newline appended at EOF

    async def test_insert_into_empty_document(self):
        self._seed("")
        r = await self._edits([{"selector": {"by": "lines", "start": 1}, "text": "hello"}])
        self.assertTrue(r["ok"], r)
        self.assertEqual(self._doc(), "hello\n")

    async def test_insert_past_eof_appends(self):
        self._seed("a\nb\n")
        r = await self._edits([{"selector": {"by": "lines", "start": 999}, "text": "z"}])
        self.assertTrue(r["ok"], r)
        self.assertEqual(self._doc(), "a\nb\nz\n")

    async def test_adjacent_batch_edits_both_apply(self):
        self._seed("0123456789\n")
        r = await self._edits([
            {"selector": {"by": "range", "from": 2, "to": 4}, "text": "AB"},
            {"selector": {"by": "range", "from": 4, "to": 6}, "text": "CD"},   # touches the first
        ])
        self.assertTrue(r["ok"], r)
        self.assertEqual(self._doc(), "01ABCD6789\n")

    async def test_delete_and_insert_in_one_batch(self):
        self._seed("keep DELETE keep2\n")
        r = await self._edits([
            {"selector": {"by": "anchor", "text": "DELETE "}, "text": ""},          # delete
            {"selector": {"by": "anchor", "text": "keep2", "side": "before"}, "text": "NEW "},
        ])
        self.assertTrue(r["ok"], r)
        self.assertEqual(self._doc(), "keep NEW keep2\n")

    async def test_stale_base_rev_applies_when_region_unchanged(self):
        self._seed("a\nb\nc\n")
        r1 = await self._edits([{"selector": {"by": "anchor", "text": "a"}, "text": "A"}])
        self.assertTrue(r1["ok"])
        # base_rev is now stale (0), but this edit touches a DIFFERENT region -> still applies
        r2 = await self._edits([{"selector": {"by": "anchor", "text": "c"}, "text": "C"}], base_rev=0)
        self.assertTrue(r2["ok"], r2)
        self.assertTrue(r2["rebased"])
        self.assertEqual(self._doc(), "A\nb\nC\n")

    async def test_expect_guards_a_concurrently_changed_region(self):
        self._seed("title: Draft\n")
        # someone changed "Draft" -> "Final" already; an edit that still expects "Draft" must refuse
        await self._edits([{"selector": {"by": "anchor", "text": "Draft"}, "text": "Final"}])
        r = await self._edits([{"selector": {"by": "anchor", "text": "Final"},
                                "text": "Shipped", "expect": "Draft"}])
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"], "expect mismatch")
        self.assertEqual(self._doc(), "title: Final\n")     # untouched

    async def test_comment_anchor_on_trailing_text_of_no_newline_file(self):
        # Regression: sticky_index(EOF, Assoc.AFTER) panics (a BaseException that escapes
        # `except Exception`), which would 500 comment creation. Anchoring the last word of a
        # file with no trailing newline must succeed and resolve.
        self._seed("= Title\nBody text")               # no trailing newline; "text" ends at EOF
        i = self._doc().index("text")
        rel = await self.docstore.make_rel_anchors([[i, i + 4]], "main.typ")
        self.assertEqual(len(rel), 1)                   # anchor created (did not panic / drop)
        spans = await self.docstore.resolve_rel_anchors(rel, "main.typ")
        self.assertEqual([self._doc()[a:b] for a, b in spans], ["text"])

    async def test_comment_anchor_on_empty_document_does_not_crash(self):
        self._seed("")
        rel = await self.docstore.make_rel_anchors([[0, 0]], "main.typ")   # must not panic
        spans = await self.docstore.resolve_rel_anchors(rel, "main.typ")
        for a, b in spans:
            self.assertTrue(0 <= a <= b <= len(self._doc()))

    async def test_zero_width_caret_anchor_resolves_in_order(self):
        self._seed("abcdef")
        rel = await self.docstore.make_rel_anchors([[3, 3]], "main.typ")   # a caret
        await self._edits([{"selector": {"by": "range", "from": 3, "to": 3}, "text": "XY"}])
        spans = await self.docstore.resolve_rel_anchors(rel, "main.typ")
        self.assertEqual(len(spans), 1)
        a, b = spans[0]
        self.assertTrue(0 <= a <= b <= len(self._doc()))    # never from > to

    async def test_same_offset_batch_inserts_keep_input_order(self):
        # Regression: two inserts at one offset must land in input order ("AXYB"), not swapped.
        self._seed("AB")
        r = await self._edits([
            {"selector": {"by": "range", "from": 1, "to": 1}, "text": "X"},
            {"selector": {"by": "range", "from": 1, "to": 1}, "text": "Y"},
        ])
        self.assertTrue(r["ok"], r)
        self.assertEqual(self._doc(), "AXYB")

    async def test_delete_last_line_of_no_newline_file_leaves_no_phantom(self):
        self._seed("a\nb\nc")                            # no trailing newline
        r = await self._edits([{"selector": {"by": "lines", "start": 3, "end": 3}, "text": ""}])
        self.assertTrue(r["ok"], r)
        self.assertEqual(self._doc(), "a\nb")            # not "a\nb\n"

    async def test_nfc_nfd_anchor_mismatch_refuses_cleanly(self):
        self._seed("cafe\u0301 latte")           # DECOMPOSED: e + U+0301 combining acute
        before = self._doc()
        r = await self._edits([{"selector": {"by": "anchor", "text": "caf\u00e9"},  # precomposed
                                "text": "COFFEE"}])
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"], "anchor not found")
        self.assertEqual(self._doc(), before)            # no corruption near the combining mark

    async def test_insert_at_line_1_pushes_document_down(self):
        self._seed("first\nsecond")
        r = await self._edits([{"selector": {"by": "lines", "start": 1}, "text": "zero"}])
        self.assertTrue(r["ok"], r)
        self.assertEqual(self._doc(), "zero\nfirst\nsecond")

    async def test_malformed_selectors_refuse_without_mutating(self):
        self._seed("a\nb\nc\n")
        before = self._doc()
        cases = [
            {"selector": {"by": "anchor", "text": ""}, "text": "X"},                 # empty anchor
            {"selector": {"by": "range", "from": 0, "to": 999}, "text": "X"},         # past EOF
            {"selector": {"by": "lines", "start": 2, "end": 1}, "text": "X"},         # inverted range
            {"selector": {"by": "bogus"}, "text": "X"},                              # unknown kind
        ]
        for c in cases:
            r = await self._edits([c])
            self.assertFalse(r["ok"], c)
            self.assertEqual(self._doc(), before)        # nothing applied on any refusal

    async def test_invalid_anchor_occurrence_and_side_refuse_without_mutating(self):
        cases = [
            {"selector": {"by": "anchor", "text": "unique", "occurrence": 0}, "text": "X"},
            {"selector": {"by": "anchor", "text": "unique", "occurrence": -1}, "text": "X"},
            {"selector": {"by": "anchor", "text": "x", "occurrence": 3}, "text": "X"},
            {"selector": {"by": "anchor", "text": "unique", "occurrence": "bad"}, "text": "X"},
            {"selector": {"by": "anchor", "text": "unique", "side": "around"}, "text": "X"},
        ]
        for edit in cases:
            with self.subTest(selector=edit["selector"]):
                self._seed("x x unique\n")
                before = self._doc()
                r = await self._edits([edit])
                self.assertFalse(r["ok"], r)
                self.assertTrue(r["conflict"], r)
                self.assertEqual(self._doc(), before)

    async def test_comment_anchor_stays_in_bounds_after_delete_then_reinsert(self):
        self._seed("alpha beta gamma\n")
        rel = await self.docstore.make_rel_anchors([[6, 10]], "main.typ")   # "beta"
        await self._edits([{"selector": {"by": "anchor", "text": "beta "}, "text": ""}])
        await self._edits([{"selector": {"by": "anchor", "text": "alpha ", "side": "after"},
                            "text": "beta "}])                              # reinsert new chars
        spans = await self.docstore.resolve_rel_anchors(rel, "main.typ")
        self.assertEqual(len(spans), 1)
        a, b = spans[0]
        self.assertTrue(0 <= a <= b <= len(self._doc()))     # deterministic, never out of range


class VcsVersioningTest(unittest.TestCase):
    """The version system must version the DECK only — not the app-managed tooling
    (AGENTS.md / CLAUDE.md / .codex) that `workdir.setup()` regenerates on every open, nor
    crash dumps. Otherwise the repo is falsely 'dirty' (yellow dots), saves fire when the deck
    is unchanged, and re-opening a project stops pointing at the latest saved version."""

    def setUp(self):
        import vcs
        self.vcs = vcs
        self._tmp = tempfile.TemporaryDirectory()
        self.d = Path(self._tmp.name)
        (self.d / "main.typ").write_text("#slide[hello]\n", encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def _tooling(self):
        (self.d / "AGENTS.md").write_text("agent instructions\n", encoding="utf-8")
        (self.d / "CLAUDE.md").write_text("agent instructions\n", encoding="utf-8")
        (self.d / ".codex").mkdir(exist_ok=True)
        (self.d / ".codex" / "config.toml").write_text("cfg\n", encoding="utf-8")

    def _git(self, *args):
        return subprocess.run(["git", *args], cwd=str(self.d), capture_output=True, text=True)

    def test_fresh_project_reports_unsaved_and_first_save_creates_v1(self):
        st = self.vcs.status(self.d)
        self.assertFalse(st["initialized"])
        self.assertTrue(st["dirty"])                         # keeps the first-save UI enabled
        self.assertEqual(st["current"], None)

        result = self.vcs.save_version(self.d, "first")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["tag"], "v1")
        self.assertEqual([v["tag"] for v in self.vcs.list_versions(self.d)], ["v1"])
        self.assertEqual(self.vcs.status(self.d)["current"], "v1")

    def test_clean_untagged_repository_can_create_v1(self):
        self.vcs.save_version(self.d, "first")
        self._git("tag", "-d", "v1")
        self.assertEqual(self.vcs.list_versions(self.d), [])
        self.assertFalse(self.vcs.status(self.d)["dirty"])

        result = self.vcs.save_version(self.d, "replacement first")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["tag"], "v1")
        self.assertFalse(result.get("skipped", False))

    def test_version_captures_deck_only_not_tooling_or_crash_dumps(self):
        self._tooling()
        (self.d / "core").write_bytes(b"x" * 4096)          # a crash dump
        r = self.vcs.save_version(self.d, "first")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["tag"], "v1")
        tracked = set(self._git("ls-files").stdout.split())
        self.assertIn("main.typ", tracked)
        for junk in ("AGENTS.md", "CLAUDE.md", ".codex/config.toml", "core"):
            self.assertNotIn(junk, tracked)
        st = self.vcs.status(self.d)
        self.assertFalse(st["dirty"])
        self.assertEqual(st["current"], "v1")

    def test_regenerated_tooling_does_not_make_repo_dirty(self):
        self._tooling()
        self.vcs.save_version(self.d, "first")
        # simulate re-open: setup() rewrites the tooling with DIFFERENT content
        (self.d / "AGENTS.md").write_text("NEW v2 instructions\n", encoding="utf-8")
        (self.d / ".codex" / "config.toml").write_text("new cfg\n", encoding="utf-8")
        st = self.vcs.status(self.d)
        self.assertFalse(st["dirty"])                       # invisible to version control
        self.assertEqual(st["current"], "v1")               # still points at the latest version

    def test_save_is_noop_when_only_tooling_changed(self):
        self._tooling()
        self.vcs.save_version(self.d, "first")
        (self.d / "AGENTS.md").write_text("regenerated\n", encoding="utf-8")
        r = self.vcs.save_version(self.d, "again")
        self.assertTrue(r.get("skipped"))                   # no duplicate version
        self.assertEqual(r["tag"], "v1")
        self.assertEqual([v["tag"] for v in self.vcs.list_versions(self.d)], ["v1"])

    def test_real_deck_change_creates_new_version(self):
        self.vcs.save_version(self.d, "first")
        (self.d / "main.typ").write_text("#slide[hello world]\n", encoding="utf-8")
        self.assertTrue(self.vcs.status(self.d)["dirty"])
        self.assertEqual(self.vcs.save_version(self.d, "second")["tag"], "v2")

    def test_migrate_untracks_legacy_tooling_and_keeps_current_version(self):
        # An OLD repo that committed the tooling before it was ignored (the real-world case).
        self._tooling()
        self._git("init")
        self._git("config", "user.email", "t@t"); self._git("config", "user.name", "t")
        self._git("add", "main.typ", "AGENTS.md", "CLAUDE.md", ".codex/config.toml")
        self._git("commit", "-m", "old")
        self._git("tag", "-a", "v1", "-m", "My named version")   # a real, user-chosen name
        original_tag_target = self._git("rev-parse", "v1^{}").stdout.strip()
        self.assertIn("AGENTS.md", self._git("ls-files").stdout)
        self.assertEqual(self.vcs.status(self.d)["current"], "v1")
        # migrate (as a project-open would): untrack tooling, keep the deck + the version tag
        self.vcs.migrate(self.d)
        tracked = self._git("ls-files").stdout
        self.assertIn("main.typ", tracked)
        self.assertNotIn("AGENTS.md", tracked)
        self.assertNotIn(".codex/config.toml", tracked)
        st = self.vcs.status(self.d)
        self.assertFalse(st["dirty"])                       # cleanup committed, deck unchanged
        self.assertEqual(st["current"], "v1")               # tag carried onto the cleanup commit
        # the tag's NAME/message must survive the move (not be clobbered to a bare "v1")
        subj = self._git("for-each-ref", "refs/tags/v1", "--format=%(contents:subject)").stdout.strip()
        self.assertEqual(subj, "My named version")
        self.assertEqual(self._git("rev-parse", "v1^{}").stdout.strip(), original_tag_target)
        # exactly one tag exists — the move must not spawn a duplicate
        self.assertEqual([v["tag"] for v in self.vcs.list_versions(self.d)], ["v1"])

    def test_migrate_does_not_commit_pre_staged_deck_edits(self):
        self._tooling()
        self._git("init")
        self._git("config", "user.email", "t@t"); self._git("config", "user.name", "t")
        self._git("add", "main.typ", "AGENTS.md", "CLAUDE.md", ".codex/config.toml")
        self._git("commit", "-m", "old")
        self._git("tag", "-a", "v1", "-m", "First version")
        original_tag_target = self._git("rev-parse", "v1^{}").stdout.strip()

        edited = "#slide[staged user edit]\n"
        (self.d / "main.typ").write_text(edited, encoding="utf-8")
        self._git("add", "main.typ")
        self.vcs.migrate(self.d)

        self.assertEqual(self._git("show", "HEAD:main.typ").stdout, "#slide[hello]\n")
        self.assertEqual(self._git("show", ":main.typ").stdout, edited)
        self.assertEqual(self._git("rev-parse", "v1^{}").stdout.strip(), original_tag_target)
        self.assertNotIn("AGENTS.md", self._git("ls-files").stdout)
        self.assertTrue(self.vcs.status(self.d)["dirty"])

    def test_migrate_preserves_uncommitted_deck_edits_as_dirty(self):
        self._tooling()
        self.vcs.save_version(self.d, "first")
        # legacy: pretend AGENTS.md got tracked, and the deck has a real pending edit
        self._git("add", "-f", "AGENTS.md")
        self._git("commit", "-m", "track agents")
        (self.d / "main.typ").write_text("#slide[edited]\n", encoding="utf-8")
        self.vcs.migrate(self.d)
        self.assertNotIn("AGENTS.md", self._git("ls-files").stdout)   # tooling untracked
        self.assertTrue(self.vcs.status(self.d)["dirty"])             # real deck edit still pending


class DoneCommentOrderingRegressionTest(unittest.TestCase):
    def setUp(self):
        import store

        self.store = store
        self._old_override = store._override
        self._tmp = tempfile.TemporaryDirectory()
        store.close()
        store.set_path(str(Path(self._tmp.name) / "comments.db"))

    def tearDown(self):
        self.store.close()
        self.store._override = self._old_override
        self._tmp.cleanup()

    def test_latest_completion_is_first_instead_of_creation_sequence(self):
        with patch.object(self.store, "_now", return_value="2026-07-18T09:00:00"):
            older = self.store.add_comment({"body": "created first"})
        with patch.object(self.store, "_now", return_value="2026-07-18T10:00:00"):
            newer = self.store.add_comment({"body": "created second"})
        with patch.object(self.store, "_now", return_value="2026-07-18T11:00:00"):
            self.store.set_status(older["id"], "done")
        with patch.object(self.store, "_now", return_value="2026-07-18T12:00:00"):
            self.store.set_status(newer["id"], "done")

        done = self.store.list_comments("done")
        self.assertEqual([c["id"] for c in done], [newer["id"], older["id"]])
        self.assertEqual([c["seq"] for c in self.store.list_comments()], [1, 2])


class DockerEntrypointMigrationRegressionTest(unittest.TestCase):
    def test_failed_agent_home_copy_preserves_original_directory(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            workspace = root / "workspace"
            fake_bin = root / "bin"
            source = home / ".claude"
            source.mkdir(parents=True)
            workspace.mkdir()
            fake_bin.mkdir()
            (source / "credentials.json").write_text("keep me\n", encoding="utf-8")
            failing_cp = fake_bin / "cp"
            failing_cp.write_text("#!/bin/sh\nexit 23\n", encoding="utf-8")
            failing_cp.chmod(0o755)
            env = os.environ.copy()
            env.update({
                "HOME": str(home),
                "TCB_BROWSE_ROOT": str(workspace),
                "TCB_STATE_PATH": str(workspace / ".tcb" / "state.json"),
                "PATH": f"{fake_bin}:{env['PATH']}",
            })

            result = subprocess.run(
                ["bash", str(ROOT / "docker-entrypoint.sh")],
                cwd=str(ROOT),
                env=env,
                capture_output=True,
                text=True,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(source.is_symlink())
            self.assertEqual(
                (source / "credentials.json").read_text(encoding="utf-8"),
                "keep me\n",
            )


class ProjectFileOperationsRegressionTest(unittest.TestCase):
    def setUp(self):
        import projects

        self.projects = projects
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "project"
        self.root.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def test_upload_goes_directly_into_requested_folder(self):
        (self.root / "papers").mkdir()
        result = self.projects.store_upload(self.root, "paper.pdf", b"pdf", "papers")
        self.assertEqual(result["path"], "papers/paper.pdf")
        self.assertEqual((self.root / "papers" / "paper.pdf").read_bytes(), b"pdf")
        self.assertFalse((self.root / "paper.pdf").exists())

    def test_upload_collision_keeps_both_files(self):
        papers = self.root / "papers"
        papers.mkdir()
        (papers / "paper.md").write_bytes(b"old")
        result = self.projects.store_upload(self.root, "paper.md", b"new", "papers")
        self.assertTrue(result["collision_renamed"])
        self.assertEqual(result["path"], "papers/paper_1.md")
        self.assertEqual((papers / "paper.md").read_bytes(), b"old")
        self.assertEqual((papers / "paper_1.md").read_bytes(), b"new")

    def test_move_collision_keeps_both_files_instead_of_409(self):
        papers = self.root / "papers"
        papers.mkdir()
        (self.root / "First-Class_Verification_Dialects_for_MLIR.md").write_bytes(b"incoming")
        (papers / "First-Class_Verification_Dialects_for_MLIR.md").write_bytes(b"existing")
        result = self.projects.move_item(
            self.root, "First-Class_Verification_Dialects_for_MLIR.md", "papers"
        )
        self.assertTrue(result["collision_renamed"])
        self.assertEqual(result["path"], "papers/First-Class_Verification_Dialects_for_MLIR_1.md")
        self.assertFalse((self.root / "First-Class_Verification_Dialects_for_MLIR.md").exists())
        self.assertEqual((papers / "First-Class_Verification_Dialects_for_MLIR.md").read_bytes(), b"existing")
        self.assertEqual((papers / "First-Class_Verification_Dialects_for_MLIR_1.md").read_bytes(), b"incoming")

    def test_sibling_prefix_does_not_bypass_project_path_guard(self):
        sibling = self.root.parent / f"{self.root.name}-outside"
        sibling.mkdir()
        with self.assertRaises(PermissionError):
            self.projects._resolve_project_path(self.root, f"../{sibling.name}/escaped.txt")


class RenderTokenRegressionTest(unittest.TestCase):
    def test_page_tokens_are_content_hashes_and_change_with_svg_bytes(self):
        import typst_service

        with tempfile.TemporaryDirectory() as td:
            render_dir = Path(td)
            (render_dir / "page-2.svg").write_text("<svg>two</svg>", encoding="utf-8")
            (render_dir / "page-1.svg").write_text("<svg>one</svg>", encoding="utf-8")

            with patch.object(typst_service.runtime, "render_dir", return_value=render_dir):
                self.assertEqual(typst_service.list_pages(), ["page-1.svg", "page-2.svg"])
                first = typst_service.page_tokens()
                self.assertEqual(
                    first["page-1.svg"],
                    hashlib.sha1(b"<svg>one</svg>").hexdigest()[:12],
                )

                (render_dir / "page-1.svg").write_text("<svg>changed</svg>", encoding="utf-8")
                second = typst_service.page_tokens()

        self.assertNotEqual(first["page-1.svg"], second["page-1.svg"])
        self.assertEqual(first["page-2.svg"], second["page-2.svg"])


class CodexWrapperRegressionTest(unittest.TestCase):
    def test_project_codex_config_is_loaded_and_cleared_by_directory(self):
        wrapper = ROOT / "codex-project-wrapper.sh"
        managed = """# TYPST-COMMENT-BRIDGE:BEGIN (auto-managed - edits here will be overwritten)
[mcp_servers.vibe-typst]
command = "/app/backend/.venv/bin/python"
args = ["/app/backend/mcp_server.py"]

[mcp_servers.vibe-typst.env]
COMMENT_STORE_PATH = "/workspace/example/.slide-comments.db"
TCB_BACKEND_URL = "http://127.0.0.1:8080"
# TYPST-COMMENT-BRIDGE:END
"""

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            project = root / "workspace" / "project"
            outside = root / "workspace"
            project_codex = project / ".codex"
            project_codex.mkdir(parents=True)
            outside.mkdir(exist_ok=True)
            (project_codex / "config.toml").write_text(managed, encoding="utf-8")

            fake = root / "codex-real"
            fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            fake.chmod(0o755)
            env = {**os.environ, "HOME": str(home), "CODEX_REAL": str(fake)}

            subprocess.run(["bash", str(wrapper), "mcp", "list"], cwd=project, env=env, check=True)
            home_cfg = home / ".codex" / "config.toml"
            self.assertIn("mcp_servers.vibe-typst", home_cfg.read_text(encoding="utf-8"))

            subprocess.run(["bash", str(wrapper), "mcp", "list"], cwd=outside, env=env, check=True)
            self.assertNotIn("mcp_servers.vibe-typst", home_cfg.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
