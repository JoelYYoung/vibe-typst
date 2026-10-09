"""Stable recording anchors travel with a Touying slide's source opener line."""
import asyncio
import json
import os
import re
import subprocess
import tempfile
import threading
import uuid
from collections import Counter
from pathlib import Path

import docstore
import presentation_recording as recording
import runtime

_MARKER = re.compile(r"// vibe-typst-recording: ([a-f0-9]{32})\s*$")
_OPENER = re.compile(r"^[ \t]*#(slide|centered-slide|focus-slide|title-slide)\b")
_locks = {}
_binding_locks = {}
_cache = {}


def _openers(source):
    i, size = 0, len(source)
    while i < size:
        if source.startswith('//', i):
            end = source.find('\n', i + 2)
            i = size if end < 0 else end + 1
            continue
        if source.startswith('/*', i):
            i += 2
            depth = 1
            while i < size and depth:
                if source.startswith('/*', i):
                    depth += 1
                    i += 2
                elif source.startswith('*/', i):
                    depth -= 1
                    i += 2
                else:
                    i += 1
            continue
        if source[i] in ('"', '`'):
            delimiter = source[i]
            end = i + 1
            if delimiter == '`':
                while end < size and source[end] == '`':
                    end += 1
                delimiter = source[i:end]
                closing = source.find(delimiter, end)
                i = size if closing < 0 else closing + len(delimiter)
            else:
                i = end
                while i < size:
                    if source[i] == '\\':
                        i += 2
                    elif source[i] == '"':
                        i += 1
                        break
                    else:
                        i += 1
            continue
        if (i == 0 or source[i - 1] == '\n') and _OPENER.match(source[i:]):
            end = source.find('\n', i)
            yield i, source[i:size if end < 0 else end]
        i += 2 if source[i] == '\\' else 1


def anchors(source):
    return [(line, match.group(1) if (match := _MARKER.search(line)) else None)
            for _, line in _openers(source)]


def anchor_edits(source):
    edits, seen = [], set()
    for offset, line in _openers(source):
        if len(list(_openers(line + '\n#slide[]'))) != 2:
            raise ValueError('put multiline strings or raw examples below the slide opener before recording')
        match = _MARKER.search(line)
        identity = match.group(1) if match else None
        if identity and identity not in seen:
            seen.add(identity)
            continue
        identity = uuid.uuid4().hex
        seen.add(identity)
        if match:
            replacement = _MARKER.sub('// vibe-typst-recording: ' + identity, line)
            side = 'in'
        else:
            replacement = ' // vibe-typst-recording: ' + identity
            side = 'after'
        occurrence = source[:offset].count(line) + 1
        anchor = line
        # The editor rejects occurrence=1 for a repeated anchor. Include its
        # preceding source to identify the first opener without positional edits.
        if occurrence == 1 and source.count(line) > 1:
            anchor = source[:offset + len(line)]
            if side == 'in':
                replacement = source[:offset] + replacement
        edits.append({'selector': {'by': 'anchor', 'text': anchor, 'occurrence': occurrence, 'side': side}, 'text': replacement})
    return edits


def _query_pages(document, project, source):
    """Label each call's page preamble in a disposable, otherwise identical deck.

    Touying's displayed counter is neither a source index nor a physical page ID:
    titles can freeze it and overflowing bodies repeat the header on extra pages.
    Evaluate the original wrapper with its original config/preamble, adding only
    invisible metadata. The UUID comments remain the sole persisted source edits.
    """
    instrumented = source
    for offset, line in reversed(list(_openers(source))):
        opener = _OPENER.match(line)
        name = opener.group(1)
        marker = _MARKER.search(line)
        identity = 'none' if marker is None else json.dumps(marker.group(1))
        wrapper = '''((..args) => touying-slide-wrapper(self => {
 let named = args.named()
 let config = named.remove("config", default: (:))
 let prior = utils.merge-dicts(self, config).at("page-preamble", default: none)
 let hook = config-common(page-preamble: me => {
   utils.call-or-display(me, prior)
   context [#metadata((id: %s, overlay: me.subslide - 1, page: here().page())) <vibe-typst-recording-page>]
 })
 (%s(..args.pos(), ..named, config: utils.merge-dicts(config, hook)).value.fn)(self)
}))''' % (identity, name)
        start = offset + opener.start(1)
        end = offset + opener.end(1)
        instrumented = instrumented[:start] + wrapper + instrumented[end:]
    # Keep relative imports/images relative to the original main file, including
    # documents in subdirectories. Never replace or rewrite the user's document.
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=document.parent,
                                     prefix='.recording-query-', suffix='.typ', delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(instrumented)
    try:
        result = subprocess.run(
            ['typst', 'query', '--root', str(project), str(temporary),
             '<vibe-typst-recording-page>', '--field', 'value'],
            capture_output=True, text=True, cwd=project, timeout=120,
            env={**os.environ, 'RAYON_NUM_THREADS': '1'})
        if result.returncode:
            raise ValueError('could not resolve source slide bindings; check the slide preview and retry')
        try:
            rows = json.loads(result.stdout)
            if not isinstance(rows, list):
                raise ValueError()
            return rows
        except ValueError as exc:
            raise ValueError('could not read source slide bindings; retry recording mode') from exc
    except subprocess.TimeoutExpired as exc:
        raise ValueError('source slide binding timed out; retry recording mode') from exc
    finally:
        temporary.unlink(missing_ok=True)


