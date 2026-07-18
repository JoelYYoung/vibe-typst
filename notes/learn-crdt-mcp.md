# Learning notes: the CRDT model and how MCP edits reach the room

A from-scratch, math-first account of the shared-document core: the CRDT algebra, the
room's real instruction set, and how every MCP operation (and every human keystroke)
compiles down to it. Companion to `workbook-bugfix-0709.md` (which covers the bugs).

Stack under discussion: `pycrdt` (Python Yjs) `Text` in `backend/docstore.py`, edited by the
browser over y-websocket and by the agent over MCP → HTTP `/api/edit`.

---

## 1. The CRDT math model, from scratch

### 1.1 The goal: Strong Eventual Consistency (SEC)

Replicas `r ∈ R` each hold a state; each applies operations locally, then ships them to the
others. We want:

> **SEC:** any two replicas that have applied the *same set* of operations have equal state —
> regardless of the **order** or **duplication** of delivery.

Two standard ways to guarantee it:

- **State-based (CvRDT):** the state space `(S, ⊔)` is a **join-semilattice** — the merge `⊔`
  is **commutative, associative, idempotent**. Merging a set of states is then its least
  upper bound, which is order-independent. SEC is immediate.
- **Op-based (CmRDT):** concurrent operations **commute**, and redelivery is a no-op
  (**idempotent**). Yjs is essentially this.

Those three adjectives *are* the semilattice laws. Idempotency is what makes a reconnecting
browser's replay of updates harmless (and its absence is what once ballooned a deck 512×).

### 1.2 The sequence (text) CRDT — YATA (what Yjs / pycrdt use)

Model text not as a string but as a set of **elements**, each an immutable-identity character:

```
e = (id, v, oL, oR, d)
```

- `id = (client, clock) ∈ ℕ×ℕ` — globally unique, minted once, **never reused**.
- `v` — the character value.
- `oL, oR` — ids of the left/right neighbor **at insertion time** (its "origins").
- `d ∈ {0,1}` — the **tombstone** bit.

**Insert** `c` between `L, R`: mint `e` with `oL = id(L)`, `oR = id(R)`, fresh `id`.
**Delete** `e`: set `d := 1` (the element is **never physically removed**).

Define a strict total order `<` on ids, computable purely from `(oL, oR, id)`, such that
(i) an element lies between its origins, and (ii) concurrent inserts sharing origins are
broken by a deterministic tiebreak on `id`. YATA's rule makes `<` a **pure function of the
element set** — independent of arrival order.

The **state** is `E` = a set of elements. Merge:

```
E1 ⊔ E2 = { e : e ∈ E1 ∪ E2,  d(e) = d1(e) ∨ d2(e) }
```

i.e. **set union** of elements, **OR** of tombstone bits. Union and OR are each
commutative / associative / idempotent ⇒ `(E, ⊔)` is a join-semilattice ⇒ **SEC**. ∎

The **visible string** is a pure function of `E`:

```
V(E) = [ v(e) : e ∈ E, d(e) = 0, in order < ]
```

Same `E` ⇒ same string on every replica. That is the entire guarantee.

### 1.3 Positions / cursors = relative positions (StickyIndex)

An offset `k` is meaningless across edits. A **relative position** is

```
RelPos = (id, assoc),   assoc ∈ {before, after}
```

resolved to a live offset by counting surviving elements before the target:

```
resolve(RelPos, E) = | { e ∈ E : d(e) = 0,  e < target } |
```

(If the target is tombstoned, take the nearest survivor per `assoc`.) Because ids are
immutable, a `RelPos` never drifts. **This is pycrdt's `StickyIndex`** — what our comment
anchors use (`make_rel_anchors` / `resolve_rel_anchors` in `docstore.py`).

### 1.4 Replica topology — who actually holds a replica (CORE DESIGN)

This is the single most important structural fact about the system, so state it exactly:
**there are TWO CRDT replicas — the browser and the backend "room" — and only they sync.
The MCP server is NOT a replica; it is a remote procedure call into the backend's replica.**

