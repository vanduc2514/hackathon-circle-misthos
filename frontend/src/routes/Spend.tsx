import { useQuery } from '@tanstack/react-query'
import { Link, useSearchParams } from 'react-router-dom'
import {
  api,
  money,
  shortTime,
  unwrap,
  type FinanceOut,
  type Publisher,
  type SpendOut,
} from '../lib/client'
import { Panel, Stat } from '../components/ui'

/**
 * The organisation's view: what it budgeted, committed, released and can file for a
 * security review, by category and against its own limits. The audit export is the
 * same record, unedited, for an auditor.
 */
export default function Spend() {
  const [params, setParams] = useSearchParams()

  const publishers = useQuery({
    queryKey: ['publishers'],
    queryFn: async () => (await api.GET('/api/v1/publishers')).data as Publisher[] | undefined,
  })
  const chosen = params.get('publisher') ?? publishers.data?.[0]?.id

  const spend = useQuery({
    queryKey: ['spend', chosen],
    enabled: Boolean(chosen),
    retry: false,
    // A plan without spend reporting answers 402 with the plan that has it.
    queryFn: async () =>
      unwrap(
        await api.GET('/api/v1/publishers/{publisher_id}/spend', {
          params: { path: { publisher_id: chosen as string } },
        }),
      ) as SpendOut,
  })

  // What caps every price: the declared budget, or less where the books say so (#43).
  const books = useQuery({
    queryKey: ['finance', chosen],
    enabled: Boolean(chosen),
    queryFn: async () =>
      (
        await api.GET('/api/v1/publishers/{publisher_id}/finance', {
          params: { path: { publisher_id: chosen as string } },
        })
      ).data as FinanceOut | undefined,
  })

  const s = spend.data
  const f = books.data
  const publisher = publishers.data?.find((p) => p.id === chosen)

  return (
    <>
      <div className="page-head">
        <div>
          <h1 className="page-title">Spend{s ? ` · ${s.name}` : ''}</h1>
          <p className="page-sub">
            What was budgeted, committed and released on dependency fixes in {s?.year ?? 'the year'},
            and what to file for a security review. Every figure is from the payment ledger.
          </p>
        </div>
        <div className="btn-row">
          <select
            className="btn"
            value={chosen ?? ''}
            onChange={(e) => setParams({ publisher: e.target.value })}
          >
            {publishers.data?.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </select>
          {chosen && (
            <a className="btn" href={`/api/v1/publishers/${chosen}/audit?format=csv`}>
              Audit export (CSV)
            </a>
          )}
        </div>
      </div>

      <div className="grid k4">
        <Stat label="Budget remaining" value={`$${money(s?.budget_remaining)}`} />
        <Stat label="Held in escrow now" value={`$${money(s?.committed_held)}`} />
        <Stat label="Released this year" value={`$${money(s?.released)}`} />
        <Stat label="Refunded this year" value={`$${money(s?.refunded)}`} />
      </div>

      {spend.error && (
        <div className="banner">
          {spend.error.message}{' '}
          <Link to="/plans" style={{ textDecoration: 'underline' }}>
            See the plans
          </Link>
        </div>
      )}

      {f && (
        <div className="banner">
          {f.connected && f.source ? (
            <>
              Read from {f.source}
              {f.as_of ? ` at ${shortTime(f.as_of)}` : ''}:{' '}
              {f.budget_remaining_usdc !== null && f.budget_remaining_usdc !== undefined
                ? `$${money(f.budget_remaining_usdc)} left in the budget`
                : 'no budget figure'}
              {f.cash_usdc ? `, $${money(f.cash_usdc)} in cash` : ''}. Prices are capped at $
              {money(f.caps_prices_at_usdc)}, the lower of that and the declared budget.
            </>
          ) : f.connected ? (
            <>Your books could not be read ({f.note}). The declared budget caps prices.</>
          ) : (
            <>
              Prices are capped at the declared budget, ${money(f.declared_budget_usdc)}. Connect
              Firefly III or a beancount ledger to cap them at what your books say is left.
            </>
          )}
          {f.connected && f.source && f.note ? ` Note: ${f.note}.` : ''}
        </div>
      )}

      {publisher?.approval_threshold_usdc && (
        <div className="banner">
          Releases over ${publisher.approval_threshold_usdc} wait for{' '}
          {(publisher.approvers ?? []).join(', ')}.
        </div>
      )}

      <div className="grid k2">
        <Panel title="By category" flush>
          <table className="table">
            <thead>
              <tr>
                <th>Label</th>
                <th style={{ textAlign: 'right' }}>Committed</th>
                <th style={{ textAlign: 'right' }}>Released</th>
                <th style={{ textAlign: 'right' }}>Monthly limit</th>
              </tr>
            </thead>
            <tbody>
              {s?.by_category.map((c) => (
                <tr key={c.label}>
                  <td>{c.label}</td>
                  <td className="mono-num" style={{ textAlign: 'right' }}>
                    ${money(c.committed)}
                  </td>
                  <td className="mono-num" style={{ textAlign: 'right' }}>
                    ${money(c.released)}
                  </td>
                  <td className="mono-num dim" style={{ textAlign: 'right' }}>
                    {c.limit ? `$${money(c.limit)}` : '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Panel>

        <Panel title="To file for a security review" flush>
          {s?.fileable.length ? (
            <table className="table">
              <thead>
                <tr>
                  <th>Fix</th>
                  <th style={{ textAlign: 'right' }}>Paid</th>
                  <th style={{ textAlign: 'right' }}>Settled</th>
                </tr>
              </thead>
              <tbody>
                {s.fileable.map((f) => (
                  <tr key={f.issue_id}>
                    <td>
                      <a href={f.github_url} target="_blank" rel="noreferrer" className="row-link">
                        <div className="cell-title">{f.title}</div>
                        <div className="cell-repo">
                          {f.repo}#{f.number} &middot; {f.acceptance_criteria.length} criteria met
                        </div>
                      </a>
                    </td>
                    <td className="mono-num" style={{ textAlign: 'right' }}>
                      ${money(f.amount)}
                    </td>
                    <td className="dim" style={{ textAlign: 'right' }}>
                      {shortTime(f.settled_at)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <div className="empty">No compliance or security fixes settled this year.</div>
          )}
        </Panel>
      </div>
    </>
  )
}
