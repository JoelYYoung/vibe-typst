import React from 'react'

const paths = {
  mic: <><rect x="9" y="2" width="6" height="12" rx="3" /><path d="M5 10v2a7 7 0 0 0 14 0v-2M12 19v3M8 22h8" /></>,
  record: <circle cx="12" cy="12" r="6" fill="currentColor" stroke="none" />,
  stop: <rect x="6" y="6" width="12" height="12" rx="2" fill="currentColor" />,
  refresh: <><path d="M20 4v5h-5M4 20v-5h5" /><path d="M4 10a8 8 0 0 1 13.7-3.7L20 9M20 14a8 8 0 0 1-13.7 3.7L4 15" /></>,
  play: <path d="m8 5 11 7-11 7Z" />,
  close: <path d="m6 6 12 12M18 6 6 18" />,
  trash: <><path d="M3 6h18M9 6V3h6v3M6 6l1 15h10l1-15M10 10v7M14 10v7" /></>,
  export: <><path d="M14 3H5v18h14v-8M13 11l8-8M15 3h6v6" /></>,
  download: <path d="M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5" />,
  upload: <path d="M12 15V3m-5 5 5-5 5 5M4 16v5h16v-5" />,
  chevron: <path d="m6 9 6 6 6-6" />,
  check: <path d="m5 12 4 4 10-10" />,
  clock: <><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 2" /></>,
  total: <><path d="M5 5h14M5 19h14M7 5v3l5 4-5 4v3M17 5v3l-5 4 5 4v3" /></>,
  target: <><circle cx="12" cy="12" r="9" /><circle cx="12" cy="12" r="4" /><circle cx="12" cy="12" r="1" fill="currentColor" /></>,
  guide: <><path d="M4 4h16v16H4ZM8 8h8M8 12h5M8 16h3" /></>,
  grid: <path d="M3 3h7v7H3ZM14 3h7v7h-7ZM3 14h7v7H3ZM14 14h7v7h-7Z" />,
  screen: <><rect x="3" y="3" width="18" height="13" rx="2" /><path d="M12 16v5M8 21h8" /></>,
  collapse: <><rect x="3" y="3" width="18" height="18" rx="2" /><path d="m8 10 4 4 4-4" /></>,
  expand: <><rect x="3" y="3" width="18" height="18" rx="2" /><path d="m8 14 4-4 4 4" /></>,
  save: <><path d="M4 3h13l3 3v15H4ZM8 3v6h8V3M8 21v-7h8v7" /></>,
  prev: <path d="m15 5-7 7 7 7" />,
  next: <path d="m9 5 7 7-7 7" />,
}

export default function PresenterIcon({ name }) {
  return <svg className="pr-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor"
    strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[name]}</svg>
}