| Party | Holds a `Y.Doc` replica? | How it edits |
|---|---|---|
| **Browser** (CodeMirror) | **Yes** — `new Y.Doc()` in `TypstEditor.jsx`, bound to CM via `y-codemirror.next`, connected by `WebsocketProvider` | applies to its OWN replica first, then syncs |
| **Backend room** (`docstore.py`) | **Yes** — the authoritative pycrdt `Y.Doc`, one per file; also the persistence authority (flushes to `.typ`) | edited in-process |
| **MCP server** (`mcp_server.py`) | **No** | `POST /api/edit` (HTTP RPC); the backend mutates the room replica |

CRDT sync — state vectors + updates, YATA merge — happens on exactly **one axis:
browser ↔ backend room**. The MCP/agent reaches *into* the backend replica over RPC.

**Human keystroke — replica-first, then sync (classic op-based CRDT):**
```
CM change → apply to BROWSER Y.Doc (local, optimistic)
          → Yjs update → y-websocket → merge into BACKEND room replica
          → rebroadcast → other browser replicas merge
```

**MCP / agent edit — no MCP replica; direct mutation of the server replica:**
```
apply_edits(...) → HTTP → backend
                 → resolve selector → del/insert on the BACKEND room replica (one txn)
                 → the txn's Yjs update is broadcast → BROWSER replica merges
                 → debounced flush of str(text) → .typ
```
There is **no "MCP replica" that is edited then reconciled.** The agent's edit is transformed
into `del`/`insert` exactly once, at the backend replica, and only then flows to the browser
as an ordinary Yjs update.

**Why it's built this way.** A separate OS process (the MCP server driving Codex/Claude over
stdio) cannot touch the in-memory room, and giving it its own `Y.Doc` would add a THIRD
replica to keep synced and persist. So it deliberately stays a thin RPC client into the one
process that owns the room.

**Consequences (they explain the whole bug history):**
- Consecutive MCP edits need no cross-call CRDT reconciliation — they are just serialized
  transactions on the single server replica (so `apply_edits` resolving against current text
  is sufficient).
- The only genuine concurrent-merge case (human typing while the agent edits) is resolved by
  YATA on the browser↔server axis.
- The "split-brain room" bug was NOT two replicas of one document diverging — it was two
  DISTINCT rooms (different keys) backing one file, with the browser synced to one and the MCP
  writing the other. An identity/routing bug, not a CRDT-merge failure. Unifying the room key
  makes MCP edits land in the very replica the browser syncs with, so the agent's change
  reaches the editor as an ordinary Yjs update — which is also why the per-edit editor remount
  became unnecessary (see workbook Follow-up 5).

---

## 2. The room's actual instruction set

The punchline that unifies cursor edits and MCP edits: **pycrdt `Text` exposes exactly two
mutating primitives**, addressed by **UTF-8 byte offsets**:

```
insert(b, s)        del[b1:b2]
```

- `insert(b, s)`: for each char in `s`, find the element currently at byte-boundary `b`,
  read its left neighbor `L`, and mint a new element with `oL = id(L)`. The offset is used
  **only at apply time** to pick the neighbor; the resulting element is identity-anchored
  forever after.
- `del[b1:b2]`: set `d := 1` on every element currently spanning that byte range.

**Both a human keystroke and an MCP edit compile to these two calls.** `y-codemirror.next`
turns a CodeMirror change `(from, to, insert)` into `ytext.delete(from, to−from)` +
`ytext.insert(from, insert)` — the *same* interface the MCP path uses. So "how does a cursor
edit become a CRDT edit?" — it becomes an offset-addressed `del`+`insert`, which YATA turns
into element-id ops. There is no separate mechanism for the agent.

> pycrdt `Text` is indexed by **UTF-8 bytes**, while callers count **code points**. The
> backend converts with `_cp_to_byte` before touching the Text — a multibyte char (—, CJK,
> emoji) before the edit point otherwise lands the edit mid-character. See the header comment
> in `docstore.py`.

---

## 3. Every MCP operation → CRDT edits

**Read-only** (no CRDT mutation): `get_document`, `find_in_document`, `get_pending_comments`,
`get_comment`, `list_all_comments`, `get_transcripts`.

**Comment anchors** (no text mutation): `make_rel_anchors` → `text.sticky_index(b, assoc)`;
`resolve_rel_anchors` → `StickyIndex.get_index`.

