export function canSaveVersion(status, versionCount) {
  if (!status) return false
  return Boolean(status.dirty || versionCount === 0)
}
