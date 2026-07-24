function completionTime(comment) {
  return comment.done_at || comment.updated_at || comment.created_at || ''
}

export function filterAndSortComments(comments, filter) {
  const visible = comments.filter((comment) => filter === 'all' || comment.status === filter)
  if (filter !== 'done') return visible
  return visible.sort((a, b) => (
    Number(b.done_seq || 0) - Number(a.done_seq || 0)
    || completionTime(b).localeCompare(completionTime(a))
    || Number(b.seq || 0) - Number(a.seq || 0)
  ))
}
