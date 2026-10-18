import React, { useEffect, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { normalizeGatewayKey } from './auth.js'
import './style.css'

const WINDOWS = [
  { value: '1h', label: 'Last hour' },
  { value: '24h', label: '24 hours' },
  { value: '7d', label: '7 days' },
  { value: '30d', label: '30 days' },
]
const PAGE_SIZE = 25

const integer = (value) => new Intl.NumberFormat().format(value ?? 0)
const percent = (value) => new Intl.NumberFormat(undefined, {
  style: 'percent', maximumFractionDigits: 1,
}).format(value ?? 0)
const dollars = (value) => {
  if (value == null) return '—'
  const amount = Number(value)
  if (amount === 0) return '$0.00'
  return new Intl.NumberFormat(undefined, {
    style: 'currency', currency: 'USD', minimumFractionDigits: 2,
    maximumFractionDigits: amount < 0.01 ? 8 : 4,
  }).format(amount)
}
const milliseconds = (value) => `${new Intl.NumberFormat(undefined, {
  maximumFractionDigits: 1,
}).format(value ?? 0)} ms`
const timestamp = (value) => new Date(value).toLocaleString()

async function loadJson(path, key, signal) {
  const response = await fetch(path, {
    headers: { Authorization: `Bearer ${key}` },
    cache: 'no-store',
    signal,
  })
  if (!response.ok) {
    if (response.status === 401) throw new Error('Gateway key rejected. Use an active gateway key supplied privately by the owner.')
    if (response.status === 503) throw new Error('Analytics is temporarily unavailable. Please try again.')
    throw new Error(`Analytics request failed (${response.status}).`)
  }
  return response.json()
}

function Metric({ label, value, detail }) {
  return <div className="metric">
    <span className="metric-label">{label}</span>
    <strong>{value}</strong>
    {detail && <span className="metric-detail">{detail}</span>}
  </div>
}

function App() {
  const [draftKey, setDraftKey] = useState('')
  const [key, setKey] = useState('')
  const [windowSize, setWindowSize] = useState('24h')
  const [offset, setOffset] = useState(0)
  const [refresh, setRefresh] = useState(0)
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(false)
  const [connecting, setConnecting] = useState(false)
  const [error, setError] = useState('')

  async function connect(event) {
    event.preventDefault()
    const candidate = normalizeGatewayKey(draftKey)
    if (!candidate) return
    setConnecting(true)
    setError('')
    try {
      await loadJson('/analytics/summary?window=24h', candidate)
      setKey(candidate)
      setDraftKey('')
    } catch (reason) {
      setError(reason.message)
    } finally {
      setConnecting(false)
    }
  }

  useEffect(() => {
    if (!key) return
    const controller = new AbortController()
    const query = `window=${windowSize}`
    setLoading(true)
    setError('')
    setData(null)
    Promise.all([
      loadJson(`/analytics/summary?${query}`, key, controller.signal),
      loadJson(`/analytics/providers?${query}`, key, controller.signal),
      loadJson(`/requests?${query}&limit=${PAGE_SIZE}&offset=${offset}`, key, controller.signal),
    ]).then(([summary, providers, history]) => {
      setData({ summary, providers: providers.providers, history })
    }).catch((reason) => {
      if (reason.name !== 'AbortError') setError(reason.message)
    }).finally(() => {
      if (!controller.signal.aborted) setLoading(false)
    })
    return () => controller.abort()
  }, [key, windowSize, offset, refresh])

  const summary = data?.summary
  const providers = data?.providers ?? []
  const history = data?.history
  const maximum = Math.max(1, ...providers.map((item) => item.request_count))

  return <div className="page">
    <header className="masthead">
      <div className="brand"><span className="brand-mark">G</span><span>Gateway <b>Analytics</b></span></div>
      <span className="environment">GATEWAY TELEMETRY</span>
    </header>

    <main>
      <section className="intro">
        <div>
          <p className="eyebrow">REQUEST ANALYTICS</p>
          <h1>One view of every request.</h1>
          <p className="subhead">Usage, outcomes, and estimated costs across your providers.</p>
        </div>
        {key && <button className="text-button" onClick={() => {
          setKey(''); setDraftKey(''); setData(null); setError('')
        }}>Disconnect</button>}
      </section>

      {!key ? <><form className="connect-panel" onSubmit={connect}>
        <div><h2>Connect to your gateway</h2><p>Enter your private gateway key to read analytics. It stays in this tab’s memory.</p></div>
        <div className="connect-controls">
          <label htmlFor="gateway-key">Gateway API key</label>
          <div className="input-row">
            <input id="gateway-key" type="password" value={draftKey}
              onChange={(event) => setDraftKey(event.target.value)} autoComplete="off" required />
            <button type="submit" className="primary-button" disabled={connecting}>{connecting ? 'Checking…' : 'Connect'}</button>
          </div>
        </div>
      </form>{error && <div className="notice error" role="alert">{error}</div>}</> : <>
        <div className="toolbar">
          <div className="range-control">
            <label htmlFor="window">Time range</label>
            <select id="window" value={windowSize} onChange={(event) => {
              setWindowSize(event.target.value); setOffset(0)
            }}>{WINDOWS.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}</select>
          </div>
          <button className="secondary-button" onClick={() => setRefresh((value) => value + 1)}>Refresh</button>
        </div>

        {error && <div className="notice error" role="alert">{error}</div>}
        {loading && <div className="notice" role="status">Loading telemetry…</div>}
        {summary && <>
          <div className="section-heading"><h2>Overview</h2><span>Through {timestamp(summary.end)}</span></div>
          <section className="metrics-grid" aria-label="Gateway overview">
            <Metric label="Requests" value={integer(summary.request_count)} detail={`${integer(summary.success_count)} successful`} />
            <Metric label="Success rate" value={percent(summary.success_rate)}
              detail={`${percent(summary.error_rate)} error rate · ${integer(summary.error_count)} errors`} />
            <Metric label="Cache-hit rate" value={percent(summary.cache_hit_rate)} detail={`${integer(summary.cache_hit_count)} hits`} />
            <Metric label="Tokens used" value={integer(summary.input_tokens + summary.output_tokens)}
              detail={`${integer(summary.input_tokens)} in · ${integer(summary.output_tokens)} out`} />
            <Metric label="Estimated cost" value={dollars(summary.estimated_cost_usd)}
              detail={summary.unpriced_success_count ? `${summary.unpriced_success_count} successful requests unpriced` : 'Reported usage at configured rates'} />
            <Metric label="Latency p95" value={milliseconds(summary.p95_latency_ms)}
              detail={`Mean ${milliseconds(summary.mean_latency_ms)}`} />
          </section>

          <section className="panel">
            <div className="section-heading"><h2>Provider breakdown</h2><span>Final provider · includes cache hits</span></div>
            {providers.length === 0 ? <p className="empty">No requests in this time range.</p> :
              <div className="provider-list">{providers.map((item) => <div className="provider-row" key={item.provider}>
                <div className="provider-name"><span className="provider-dot" />{item.provider}</div>
                <div className="provider-volume"><div className="bar" style={{ width: `${100 * item.request_count / maximum}%` }} /></div>
                <div className="provider-stats">
                  <span><b>{integer(item.request_count)}</b> requests</span>
                  <span><b>{percent(item.success_rate)}</b> success</span>
                  <span><b>{percent(item.cache_hit_rate)}</b> cache hit</span>
                  <span><b>{integer(item.input_tokens + item.output_tokens)}</b> tokens</span>
                  <span><b>{dollars(item.estimated_cost_usd)}</b> est.</span>
                  <span><b>{milliseconds(item.p95_latency_ms)}</b> p95</span>
                </div>
              </div>)}</div>}
          </section>

          <section className="panel history-panel">
            <div className="section-heading"><h2>Recent requests</h2><span>{integer(history.total)} in range</span></div>
            <div className="table-scroll"><table>
              <thead><tr><th>Time</th><th>Alias</th><th>Provider</th><th>Outcome</th><th>Tokens</th><th>Latency</th><th>Est. cost</th></tr></thead>
              <tbody>{history.items.map((item) => <tr key={item.request_id}>
                <td>{timestamp(item.created_at)}</td>
                <td>{item.model_alias ?? '—'}</td>
                <td>{item.final_provider ?? 'Unrouted'}</td>
                <td><span className={`status ${item.status_code < 300 ? 'good' : 'bad'}`}>{item.outcome}</span></td>
                <td>{item.input_tokens == null && item.output_tokens == null
                  ? '—' : integer((item.input_tokens ?? 0) + (item.output_tokens ?? 0))}</td>
                <td>{milliseconds(item.latency_ms)}</td>
                <td>{dollars(item.estimated_cost_usd)}</td>
              </tr>)}</tbody>
            </table></div>
            {history.items.length === 0 && <p className="empty">No requests in this time range.</p>}
            <div className="pagination">
              <span>{history.total ? `${offset + 1}–${Math.min(offset + PAGE_SIZE, history.total)} of ${history.total}` : '0 requests'}</span>
              <div><button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}>Previous</button>
                <button disabled={offset + PAGE_SIZE >= history.total} onClick={() => setOffset(offset + PAGE_SIZE)}>Next</button></div>
            </div>
          </section>
        </>}
      </>}
    </main>
    <footer>Costs are estimates based on configured prices and reported tokens. Times shown in your local timezone.</footer>
  </div>
}

createRoot(document.getElementById('root')).render(<App />)
