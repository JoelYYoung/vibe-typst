import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'

test('Presenter keeps text typed after save while refreshed saved text becomes its baseline', async () => {
  const presenterState = await import('../src/presenterState.js').catch(() => ({}))
  assert.equal(typeof presenterState.reconcilePresenterDraft, 'function')
  assert.equal(typeof presenterState.finishPresenterDraftSave, 'function')

  let script = presenterState.reconcilePresenterDraft({}, 1, 'original', 'generation-A')
  script = { ...script, draft: 'A' }
  const saveRequest = { page: 1, generation: script.generation, text: script.draft }
  script = { ...script, draft: 'B' }
  script = presenterState.finishPresenterDraftSave(script, saveRequest)
  script = presenterState.reconcilePresenterDraft(script, 1, 'A', 'generation-A')

  assert.deepEqual(script, { page: 1, generation: 'generation-A', draft: 'B', base: 'A' })
  const presenter = fs.readFileSync(new URL('../src/Presenter.jsx', import.meta.url), 'utf8')
  assert.match(presenter, /finishPresenterDraftSave/)
  assert.match(presenter, /reconcilePresenterDraft/)
})

test('Presenter resets an old dirty draft when a replacement changes PDF generation', async () => {
  const { reconcilePresenterDraft } = await import('../src/presenterState.js')
  let script = reconcilePresenterDraft({}, 1, 'old transcript', 'generation-A')
  script = { ...script, draft: 'old dirty draft' }

  script = reconcilePresenterDraft(script, 1, 'replacement transcript', 'generation-B')

  assert.deepEqual(script, {
    page: 1,
    generation: 'generation-B',
    draft: 'replacement transcript',
    base: 'replacement transcript',
  })
  const presenter = fs.readFileSync(new URL('../src/Presenter.jsx', import.meta.url), 'utf8')
  assert.match(presenter, /reconcilePresenterDraft\(previous, page, info\.note, generation\)/)
})

test('Presenter ignores a save completion captured for a replaced PDF generation', async () => {
  const { finishPresenterDraftSave, reconcilePresenterDraft } = await import('../src/presenterState.js')
  const oldSave = { page: 1, generation: 'generation-A', text: 'saved old transcript' }
  let script = reconcilePresenterDraft({}, 1, 'old transcript', 'generation-A')
  script = reconcilePresenterDraft(script, 1, 'replacement transcript', 'generation-B')

  script = finishPresenterDraftSave(script, oldSave)

  assert.deepEqual(script, {
    page: 1,
    generation: 'generation-B',
    draft: 'replacement transcript',
    base: 'replacement transcript',
  })
})

test('Presenter render identity changes with generation or current page content', async () => {
  const presenterState = await import('../src/presenterState.js')
  assert.equal(typeof presenterState.presenterRenderIdentity, 'function')
  const initial = presenterState.presenterRenderIdentity(
    1, ['page.png'], { 'page.png': 'token-A' }, 'generation-A'
  )

  assert.equal(
    presenterState.presenterRenderIdentity(1, ['page.png'], { 'page.png': 'token-A' }, 'generation-A'),
    initial,
  )
  assert.notEqual(
    presenterState.presenterRenderIdentity(1, ['page.png'], { 'page.png': 'token-A' }, 'generation-B'),
    initial,
  )
  assert.notEqual(
    presenterState.presenterRenderIdentity(1, ['page.png'], { 'page.png': 'token-B' }, 'generation-A'),
    initial,
  )
  const presenter = fs.readFileSync(new URL('../src/Presenter.jsx', import.meta.url), 'utf8')
  assert.match(presenter, /const renderIdentity = presenterRenderIdentity\(page, pages, tokens, generation\)/)
  assert.match(presenter, /useEffect\(\(\) => \{\s*clearPointer\(\)\s*\}, \[renderIdentity, clearPointer\]\)/)
})