**The primary tool is `apply_edits(edits, base_rev)`** — an atomic batch over a tagged-union
selector (`anchor` / `lines` / `range`), with an optional per-edit `expect` compare-and-swap and
a monotonic per-room `rev`. It resolves every selector against one snapshot, rejects overlaps,
and applies the whole batch (highest offset first) in a single transaction — or refuses it whole
with the live neighborhood. The single-edit tools below are thin sugar over it and emit the same
CRDT primitives; each resolves a **code-point** range, converts to **byte** offsets, then emits
`del`+`insert` inside one transaction:

| MCP tool | how the offset `b` is found | CRDT emission |
|---|---|---|
| `replace_anchor(a, s)` | `b1 = find(a)`, `b2 = b1 + |a|` | `del[b1:b2]; insert(b1, s)` |
| `insert_before_anchor(a, t)` | `b = find(a)` | `insert(b, t)` |
| `insert_after_anchor(a, t)` | `b = find(a) + |a|` | `insert(b, t)` |
| `replace_range(f, t, s)` | given (code points) | `del[f:t]; insert(f, s)` |
| `insert_text(at, t)` | given | `insert(at, t)` |
| `replace_lines(i, j, s)` | line-start table → `b1, b2` | `del[b1:b2]; insert(b1, s)` |
| `insert_at_line(i, t)` | line-start table → `b` | `insert(b, t)` |

The transaction produces one Yjs binary update (new elements + tombstones), which is
(a) broadcast to browsers and merged by `⊔`, and (b) debounced-flushed as `V(E)` to the `.typ`.

So the room's entire write API is just `{ insert(offset, str), del(offset, len) }` within a
transaction; every MCP write is a thin front-end that computes the offset.

---

## 4. Worked example (end-to-end, with concurrency)

Doc `"abc"`: elements `a=(1,0), b=(1,1), c=(1,2)`, order `a < b < c`, origins
`a.oL=⊥, b.oL=a, c.oL=b`.

**MCP `replace_anchor("b", "XY")`:**

1. `s = "abc"`, find `"b"` at code-point 1 → byte range `[1, 2)`.
2. Transaction:
   - `del[1:2]` → `d(b) := 1`.
   - `insert(1, "XY")` at the boundary between `a` and `b`: mint `X=(1,3), oL=a`;
     `Y=(1,4), oL=X`.
3. Order: `a < X < Y < b(d=1) < c`. Visible `V = "aXYc"`.

**Concurrent human insert `"Z"` at offset 0** (element `Z=(2,0), oL=⊥`), delivered in either
order. Merge = union:

```
Z < a < X < Y < b(d=1) < c    ⇒    V = "ZaXYc"
```

Both replicas converge to the same string **whichever order the two edits arrive**
(commutativity), and the agent's `XY` stayed glued between `a` and `c` — **no drift**, because
after apply it is anchored to element ids, not to offset 1.

**Comment anchored to `b`** as `RelPos = (id=b, after)`: after `b` is tombstoned, `resolve`
returns the offset of the nearest survivor (`c`), deterministically on every replica.

---

## 5. One-line summary

The room is a join-semilattice of identity-stamped characters with tombstones; the only
mutations are offset-addressed `insert` / `del`; offsets are consumed *at apply time* to pick
a neighbor and are never stored; the human's cursor edits and all MCP edits are the *same*
`insert` / `del` calls; and durable references (comments) skip offsets entirely via
`StickyIndex`.

## 6. Addressing schemes, compared (why we use a hybrid)

The CRDT is always the substrate. What differs per edit is *how it names a location*:

| scheme | reference is… | drifts under concurrent edits? | agent ergonomics | used for |
|---|---|---|---|---|
| line number | a position | **yes** | best (no escaping) | `replace_lines` / `insert_at_line` (bulk rewrites) |
| content anchor | content match | no, if text unchanged & unique | poor (escaping / ambiguity) | `replace_anchor` / `insert_*_anchor` (surgical) |
| CRDT `StickyIndex` | character identity | **never** | unusable by an LLM (it reasons in text) | comment anchors |

Line numbers are the most *token-efficient* for the agent but the least safe in a **live
shared** doc (the human can shift lines between the agent's read and its write). VSCode's
line-number MCP is safe only because nothing else edits the file mid-edit. Recommended
direction: make line edits the agent default but add an `expect` compare-and-swap guard
(apply only if the target lines still match), keep anchors as fallback, keep `StickyIndex`
for comments.
