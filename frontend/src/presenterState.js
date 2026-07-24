const savedText = (value) => typeof value === 'string' ? value : ''

export function reconcilePresenterDraft(previous = {}, page, saved, generation) {
  const base = savedText(saved)
  const generationChanged = generation !== undefined && previous.generation !== generation
  if (previous.page !== page || generationChanged) {
    return {
      page,
      ...(generation === undefined ? {} : { generation }),
      draft: base,
      base,
    }
  }
  const previousBase = savedText(previous.base)
  const draft = savedText(previous.draft)
  return {
    page,
    ...(generation === undefined ? {} : { generation }),
    draft: draft === previousBase ? base : draft,
    base,
  }
}

export function finishPresenterDraftSave(previous = {}, request = {}) {
  const wrongGeneration = request.generation !== undefined && previous.generation !== request.generation
  if (previous.page !== request.page || wrongGeneration || typeof request.text !== 'string') return previous
  return { ...previous, base: request.text }
}

export function presenterRenderIdentity(page, pages = [], tokens = {}, generation) {
  const name = Array.isArray(pages) ? pages[page - 1] : undefined
  const token = name && tokens && typeof tokens === 'object' ? tokens[name] : undefined
  return JSON.stringify([page, generation || '', name || '', token || ''])
}
