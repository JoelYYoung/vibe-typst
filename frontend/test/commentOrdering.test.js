import test from 'node:test'
import assert from 'node:assert/strict'
import { filterAndSortComments } from '../src/commentOrdering.js'

test('done comments are ordered by completion time rather than creation sequence', () => {
  const comments = [
    { id: 'old', seq: 1, status: 'done', done_at: '2026-07-18T11:00:00' },
    { id: 'new', seq: 2, status: 'done', done_at: '2026-07-18T12:00:00' },
    { id: 'pending', seq: 3, status: 'pending', created_at: '2026-07-18T13:00:00' },
  ]
  assert.deepEqual(filterAndSortComments(comments, 'done').map((c) => c.id), ['new', 'old'])
})

test('legacy done comments fall back to updated time and then sequence', () => {
  const comments = [
    { id: 'first', seq: 1, status: 'done', updated_at: '2026-07-18T11:00:00' },
    { id: 'second', seq: 2, status: 'done', updated_at: '2026-07-18T11:00:00' },
  ]
  assert.deepEqual(filterAndSortComments(comments, 'done').map((c) => c.id), ['second', 'first'])
})

test('pending and all views retain their existing sequence order', () => {
  const comments = [
    { id: 'first', seq: 1, status: 'pending' },
    { id: 'second', seq: 2, status: 'done', done_at: '2026-07-18T12:00:00' },
  ]
  assert.deepEqual(filterAndSortComments(comments, 'pending').map((c) => c.id), ['first'])
  assert.deepEqual(filterAndSortComments(comments, 'all').map((c) => c.id), ['first', 'second'])
})
