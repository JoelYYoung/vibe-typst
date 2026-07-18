const finitePositive = (value) => Number.isFinite(value) && value > 0

export function containRect(containerWidth, containerHeight, contentWidth, contentHeight) {
  if (![containerWidth, containerHeight, contentWidth, contentHeight].every(finitePositive)) return null
  const scale = Math.min(containerWidth / contentWidth, containerHeight / contentHeight)
  const width = contentWidth * scale
  const height = contentHeight * scale
  return {
    left: (containerWidth - width) / 2,
    top: (containerHeight - height) / 2,
    width,
    height,
  }
}

// Convert a browser pointer position to slide-relative coordinates. Clicks in the
// letterbox around the slide are deliberately ignored.
export function clientToSlidePoint(clientX, clientY, containerRect, contentWidth, contentHeight) {
  if (!containerRect) return null
  const slide = containRect(containerRect.width, containerRect.height, contentWidth, contentHeight)
  if (!slide) return null
  const x = clientX - containerRect.left - slide.left
  const y = clientY - containerRect.top - slide.top
  if (x < 0 || y < 0 || x > slide.width || y > slide.height) return null
  return { x: x / slide.width, y: y / slide.height }
}

export function sanitizePresentationPointer(value) {
  if (!value || !Number.isInteger(value.page) || value.page < 1
      || !Number.isFinite(value.x) || !Number.isFinite(value.y)) return null
  return {
    page: value.page,
    x: Math.min(1, Math.max(0, value.x)),
    y: Math.min(1, Math.max(0, value.y)),
  }
}

// Map a slide-relative point into the contain-fitted image inside a projection viewport.
export function slidePointToPixels(pointer, containerWidth, containerHeight, contentWidth, contentHeight) {
  const clean = sanitizePresentationPointer(pointer)
  const slide = containRect(containerWidth, containerHeight, contentWidth, contentHeight)
  if (!clean || !slide) return null
  return {
    left: slide.left + clean.x * slide.width,
    top: slide.top + clean.y * slide.height,
  }
}
