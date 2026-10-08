import React, { useEffect, useId, useRef, useState } from 'react'
import Icon from './PresenterIcon.jsx'
import { formatRecordingTime } from './slideRecorder.js'

export default function RecordingReferenceSelect({ takes, value, onChange }) {
  const root = useRef(null)
  const search = useRef({ text: '', time: 0 })
  const id = useId()
  const [open, setOpen] = useState(false)
  const options = takes.map(take => ({ value: String(take.page), label: `Page ${take.page}`, duration: formatRecordingTime(take.duration), icon: 'mic' }))
    .concat({ value: 'upload', label: 'Upload audio', icon: 'upload' })
  const selected = options.findIndex(option => option.value === value)
  const [active, setActive] = useState(Math.max(0, selected))
  const current = options[selected] || { label: 'Choose voice', icon: 'mic' }

  useEffect(() => {
    if (!open) return
    const outside = event => { if (!root.current?.contains(event.target)) setOpen(false) }
    document.addEventListener('pointerdown', outside)
    return () => document.removeEventListener('pointerdown', outside)
  }, [open])
  useEffect(() => {
    if (open) document.getElementById(`${id}-${active}`)?.scrollIntoView({ block: 'nearest' })
  }, [open, active, id])

  function choose(index) {
    onChange(options[Math.min(index, options.length - 1)].value)
    setOpen(false)
  }
  function show() { setActive(Math.max(0, selected)); setOpen(true); search.current.text = '' }
  function keyDown(event) {
    if (event.key === 'Escape' && open) {
      event.preventDefault()
      event.stopPropagation()
      setOpen(false)
    } else if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
      event.preventDefault()
      event.stopPropagation()
      if (!open) show()
      setActive(event.key === 'Home' ? 0 : event.key === 'End' ? options.length - 1
        : Math.max(0, Math.min(options.length - 1, (open ? active : Math.max(0, selected)) + (event.key === 'ArrowDown' ? 1 : -1))))
    } else if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault()
      event.stopPropagation()
      if (open) choose(active)
      else show()
    } else if (event.key === 'Tab') {
      setOpen(false)
    } else if (event.key.length === 1 && !event.ctrlKey && !event.metaKey && !event.altKey) {
      const now = Date.now()
      search.current.text = (now - search.current.time < 600 ? search.current.text : '') + event.key.toLowerCase()
      search.current.time = now
      const match = options.findIndex(option => option.label.toLowerCase().startsWith(search.current.text) || option.value.startsWith(search.current.text))
      if (match >= 0) {
        event.preventDefault()
        event.stopPropagation()
        setOpen(true)
        setActive(match)
      }
    }
  }
  return <div className="pr-reference-select" ref={root} onBlur={event => {
    if (!event.currentTarget.contains(event.relatedTarget)) setOpen(false)
  }}>
    <button className="pr-reference-trigger" type="button" role="combobox" aria-label="Reference voice"
      aria-haspopup="listbox" aria-expanded={open} aria-controls={`${id}-list`}
      aria-activedescendant={open ? `${id}-${active}` : undefined}
      onClick={() => open ? setOpen(false) : show()} onKeyDown={keyDown}>
      <Icon name={current.icon} /><span>{current.label}</span>
      {current.duration && <small>{current.duration}</small>}
      <Icon name="chevron" />
    </button>
    {open && <div className="pr-reference-menu" role="listbox" aria-label="Reference voice options" id={`${id}-list`}>
      {options.map((option, index) => <div key={option.value} id={`${id}-${index}`} role="option"
        aria-selected={option.value === value} data-value={option.value}
        className={`pr-reference-option${index === active ? ' active' : ''}${option.value === 'upload' ? ' upload' : ''}`}
        onPointerMove={() => setActive(index)} onPointerDown={event => event.preventDefault()} onClick={() => choose(index)}>
        <Icon name={option.icon} /><span>{option.label}</span>
        {option.duration && <small>{option.duration}</small>}
        <span className="pr-reference-check">{option.value === value && <Icon name="check" />}</span>
      </div>)}
    </div>}
  </div>
}
