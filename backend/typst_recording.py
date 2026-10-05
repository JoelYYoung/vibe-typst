"""Stable recording anchors travel with a Touying slide's source opener line."""
import asyncio
import json
import re
import uuid
from collections import Counter

import docstore
import notes
import presentation_recording as recording
import runtime

_MARKER = re.compile(r"// vibe-typst-recording: ([a-f0-9]{32})\s*$")
_locks = {}
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
        if (i == 0 or source[i - 1] == '\n') and notes._OPENER.match(source[i:]):
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


def page_bindings(document, project, page_count):
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
    cache_key = (str(document), source)
    if cache_key not in _cache:
        rows = notes.pdfpc_pages(document, project)
        if document.read_text(encoding='utf-8') != source:
            raise ValueError('slides are recompiling; wait for the preview to finish, then retry')
        ids = []
        for row in rows:
            label = str(row.get('label', ''))
            if not label.isdigit() or not 1 <= int(label) <= len(entries):
                raise ValueError('cannot match this rendered page to a source slide; use explicit Touying slide openers')
            identity = entries[int(label) - 1][1]
            ids.append(f"{identity}-{row.get('overlay') or 0}" if identity else None)
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
