import test from 'node:test'
import assert from 'node:assert/strict'
import {
  TOKEN_EXPIRIES,
  TOKEN_PRESETS,
  displayTokenPrefix,
  expiryTimestamp,
} from '../src/tokenManagement.js'

test('token expiry choices resolve to exact UTC seconds', () => {
  const now = Date.UTC(2026, 6, 28)
  assert.equal(
    expiryTimestamp('30d', now),
    (now + 30 * 86400_000) / 1000,
  )
  assert.equal(
    expiryTimestamp('90d', now),
    (now + 90 * 86400_000) / 1000,
  )
  assert.equal(
    expiryTimestamp('1y', now),
    (now + 365 * 86400_000) / 1000,
  )
  assert.equal(expiryTimestamp('never', now), null)
  assert.throws(() => expiryTimestamp('other', now), /expiry/)
})

test('only viewer and editor presets are exposed', () => {
  assert.deepEqual(
    TOKEN_PRESETS.map((item) => item.value),
    ['viewer', 'editor'],
  )
  assert.deepEqual(
    TOKEN_EXPIRIES.map((item) => item.value),
    ['30d', '90d', '1y', 'never'],
  )
})

test('token prefixes are bounded and never fall back to the secret', () => {
  assert.equal(displayTokenPrefix({ token_prefix: 'vbt_abc123' }), 'vbt_abc123…')
  assert.equal(displayTokenPrefix({ token: 'vbt_secret' }), '')
  assert.equal(displayTokenPrefix(null), '')
})
