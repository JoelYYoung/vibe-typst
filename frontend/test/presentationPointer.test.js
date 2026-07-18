import test from 'node:test'
import assert from 'node:assert/strict'
import {
  clientToSlidePoint,
  containRect,
  sanitizePresentationPointer,
  slidePointToPixels,
} from '../src/presentationPointer.js'

test('containRect centers a widescreen slide inside a square projection', () => {
  assert.deepEqual(containRect(1000, 1000, 1600, 900), {
    left: 0,
    top: 218.75,
    width: 1000,
    height: 562.5,
  })
})

test('presenter coordinates ignore letterbox clicks and normalize slide clicks', () => {
  const rect = { left: 100, top: 50, width: 1000, height: 1000 }
  assert.equal(clientToSlidePoint(600, 100, rect, 1600, 900), null)
  assert.deepEqual(clientToSlidePoint(600, 550, rect, 1600, 900), { x: 0.5, y: 0.5 })
})

test('projection coordinates preserve the indicated point across aspect ratios', () => {
  assert.deepEqual(
    slidePointToPixels({ page: 2, x: 0.25, y: 0.8 }, 1000, 1000, 1600, 900),
    { left: 250, top: 668.75 },
  )
})

test('broadcast pointer input is validated and clamped', () => {
  assert.equal(sanitizePresentationPointer({ page: 0, x: 0.5, y: 0.5 }), null)
  assert.equal(sanitizePresentationPointer({ page: 1, x: Number.NaN, y: 0.5 }), null)
  assert.deepEqual(sanitizePresentationPointer({ page: 3, x: -1, y: 2 }), { page: 3, x: 0, y: 1 })
})
