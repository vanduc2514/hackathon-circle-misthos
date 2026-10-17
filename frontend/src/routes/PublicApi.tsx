import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api, unwrap } from '../lib/client'
import {
  exampleCall,
  freeEndpoints,
  grouped,
  paidEndpoints,
  shown,
  type ApiSchema,
  type Endpoint,
  type Tab,
} from '../lib/public-api'
import './public-api.css'

const DOCS = 'https://github.com/vanduc2514/hackathon-circle-misthos/blob/main/docs'

/**
 * The public API (#208): the routes anyone may call, free or paid over x402, as an
 * editorial index. The free routes are the ones the API's schema marks public; the
 * paid ones are the edge's own catalog, at the prices it charges.
 */
export default function PublicApi() {
  const [tab, setTab] = useState<Tab>('all')
  const schema = useQuery({
    queryKey: ['openapi'],
    queryFn: async () => {
      const res = await fetch('/openapi.json')
      if (!res.ok) throw new Error(`the API schema answered ${res.status}`)
      return (await res.json()) as ApiSchema
    },
    staleTime: 5 * 60_000,
  })
  const paid = useQuery({
    queryKey: ['catalog', 'paid'],
    queryFn: async () => unwrap(await api.GET('/api/v1/catalog/paid')),
    staleTime: 60_000,
  })

  const endpoints = [...freeEndpoints(schema.data), ...paidEndpoints(paid.data)]
  const free = shown(endpoints, 'free').length
  const x402 = shown(endpoints, 'x402').length
  const version = schema.data?.info?.version ?? '…'
  const tabs: { id: Tab; label: string; count: number }[] = [
    { id: 'all', label: 'All', count: endpoints.length },
    { id: 'free', label: 'Free', count: free },
    { id: 'x402', label: 'x402', count: x402 },
  ]

  return (
    <div className="api-index">
      <div className="api-toolbar">
        <div className="api-tabs" role="tablist" aria-label="Filter endpoints">
          {tabs.map((t) => (
            <button
              key={t.id}
              type="button"
              role="tab"
              aria-selected={tab === t.id}
              className={`api-tab${tab === t.id ? ' active' : ''}`}
              onClick={() => setTab(t.id)}
            >
              {t.label} <span>{t.count}</span>
            </button>
          ))}
        </div>
        <div className="api-base">
          <span>Version</span>
          <code>{version}</code>
        </div>
      </div>

      <main className="api-list" aria-live="polite">
        {schema.isLoading && <p className="api-quiet">Reading the API…</p>}
        {schema.isError && <p className="api-quiet">The API schema could not be read just now.</p>}
        {paid.data && !paid.data.available && tab !== 'free' && (
          <p className="api-quiet">The paid routes could not be read from the edge just now; the free ones are below.</p>
        )}
        {grouped(shown(endpoints, tab)).map(({ group, rows }) => (
          <section className="api-group" key={group} aria-labelledby={`api-${group}`}>
            <div className="api-group-head">
              <h2 id={`api-${group}`}>{group}</h2>
              <span>
                {rows.length} {rows.length === 1 ? 'endpoint' : 'endpoints'}
              </span>
            </div>
            <div className="api-table">
              {rows.map((ep) => (
                <Row key={ep.key} endpoint={ep} paidBase={paid.data?.base_url ?? null} />
              ))}
            </div>
          </section>
        ))}
        {x402 > 0 && tab !== 'free' && (
          <p className="api-quiet">
            x402 routes answer <code>402</code> with the price; an agent signs it and asks again.{' '}
            <a href={`${DOCS}/BUYING.md`} target="_blank" rel="noreferrer">
              How to pay
            </a>{' '}
            ·{' '}
            <a href={`${DOCS}/PAID-API.md`} target="_blank" rel="noreferrer">
              What each route answers
            </a>
          </p>
        )}
      </main>
    </div>
  )
}

function Price({ price }: { price: string | null }) {
  if (!price) return <span className="api-free">Free</span>
  return (
    <span className="api-pay">
      <span className="api-x402">x402</span>
      <b>{price}</b> USDC
    </span>
  )
}

function Row({ endpoint: ep, paidBase }: { endpoint: Endpoint; paidBase: string | null }) {
  const notes = [
    ep.capped ? 'The price is the cap, charged exact: Circle Gateway has no upto.' : null,
    ep.async ? 'Runs as a job: 202 and a receipt, settled when it finishes; a failed job is not charged.' : null,
    ep.freeTier ? 'Free to its owner, signed in.' : null,
    ep.note,
  ].filter((n): n is string => Boolean(n))
  return (
    <details className={`api-row${ep.price ? ' paid' : ''}`}>
      <summary>
        <span className="api-method">{ep.method}</span>
        <span className="api-endpoint">
          <code>
            {/* Breaks after a slash on a narrow screen, never inside a segment. */}
            {ep.path.split('/').map((segment, i) => (
              <span key={i}>
                {i > 0 && '/'}
                {segment}
                <wbr />
              </span>
            ))}
          </code>
          <span className="api-line">
            <b>{ep.name}</b> — {ep.summary}
          </span>
        </span>
        <span className="api-returns">{ep.returns ?? ''}</span>
        <Price price={ep.price} />
        <span className="api-arrow" aria-hidden="true">
          →
        </span>
      </summary>
      <div className="api-more">
        {notes.map((n) => (
          <p key={n} className="api-note">
            {n}
          </p>
        ))}
        <pre>{exampleCall(ep, window.location.origin, paidBase)}</pre>
      </div>
    </details>
  )
}
