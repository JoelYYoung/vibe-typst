import { workspaceComponentFor } from './projectTypes.js'

export function canonicalProjectFromOpen(result) {
  return result && typeof result === 'object' && result.project && typeof result.project === 'object'
    ? result.project
    : null
}

export function workspaceViewFor(project) {
  return workspaceComponentFor(project) === 'pdf' ? 'PdfWorkspace' : 'App'
}
