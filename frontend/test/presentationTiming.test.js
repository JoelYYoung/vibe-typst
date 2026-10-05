import { test } from 'node:test'
import assert from 'node:assert/strict'
import { transcriptLength, allocateTranscriptTime, recordingDurations } from '../src/presentationTiming.js'

test('pacing counts mixed CJK characters and alphabetic words without punctuation', () => {
  assert.equal(transcriptLength('Hello 世界, café!'), 4)
  assert.equal(transcriptLength("Don't re-record ..."), 2)
  assert.equal(transcriptLength('  …! 😀 '), 0)
})

test('page budgets exhaust the target proportionally without inventing notes', () => {
  const allocation = allocateTranscriptTime(['one', 'two three', ''], 6)
  assert.deepEqual(allocation, [120, 240, null])
  assert.deepEqual(allocateTranscriptTime(['', ''], 6), [null, null])
  for (const invalid of ['', 'abc', -2, Infinity, 300]) assert.deepEqual(allocateTranscriptTime(['one'], invalid), [null])
})

test('live rerecording replaces the old page in totals and ignores removed pages', () => {
  const takes = { 1: { duration: 30 }, 2: { duration: 60 }, 3: { duration: 90 } }
  assert.deepEqual(recordingDurations(takes, 2, 1), { page: 30, total: 90 })
  assert.deepEqual(recordingDurations(takes, 2, 1, 10), { page: 10, total: 70 })
  assert.deepEqual(recordingDurations({ 2: takes[2] }, 2, 1), { page: 0, total: 60 })
})
