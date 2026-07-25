import React, {
  forwardRef,
  useImperativeHandle,
  useRef,
  useState,
} from 'react'
import { formatFileSize, selectPdfFile } from './projectCreation.js'

const PdfFilePicker = forwardRef(function PdfFilePicker(
  { file, onFile, onError },
  ref,
) {
  const inputRef = useRef(null)
  const dragDepthRef = useRef(0)
  const [dragging, setDragging] = useState(false)

  const clearInput = () => {
    if (inputRef.current) inputRef.current.value = ''
  }

  useImperativeHandle(ref, () => ({ clear: clearInput }), [])

  const openPicker = () => {
    clearInput()
    inputRef.current?.click()
  }

  const applyFiles = (files) => {
    const result = selectPdfFile(files, file)
    if (result.error) {
      onError?.(result.error)
      return
    }
    onFile(result.file)
  }

  return (
    <div
      className={`pdf-file-picker${dragging ? ' is-dragging' : ''}${file ? ' has-file' : ''}`}
      role="group"
      aria-label="PDF file"
      onDragEnter={(event) => {
        event.preventDefault()
        dragDepthRef.current += 1
        setDragging(true)
      }}
      onDragOver={(event) => {
        event.preventDefault()
        event.dataTransfer.dropEffect = 'copy'
      }}
      onDragLeave={(event) => {
        event.preventDefault()
        dragDepthRef.current = Math.max(0, dragDepthRef.current - 1)
        if (dragDepthRef.current === 0) setDragging(false)
      }}
      onDrop={(event) => {
        event.preventDefault()
        dragDepthRef.current = 0
        setDragging(false)
        applyFiles(event.dataTransfer.files)
      }}
    >
      <input
        ref={inputRef}
        className="pdf-file-picker-input"
        type="file"
        accept=".pdf,application/pdf"
        onChange={(event) => applyFiles(event.target.files)}
      />
      {file ? (
        <div className="pdf-file-picker-selection">
          <span className="pdf-file-picker-icon" aria-hidden="true">PDF</span>
          <span className="pdf-file-picker-meta">
            <span className="pdf-file-picker-name" title={file.name}>{file.name}</span>
            <span className="pdf-file-picker-size">{formatFileSize(file.size)}</span>
          </span>
          <button type="button" className="mini" onClick={openPicker}>Change</button>
          <button
            type="button"
            className="mini pdf-file-picker-remove"
            onClick={() => {
              clearInput()
              onFile(null)
            }}
          >
            Remove
          </button>
        </div>
      ) : (
        <div className="pdf-file-picker-empty">
          <button type="button" className="primary" onClick={openPicker}>Select PDF</button>
          <span>or drop one PDF here</span>
        </div>
      )}
    </div>
  )
})

export default PdfFilePicker
