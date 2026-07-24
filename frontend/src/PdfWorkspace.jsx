import React from 'react'

// Task 7 replaces this boundary with the PDF viewer and transcript workspace.
// Keeping it API-free ensures a PDF project never mounts the Typst runtime meanwhile.
export default function PdfWorkspace({ project, onBack }) {
  return (
    <main className="pdf-workspace-placeholder">
      <button className="back-btn" onClick={onBack}>← Projects</button>
      <div>
        <h1>{project?.name || 'PDF project'}</h1>
        <p>PDF workspace coming next.</p>
      </div>
    </main>
  )
}
