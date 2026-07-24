export function projectType(project) {
  return project && typeof project === 'object' && project.type === 'pdf' ? 'pdf' : 'typst'
}

export function workspaceComponentFor(project) {
  return projectType(project)
}
