const WORKSPACE_PARAM = 'workspace'
const WORKSPACE_ID = /^[a-f0-9]{24}$/

function currentSearch() {
  return typeof location === 'undefined' ? '' : location.search
}

export function projectWorkspaceId(search = currentSearch()) {
  const value = new URLSearchParams(search || '').get(WORKSPACE_PARAM)
  return value && WORKSPACE_ID.test(value) ? value : null
}

export function inProjectWorkspace(search = currentSearch()) {
  return projectWorkspaceId(search) !== null
}

export function workspacePath(path, search = currentSearch()) {
  const workspace = projectWorkspaceId(search)
  if (!workspace) return path
  const normalized = path.startsWith('/') ? path : `/${path}`
  return `/project-workspaces/${workspace}${normalized}`
}

export function workspaceWebSocketUrl(path, search = currentSearch()) {
  const normalized = workspacePath(path, search)
  if (typeof location === 'undefined') return normalized
  const protocol = location.protocol === 'https:' ? 'wss' : 'ws'
  return `${protocol}://${location.host}${normalized}`
}

export function openProjectInNewTabUrl(projectId) {
  return `/project-workspaces/open?project_id=${encodeURIComponent(projectId)}`
}

// The projection window is its own tab, so it cannot inherit the presenter's in-memory PDF
// project scope — it carries the project in its URL and restores the scope on load. `project`
// (empty) stays the flag that says "render the audience view"; `pid` says which project.
export function projectionUrl(search = currentSearch(), projectId = null) {
  const params = new URLSearchParams()
  const workspace = projectWorkspaceId(search)
  if (workspace) params.set(WORKSPACE_PARAM, workspace)
  params.set('project', '')
  if (projectId) params.set('pid', projectId)
  return `/?${params.toString()}`
}

export function projectionProjectId(search = currentSearch()) {
  const value = new URLSearchParams(search || '').get('pid')
  return value && value.trim() ? value : null
}

export function workspaceChannelName(base, search = currentSearch()) {
  const workspace = projectWorkspaceId(search)
  return workspace ? `${base}:${workspace}` : base
}
