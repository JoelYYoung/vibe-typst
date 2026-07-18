import test from 'node:test'
import assert from 'node:assert/strict'
import { canSaveVersion } from '../src/versioning.js'

test('a project with no repository can create its first version', () => {
  assert.equal(canSaveVersion({ initialized: false, dirty: false, current: null }, 0), true)
})

test('a clean repository with no tags can recreate its first version', () => {
  assert.equal(canSaveVersion({ initialized: true, dirty: false, current: null }, 0), true)
})

test('a clean repository with a saved version cannot create a redundant version', () => {
  assert.equal(canSaveVersion({ initialized: true, dirty: false, current: 'v1' }, 1), false)
})

test('changes after a saved version remain saveable', () => {
  assert.equal(canSaveVersion({ initialized: true, dirty: true, current: null }, 1), true)
})
