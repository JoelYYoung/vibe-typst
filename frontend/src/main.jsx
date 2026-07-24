import React, { useEffect, useState } from 'react'
import { createRoot } from 'react-dom/client'
import App from './App.jsx'
import Projection from './Projection.jsx'
import OnboardingPage from './OnboardingPage.jsx'
import ProjectsPage from './ProjectsPage.jsx'
import AdminPage from './AdminPage.jsx'
import PdfWorkspace from './PdfWorkspace.jsx'
import Toaster from './Toaster.jsx'
import { workspaceViewFor } from './projectRouting.js'
import './styles.css'

function Root() {
  const [view, setView] = useState('loading')
  const [activeProject, setActiveProject] = useState(null)

  async function checkState() {
    try {
      const r = await fetch('/api/app/state')
      const s = await r.json()
      // Always land on the Projects page on (re)load — only divert to onboarding when the
      // local app hasn't been configured yet. The editor is entered by opening a project.
      if (!s.configured && s.mode === 'local') setView('onboarding')
      else setView('projects')
    } catch {
      setView('projects')
    }
  }

  useEffect(() => { checkState() }, [])

  function goToEditor(project) {
    setActiveProject(project)
    setView('editor')
  }
  function goToProjects() {
    fetch('/api/projects/close', { method: 'POST' }).catch(() => {})
    setActiveProject(null)
    setView('projects')
  }
  function goToAdmin() { setView('admin') }

  if (view === 'loading') return <div className="app-loading">✦</div>
  const workspaceView = workspaceViewFor(activeProject)
  return (
    <>
      {view === 'onboarding' && <OnboardingPage onDone={() => setView('projects')} />}
      {view === 'projects' && <ProjectsPage onOpen={goToEditor} onOpenAdmin={goToAdmin} />}
      {view === 'admin' && <AdminPage onBack={goToProjects} />}
      {view === 'editor' && workspaceView === 'App' && (
        <App project={activeProject} onBack={goToProjects} onBackToProjects={goToProjects} />
      )}
      {view === 'editor' && workspaceView === 'PdfWorkspace' && (
        <PdfWorkspace project={activeProject} onBack={goToProjects} />
      )}
      <Toaster />
    </>
  )
}

// `?project` = the audience/projector window (just the current slide, follows the presenter).
const isProjection = new URLSearchParams(location.search).has('project')
createRoot(document.getElementById('root')).render(isProjection ? <Projection /> : <Root />)
