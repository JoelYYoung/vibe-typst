import React from 'react'

export function shortPath(path, threshold = 5, keep = 3) {
  if (!path) return ''
  const segments = path.split('/').filter(Boolean)
  if (segments.length <= threshold) return path
  return `…/${segments.slice(-keep).join('/')}`
}

export function TerminalIcon({ size = 16 }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      style={{ display: 'block' }}
      aria-hidden="true"
    >
      <rect x="2" y="4" width="20" height="16" rx="2" />
      <path d="M6 9l3 3-3 3" />
      <path d="M13 15h4" />
    </svg>
  )
}
