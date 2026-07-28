import { workspaceComponentFor } from './projectTypes.js'

export function canonicalProjectFromOpen(result) {
  return result && typeof result === 'object' && result.project && typeof result.project === 'object'
    ? result.project
    : null
}

export function workspaceViewFor(project) {
  return workspaceComponentFor(project) === 'pdf' ? 'PdfWorkspace' : 'App'
}

export function requestedProjectId(search) {
  const value = new URLSearchParams(search || '').get('openProject')
  return value && value.trim() ? value : null
}

export function clearRequestedProject(search) {
  const params = new URLSearchParams(search || '')
  params.delete('openProject')
  const remaining = params.toString()
  return remaining ? `?${remaining}` : ''
}
