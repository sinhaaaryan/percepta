async function req(method, path, body) {
  const res = await fetch(`/api${path}`, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : {},
    body: body ? JSON.stringify(body) : undefined,
  })
  const text = await res.text()
  const data = text && res.headers.get('content-type')?.includes('json') ? JSON.parse(text) : text
  if (!res.ok) {
    const err = new Error(data?.detail || res.statusText)
    err.data = data
    throw err
  }
  return data
}

export const api = {
  health: () => req('GET', '/health'),
  meta: () => req('GET', '/meta'),
  period: (id) => req('GET', `/periods/${id}`),
  solve: (id, timeLimit = 20) => req('POST', `/periods/${id}/solve`, { time_limit: timeLimit }),
  version: (id) => req('GET', `/versions/${id}`),
  edit: (id, ops, note) => req('POST', `/versions/${id}/edits`, { ops, note }),
  publish: (id) => req('POST', `/versions/${id}/publish`),
  certificate: (id) => req('GET', `/versions/${id}/certificate`),
  spec: () => req('GET', '/spec'),
  karma: () => req('GET', '/karma'),
}
