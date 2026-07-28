export const TOKEN_PRESETS = [
  {
    value: 'viewer',
    label: 'Viewer',
    description: 'Read projects, slides, files, transcripts, and comments.',
  },
  {
    value: 'editor',
    label: 'Editor',
    description: 'Viewer access plus project, file, document, transcript, and comment changes.',
  },
]

export const TOKEN_EXPIRIES = [
  { value: '30d', label: '30 days', days: 30 },
  { value: '90d', label: '90 days', days: 90 },
  { value: '1y', label: '1 year', days: 365 },
  { value: 'never', label: 'No expiry', days: null },
]

export function expiryTimestamp(choice, nowMs = Date.now()) {
  const option = TOKEN_EXPIRIES.find((item) => item.value === choice)
  if (!option) throw new Error('invalid token expiry')
  return option.days === null
    ? null
    : (nowMs + option.days * 86400_000) / 1000
}

export function displayTokenPrefix(token) {
  const prefix = token && typeof token.token_prefix === 'string'
    ? token.token_prefix
    : ''
  return prefix ? `${prefix.slice(0, 80)}…` : ''
}