def _bindings_from_pages(rows, identities, page_count):
    pages = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('invalid source slide binding')
        page, identity, overlay = row.get('page'), row.get('id'), row.get('overlay')
        if (type(page) is not int or not 1 <= page <= page_count or page in pages
                or identity is not None and identity not in identities
                or type(overlay) is not int or overlay < 0):
            raise ValueError('ambiguous source slide binding; check the slide preview')
        pages[page] = (identity, overlay)
    if set(pages) != set(range(1, page_count + 1)):
        raise ValueError('some rendered pages have no source slide binding; use explicit Touying slides')
    ids, counts = [], Counter()
    for page in range(1, page_count + 1):
        identity, overlay = pages[page]
        occurrence = counts[identity, overlay]
        counts[identity, overlay] += 1
        # Preserve UUID-overlay for existing decks; overflow gets a local suffix,
        # never an absolute page number that could shift when another slide moves.
        ids.append(None if identity is None else f'{identity}-{overlay}' +
                   (f'-{occurrence}' if occurrence else ''))
    return ids


def page_bindings(document, project, page_count):
    with _binding_locks.setdefault(str(document), threading.Lock()):
        return _page_bindings(document, project, page_count)


def _page_bindings(document, project, page_count):
    source = document.read_text(encoding='utf-8')
    entries = anchors(source)
    if not any(identity for _, identity in entries):
        return [None] * page_count
    snapshot = runtime.render_dir(document) / 'render-source.json'
    try:
        rendered = json.loads(snapshot.read_text())
    except (OSError, ValueError):
        raise ValueError('slides are recompiling; wait for the preview to finish, then retry')
    if rendered.get('source') != source or rendered.get('pages') != page_count:
        raise ValueError('slides are recompiling; wait for the preview to finish, then retry')
    identities = [identity for _, identity in entries if identity]
    if len(set(identities)) != len(identities):
        raise ValueError('duplicated recording anchor; reopen recording mode to prepare this deck')
    cache_key = (str(document), source, rendered.get('stamp'))
    if cache_key not in _cache:
        rows = _query_pages(document, project, source)
        if (document.read_text(encoding='utf-8') != source
                or json.loads(snapshot.read_text()) != rendered):
            raise ValueError('slides are recompiling; wait for the preview to finish, then retry')
        ids = _bindings_from_pages(rows, identities, page_count)
        if len([identity for identity in ids if identity]) != len(set(identity for identity in ids if identity)):
            raise ValueError('ambiguous slide recording anchors')
        # Keep only the latest snapshot for each document.
        for key in list(_cache):
            if key[0] == str(document):
                del _cache[key]
        _cache[cache_key] = ids
    ids = _cache[cache_key]
    if len(ids) != page_count:
        raise ValueError('slides are recompiling; wait for the preview to finish, then retry')
    return ids


def migrate_legacy(root, before, after):
    """Attach old page takes only when the current content proves their identity."""
    with recording.locked(root):
        old = []
        for path in root.glob('page-*.json'):
            match = re.fullmatch(r'page-([1-9][0-9]*)\.json', path.name)
            if match:
                data = recording._read_take(root, int(match.group(1)))
                if data:
                    old.append((path, data))
        token_counts = Counter(slide['token'] for slide in before)
        old_counts = Counter(data['token'] for _, data in old)
        for index, slide in enumerate(after):
            identity = slide.get('recording_id')
            if not identity or recording._read_take(root, index + 1, identity):
                continue
            token = before[index]['token']
            matches = [(path, data) for path, data in old if data['token'] == token]
            if token_counts[token] != 1 or old_counts[token] != 1 or len(matches) != 1:
                continue
            path, data = matches[0]
            recording.media_path(root, data)
            migrated = {**data, 'recording_id': identity, 'page': index + 1, 'name': slide['name'], 'token': slide['token']}
            recording.write_json(recording._take_file(root, index + 1, identity), migrated)
            path.unlink()


async def prepare(document, project, resolve):
    lock = _locks.setdefault(str(document), asyncio.Lock())
    async with lock:
        await docstore.ensure_room(document)
        await docstore.flush_now(document)
        async def current_render():
            for attempt in range(40):
                try:
                    return await asyncio.to_thread(resolve)
                except ValueError as exc:
                    if not str(exc).startswith('slides are recompiling') or attempt == 39:
                        raise
                    await asyncio.sleep(.2)
        # Duplicated anchors (a copied slide) are repaired before resolving bindings.
        source = docstore.get_text(document) or document.read_text(encoding='utf-8')
        if not anchors(source):
            raise ValueError('recording requires explicit Touying #slide or #title-slide openers in main.typ')
        duplicate = len([identity for _, identity in anchors(source) if identity]) != len(set(identity for _, identity in anchors(source) if identity))
        if duplicate:
            root, before = recording.directory(project, document), []
        else:
            root, before = await current_render()
        edits = anchor_edits(source)
        if edits:
            result = await docstore.apply_edits(edits, document)
            if not result.get('ok'):
                raise ValueError('the source changed while preparing recordings; retry')
            await docstore.flush_now(document)
        _, after = await current_render()
        if any(not slide.get('recording_id') for slide in after):
            raise ValueError('some rendered slides have no source recording anchor')
        if before and len(before) == len(after):
            migrate_legacy(root, before, after)
