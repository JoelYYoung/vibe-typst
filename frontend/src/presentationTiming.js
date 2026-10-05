// Use words for alphabetic scripts and characters for CJK scripts. This is a
// proportional pacing guide, not an estimate of a speaker's actual reading speed.
export function transcriptLength(text = '') {
  let characters = 0
  const words = String(text).replace(/[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}\p{Script=Hangul}]/gu, () => {
    characters++
    return ' '
  }).match(/[\p{L}\p{N}]+(?:['’-][\p{L}\p{N}]+)*/gu)
  return characters + (words?.length || 0)
}

export function allocateTranscriptTime(transcripts, targetMinutes) {
  const minutes = Number(targetMinutes)
  const weights = transcripts.map(transcriptLength)
  const total = weights.reduce((sum, weight) => sum + weight, 0)
  if (!Number.isFinite(minutes) || minutes < .5 || minutes > 240 || !total) return weights.map(() => null)
  return weights.map(weight => weight ? minutes * 60 * weight / total : null)
}

export function recordingDurations(takes, pageCount, page, liveSeconds = null) {
  const duration = (take) => Number.isFinite(take?.duration) && take.duration > 0 ? take.duration : 0
  const savedPage = duration(takes[page])
  const savedTotal = Array.from({ length: pageCount }, (_, index) => duration(takes[index + 1]))
    .reduce((sum, seconds) => sum + seconds, 0)
  const live = Number.isFinite(liveSeconds) && liveSeconds >= 0
  return { page: live ? liveSeconds : savedPage, total: live ? savedTotal - savedPage + liveSeconds : savedTotal }
}
