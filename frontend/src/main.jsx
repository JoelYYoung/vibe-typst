import React, { useEffect, useState } from 'react'
import { createRoot } from 'react-dom/client'
import App from './App.jsx'
import Projection from './Projection.jsx'
import OnboardingPage from './OnboardingPage.jsx'
import ProjectsPage from './ProjectsPage.jsx'
import AdminPage from './AdminPage.jsx'
import Toaster from './Toaster.jsx'
import './styles.css'

function Root() {
  const [view, setView] = useState('loading')

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

  function goToEditor() { setView('editor') }
  function goToProjects() {
    fetch('/api/projects/close', { method: 'POST' }).catch(() => {})
    setView('projects')
  }
  function goToAdmin() { setView('admin') }

  if (view === 'loading') return <div className="app-loading">✦</div>
  return (
    <>
      {view === 'onboarding' && <OnboardingPage onDone={() => setView('projects')} />}
      {view === 'projects' && <ProjectsPage onOpen={goToEditor} onOpenAdmin={goToAdmin} />}
      {view === 'admin' && <AdminPage onBack={goToProjects} />}
      {view === 'editor' && <App onBackToProjects={goToProjects} />}
      <Toaster />
    </>
  )
}

// `?project` = the audience/projector window (just the current slide, follows the presenter).
const isProjection = new URLSearchParams(location.search).has('project')
createRoot(document.getElementById('root')).render(isProjection ? <Projection /> : <Root />)
