import React, { useEffect, useMemo, useState } from 'react'
import { api } from './api.js'

const WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
const weekday = (iso) => WEEKDAYS[(new Date(iso + 'T12:00:00').getDay() + 6) % 7]
const short = (h) => (h ? h.slice(0, 10) : '—')
const key = (n, s) => `${n}:${s}`

export default function App() {
  const [meta, setMeta] = useState(null)
  const [health, setHealth] = useState(null)
  const [periodId, setPeriodId] = useState(null)
  const [period, setPeriod] = useState(null)
  const [version, setVersion] = useState(null)
  const [unitId, setUnitId] = useState(null)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState(null)
  const [menu, setMenu] = useState(null) // {nurse, day, x, y}
  const [modal, setModal] = useState(null) // {title, text}
  const [focus, setFocus] = useState(null) // violation to highlight

  const refreshMeta = async () => {
    const m = await api.meta()
    setMeta(m)
    return m
  }

  useEffect(() => {
    (async () => {
      try {
        setHealth(await api.health())
        const m = await refreshMeta()
        if (m.periods.length) setPeriodId(m.periods[0].id)
        if (m.units.length) setUnitId(m.units[0].id)
      } catch (e) {
        setError(e.message)
      }
    })()
  }, [])

  const loadPeriod = async (id, selectVersion = true) => {
    const p = await api.period(id)
    setPeriod(p)
    if (selectVersion && p.versions.length) setVersion(await api.version(p.versions[0].id))
    else if (!p.versions.length) setVersion(null)
    return p
  }

  useEffect(() => {
    if (periodId) loadPeriod(periodId).catch((e) => setError(e.message))
  }, [periodId])

  const run = async (label, fn) => {
    setBusy(label)
    setError(null)
    try {
      return await fn()
    } catch (e) {
      setError(e.message)
      if (e.data?.violations) setVersion((v) => v && { ...v })
    } finally {
      setBusy('')
    }
  }

  const solve = () =>
    run('Solving with CP-SAT, then checking in Lean…', async () => {
      const v = await api.solve(periodId, 20)
      setVersion(v)
      await loadPeriod(periodId, false)
      await refreshMeta()
    })

  const publish = () =>
    run('Checking the Lean certificate…', async () => {
      const v = await api.publish(version.id)
      setVersion(v)
      await loadPeriod(periodId, false)
      await refreshMeta()
    })

  const selectVersion = (id) => run('Loading…', async () => setVersion(await api.version(id)))

  const applyOps = (ops, note) =>
    run('Saving edit and re-checking in Lean…', async () => {
      const v = await api.edit(version.id, ops, note)
      setVersion(v)
      await loadPeriod(periodId, false)
    })

  const showCert = () =>
    run('Loading certificate…', async () => {
      const text = await api.certificate(version.id)
      setModal({ title: `Lean certificate · v${version.version_no}`, text })
    })

  const showSpec = () =>
    run('Loading spec…', async () => setModal({ title: 'Spec.lean — the rules every schedule must satisfy', text: await api.spec() }))

  // ---- derived data ----
  const nurses = useMemo(() => (meta ? Object.fromEntries(meta.nurses.map((n) => [n.id, n])) : {}), [meta])
  const skills = useMemo(() => (meta ? Object.fromEntries(meta.skills.map((s) => [s.id, s.code])) : {}), [meta])
  const shifts = useMemo(() => (period ? Object.fromEntries(period.shifts.map((s) => [s.id, s])) : {}), [period])
  const days = useMemo(() => {
    if (!period) return []
    const seen = new Map()
    period.shifts.forEach((s) => seen.set(s.day_index, s.date))
    return [...seen.entries()].sort((a, b) => a[0] - b[0]).map(([i, date]) => ({ i, date }))
  }, [period])
  const unitShifts = useMemo(() => {
    const m = {}
    period?.shifts.filter((s) => s.unit_id === unitId).forEach((s) => (m[`${s.day_index}:${s.kind}`] = s))
    return m
  }, [period, unitId])

  const byNurseShift = useMemo(() => {
    const m = {}
    version?.assignments.forEach((a) => (m[key(a.nurse, a.shift)] = a))
    return m
  }, [version])
  const sets = useMemo(() => {
    const mk = (xs) => new Set((xs || []).map((p) => key(p.nurse, p.shift)))
    return { unavailable: mk(version?.unavailable), likes: mk(version?.likes), dislikes: mk(version?.dislikes) }
  }, [version])
  const fast = version?.verifications.filter((v) => v.mode === 'fast').at(-1)
  const kernel = version?.verifications.filter((v) => v.mode === 'kernel').at(-1)
  const violations = fast?.violations || []
  const earned = useMemo(() => Object.fromEntries((version?.karma_earned || []).map((r) => [r.nurse, r.earned])), [version])
  const flagged = useMemo(() => {
    const s = new Set()
    violations.forEach((v) => {
      if (v.nurse && v.shift) s.add(key(v.nurse, v.shift))
    })
    return s
  }, [violations])
  const flaggedShifts = useMemo(() => new Set(violations.filter((v) => !v.nurse && v.shift).map((v) => v.shift)), [violations])

  const unitNurses = meta ? meta.nurses.filter((n) => n.unit_id === unitId) : []

  const cellFor = (nurseId, day) => {
    for (const kind of ['day', 'night']) {
      const s = unitShifts[`${day}:${kind}`]
      if (s && byNurseShift[key(nurseId, s.id)]) return { shift: s, a: byNurseShift[key(nurseId, s.id)] }
    }
    // assignments on other units' shifts (floats) for completeness
    const other = version?.assignments.find((a) => a.nurse === nurseId && shifts[a.shift]?.day_index === day && shifts[a.shift]?.unit_id !== unitId)
    return other ? { shift: shifts[other.shift], a: other, float: true } : null
  }

  const choose = (nurseId, day, choice) => {
    setMenu(null)
    const cur = cellFor(nurseId, day)
    const ops = []
    if (cur) ops.push({ op: 'unassign', nurse: nurseId, shift: cur.shift.id })
    if (choice !== 'off') {
      const [kind, charge] = choice.split('+')
      const s = unitShifts[`${day}:${kind}`]
      if (!s) return
      ops.push({ op: 'assign', nurse: nurseId, shift: s.id, isCharge: charge === 'charge' })
    }
    const n = nurses[nurseId]
    applyOps(ops, `${n.name}: day ${day + 1} → ${choice}`)
  }

  if (error && !meta) return <div className="page"><div className="error">{error}</div></div>

  return (
    <div className="page" onClick={() => setMenu(null)}>
      <header>
        <div>
          <h1>Nightingale <span className="sub">verified nurse scheduling</span></h1>
          <div className="muted small">
            Lean checker {health?.checker ? '✓ built' : '✗ missing'} · {health?.lean} · spec {short(health?.spec_hash)}
            {' · '}<a onClick={showSpec}>read the rules (Spec.lean)</a>
          </div>
        </div>
        <div className="controls">
          <select value={periodId || ''} onChange={(e) => setPeriodId(+e.target.value)}>
            {meta?.periods.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
          </select>
          <button className="primary" disabled={!!busy || !periodId} onClick={solve}>Generate schedule</button>
        </div>
      </header>

      {busy && <div className="busy">{busy}</div>}
      {error && <div className="error">{error}</div>}

      {period && (
        <div className="versions">
          <span className="muted">Versions:</span>
          {period.versions.map((v) => (
            <button key={v.id} className={`chip ${version?.id === v.id ? 'active' : ''} ${v.status}`} onClick={() => selectVersion(v.id)}>
              v{v.version_no} {v.valid ? '✓' : '✗'}{v.status === 'published' ? ' · published' : v.status === 'superseded' ? ' · old' : ''}
            </button>
          ))}
          {!period.versions.length && <span className="muted">none yet — click “Generate schedule”.</span>}
        </div>
      )}

      {version && (
        <section className="status">
          <div className={`badge ${version.valid ? 'ok' : 'bad'}`}>
            {version.valid ? '✓ Lean: valid schedule' : `✗ Lean: ${violations.length} violation(s)`}
            <span className="small"> {fast?.duration_ms} ms</span>
          </div>
          <div className={`badge ${version.certified ? 'ok' : 'neutral'}`}>
            {version.certified ? `✓ Certificate checked (${(kernel.duration_ms / 1000).toFixed(1)} s)` : 'Not certified yet'}
          </div>
          <div className="badge neutral">
            v{version.version_no} · {version.status} · {version.source}{version.solver_status ? ` (${version.solver_status}, ${version.solve_seconds}s)` : ''}
          </div>
          <div className="badge neutral">
            {version.stats.assignments} shifts · agency {version.stats.agency_shifts} · liked {version.stats.preferences.liked || 0} · disliked {version.stats.preferences.disliked || 0}
          </div>
          <div className="spacer" />
          {version.certified && <button onClick={showCert}>View certificate</button>}
          {version.status === 'draft' && (
            <button className="primary" disabled={!!busy || !version.valid} onClick={publish}
              title={version.valid ? 'Checks the Lean certificate, then publishes' : 'Fix the violations first'}>
              Publish
            </button>
          )}
        </section>
      )}

      {meta && (
        <nav className="tabs">
          {meta.units.map((u) => (
            <button key={u.id} className={u.id === unitId ? 'active' : ''} onClick={() => setUnitId(u.id)}>{u.name}</button>
          ))}
        </nav>
      )}

      <div className="main">
        <div className="gridwrap">
          {period && version && (
            <table className="grid">
              <thead>
                <tr>
                  <th className="sticky">Nurse</th>
                  <th title="karma balance (+ earned by this version)">Karma</th>
                  {days.map((d) => (
                    <th key={d.i} className={['Sat', 'Sun'].includes(weekday(d.date)) ? 'weekend' : ''}>
                      <div>{weekday(d.date)}</div>
                      <div className="small muted">{d.date.slice(5)}</div>
                      {unitShifts[`${d.i}:night`]?.minutes !== 720 && unitShifts[`${d.i}:night`] &&
                        <div className="small dst" title="DST change: this night shift is 13 hours">DST</div>}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {unitNurses.map((n) => (
                  <tr key={n.id} className={n.is_agency ? 'agency' : ''}>
                    <td className="sticky name">
                      <div>{n.name}{n.charge_qualified && <span className="tag" title="charge qualified">C</span>}</div>
                      <div className="small muted">
                        {n.seniority_years}y · {n.skills.map((s) => skills[s]).join(' ') || '—'}
                        {n.preferences?.preferred_kind ? ` · prefers ${n.preferences.preferred_kind}` : ''}
                      </div>
                    </td>
                    <td className="karma">
                      {n.is_agency ? '—' : n.karma}
                      {earned[n.id] ? <span className="small plus"> +{earned[n.id]}</span> : null}
                    </td>
                    {days.map((d) => {
                      const cell = cellFor(n.id, d.i)
                      const dayS = unitShifts[`${d.i}:day`]
                      const nightS = unitShifts[`${d.i}:night`]
                      const blocked = [dayS, nightS].some((s) => s && sets.unavailable.has(key(n.id, s.id)))
                      const s = cell?.shift
                      const pref = s ? (sets.dislikes.has(key(n.id, s.id)) ? 'disliked' : sets.likes.has(key(n.id, s.id)) ? 'liked' : '') : ''
                      const bad = s && (flagged.has(key(n.id, s.id)) || (focus && focus.nurse === n.id && focus.shift === s.id))
                      return (
                        <td key={d.i}
                          className={`cell ${blocked ? 'blocked' : ''} ${pref} ${bad ? 'flag' : ''} ${cell ? cell.shift.kind : ''}`}
                          title={blocked ? 'Unavailable (PTO / standing commitment)' : pref ? `${pref} by nurse` : ''}
                          onClick={(e) => {
                            e.stopPropagation()
                            setMenu({ nurse: n.id, day: d.i, x: e.clientX, y: e.clientY })
                          }}>
                          {cell ? (cell.shift.kind === 'day' ? 'D' : 'N') + (cell.a.isCharge ? '★' : '') + (cell.float ? '↗' : '') : ''}
                        </td>
                      )
                    })}
                  </tr>
                ))}
                {['day', 'night'].map((kind) => (
                  <tr key={kind} className="coverage">
                    <td className="sticky">{kind === 'day' ? 'Day staffed' : 'Night staffed'}</td>
                    <td />
                    {days.map((d) => {
                      const s = unitShifts[`${d.i}:${kind}`]
                      if (!s) return <td key={d.i} />
                      const staff = version.assignments.filter((a) => a.shift === s.id)
                      const ok = staff.length >= s.min_nurses && staff.length <= s.max_nurses
                      return (
                        <td key={d.i} className={`${ok && !flaggedShifts.has(s.id) ? '' : 'short'}`}
                          title={`min ${s.min_nurses}, max ${s.max_nurses}; karma cost ${s.karma_cost}`}>
                          {staff.length}<span className="small muted">/{s.min_nurses}</span>
                          {staff.some((a) => a.isCharge) ? '' : <span className="small">!★</span>}
                        </td>
                      )
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {period && !version && <div className="empty">No schedule yet. Click <b>Generate schedule</b> to run the optimizer; Lean checks every result.</div>}
        </div>

        {version && (
          <aside>
            <h3>Violations {violations.length ? `(${violations.length})` : ''}</h3>
            {!violations.length && <div className="okbox">None. Lean's checker is proven to accept exactly the schedules <code>Spec.lean</code> allows (<code>check_iff</code>).</div>}
            <ul className="violations">
              {violations.slice(0, 60).map((v, i) => (
                <li key={i} onMouseEnter={() => setFocus(v)} onMouseLeave={() => setFocus(null)}>
                  <b>{v.rule}</b>
                  {v.nurse ? ` · ${nurses[v.nurse]?.name || 'nurse ' + v.nurse}` : ''}
                  {v.shift && shifts[v.shift] ? ` · ${shifts[v.shift].date} ${shifts[v.shift].kind} (${meta.units.find((u) => u.id === shifts[v.shift].unit_id)?.code})` : ''}
                  <div className="small muted">{v.detail}</div>
                </li>
              ))}
            </ul>
            <h3>Legend</h3>
            <div className="legend">
              <span className="cell day">D</span> day 07–19 <span className="cell night">N</span> night 19–07 ★ charge
              <br /><span className="cell liked">D</span> liked <span className="cell disliked">D</span> disliked <span className="cell blocked" /> unavailable
              <br />Click any cell to edit. Every edit makes a new version, and Lean re-checks it.
            </div>
            <h3>Audit</h3>
            <div className="small mono">
              instance {short(version.instance_hash)}<br />
              schedule {short(version.schedule_hash)}<br />
              spec {short(fast?.spec_hash)}<br />
              {kernel && <>cert {short(kernel.cert_sha256)}<br />axioms: {kernel.axioms?.join(', ')}</>}
            </div>
          </aside>
        )}
      </div>

      {menu && (
        <div className="menu" style={{ left: menu.x, top: menu.y }} onClick={(e) => e.stopPropagation()}>
          <div className="small muted">{nurses[menu.nurse]?.name} · {days[menu.day]?.date}</div>
          {[['off', 'Off'], ['day', 'Day'], ['day+charge', 'Day (charge ★)'], ['night', 'Night'], ['night+charge', 'Night (charge ★)']].map(([c, l]) => (
            <button key={c} onClick={() => choose(menu.nurse, menu.day, c)}>{l}</button>
          ))}
        </div>
      )}

      {modal && (
        <div className="modal" onClick={() => setModal(null)}>
          <div className="modalbox" onClick={(e) => e.stopPropagation()}>
            <div className="modalhead"><b>{modal.title}</b><button onClick={() => setModal(null)}>Close</button></div>
            <pre>{modal.text.length > 60000 ? modal.text.slice(0, 60000) + '\n… (truncated)' : modal.text}</pre>
          </div>
        </div>
      )}
    </div>
  )
}
