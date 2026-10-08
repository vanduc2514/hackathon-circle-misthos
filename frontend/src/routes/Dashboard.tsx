import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { api, money, relativeTime, unwrap, type Decision, type IssueSummaryOut, type MetricsOut } from '../lib/client'
import { Bar, Panel, Stat, StateBadge } from '../components/ui'

export default function Dashboard() {
  const qc = useQueryClient()

  const metrics = useQuery({
    queryKey: ['metrics'],
    queryFn: async () => (await api.GET('/api/v1/metrics')).data as MetricsOut | undefined,
  })
  const decisions = useQuery({
    queryKey: ['decisions'],
    queryFn: async () => (await api.GET('/api/v1/decisions', { params: { query: { limit: 12 } } })).data as Decision[] | undefined,
  })
  const issues = useQuery({
    queryKey: ['issues'],
    queryFn: async () => (await api.GET('/api/v1/issues')).data as IssueSummaryOut[] | undefined,
  })

  // Refused with a reason where a database is configured and reset is not allowed.
  const reset = useMutation({
    mutationFn: async () => unwrap(await api.POST('/api/v1/demo/reset')),
    onSuccess: () => qc.invalidateQueries(),
  })

  const m = metrics.data
  const rows = issues.data ?? []
  const byState = m?.by_state ?? {}
  const maxState = Math.max(1, ...Object.values(byState))

  return (
    <>
      <div className="page-head">
        <div>
          <h1 className="page-title">The treasury, running itself</h1>
          <p className="page-sub">
            A company or a maintainer puts a fixed price on a GitHub issue. An agent prices
            it, a contributor fixes it, an agent reviews it, and the payment clears when the
            work is accepted.
          </p>
        </div>
        <div className="btn-row">
          <button className="btn" onClick={() => reset.mutate()} disabled={reset.isPending}>
            {reset.isPending ? 'Resetting…' : 'Reset simulation'}
          </button>
        </div>
      </div>
      {reset.error && <div className="error-box form-error">{reset.error.message}</div>}

      <div className="banner">
        Simulated build. The lifecycle, the pricing engine, the review agent and the
        decision log are real code; the money is fake and no chain is contacted. GitHub
        is simulated until a GitHub App is configured.
      </div>

      <div className="hero">
        <div className="hero-cell">
          <div className="hero-label">North star &middot; settled issues this week</div>
          <div className="hero-value">{m?.settled_issues_7d ?? '—'}</div>
          <div className="hero-note">
            An issue counts only when a pull request merged and the payment released.
            {m ? ` ${m.settled_issues} settled in all.` : ''}
            {m?.median_hours_to_payout != null &&
              ` Median ${m.median_hours_to_payout}h from claim to payout.`}
          </div>
        </div>
        <div className="hero-cell">
          <div className="hero-label">Why a fixed price rather than a bid</div>
          <p className="hero-quote" style={{ marginTop: 10 }}>
            &ldquo;Athens paid a juror, a rower, a builder on the Acropolis the day they
            worked, because nobody earning two obols could float a month of credit to the
            state.&rdquo;
          </p>
          <div className="hero-note" style={{ marginTop: 10 }}>
            At a cent a transaction, paying on delivery is finally a choice.
          </div>
        </div>
      </div>

      <div className="grid k4">
        <Stat
          label="Matched volume"
          value={`$${money(m?.matched_volume_usdc)}`}
          hint="Total value of work that changed hands"
        />
        <Stat
          label="Funded issues"
          value={m?.funded_issues_published ?? '—'}
          hint={`${m?.open_issues ?? 0} still open`}
        />
        <Stat
          label="Settled issues"
          value={m?.settled_issues ?? '—'}
          hint="Merged and paid end to end. The North Star."
        />
        <Stat
          label="Publisher overturns"
          value={m ? `${Math.round(m.publisher_overturn_rate * 100)}%` : '—'}
          hint="How often a publisher declined a passing verdict"
        />
      </div>

      <div className="grid k3">
        <Stat
          label="Claim rate within 72h"
          value={m ? `${Math.round(m.claim_rate_72h * 100)}%` : '—'}
          hint="An unclaimed issue is how you lose a publisher"
        />
        <Stat
          label="Acceptance, first review"
          value={m ? `${Math.round(m.acceptance_rate_first_review * 100)}%` : '—'}
          hint="Low means bad scoping or the wrong contributors"
        />
        <Stat
          label="Repeat publishers"
          value={m ? `${Math.round(m.repeat_publisher_rate * 100)}%` : '—'}
          hint="A marketplace of first transactions has no business under it"
        />
      </div>

      <div className="grid k4">
        <Stat
          label="Refund rate"
          value={m ? `${Math.round(m.refund_rate * 100)}%` : '—'}
          hint="Commitments that closed with the money going back. Target under 15%"
        />
        <Stat
          label="Dispute rate"
          value={m ? `${Math.round(m.dispute_rate * 100)}%` : '—'}
          hint="Submissions whose contributor disputed a verdict. Target under 5%"
        />
        <Stat
          label="Paid $500+ this month"
          value={m?.earners_over_500_share != null ? `${Math.round(m.earners_over_500_share * 100)}%` : '—'}
          hint="Of contributors paid in the last 30 days. Target above 30%"
        />
        <Stat
          label="Top 10 share of payouts"
          value={m?.top10_payout_share != null ? `${Math.round(m.top10_payout_share * 100)}%` : '—'}
          hint="A marketplace, not a roster. Target under 50%"
        />
      </div>

      <div className="grid k2">
        <Panel title="Where the issues are">
          <div className="bars">
            {Object.entries(byState).map(([state, count]) => (
              <Bar key={state} label={state} value={count} max={maxState} />
            ))}
          </div>
        </Panel>

        <Panel title="Signals that decide the price">
          <div className="bars">
            <Bar label="code surface" value={25} max={25} />
            <Bar label="requirement clarity" value={20} max={25} />
            <Bar label="test coverage" value={15} max={25} />
            <Bar label="dependency depth" value={15} max={25} />
            <Bar label="prior attempts" value={15} max={25} />
            <Bar label="blast radius" value={10} max={25} />
          </div>
          <p className="stat-hint" style={{ marginTop: 14 }}>
            Weights, summing to 100%. A requirement that is unclear hurts twice: it raises
            the price and it raises the chance a submission misses the mark.
          </p>
        </Panel>
      </div>

      <div className="grid k2">
        <Panel title="Recent decisions" flush>
          {decisions.data?.length ? (
            <div style={{ padding: '4px 18px 10px' }}>
              <div className="timeline">
                {decisions.data.map((d) => (
                  <div className="tl-item" key={d.id}>
                    <span className="tl-time">{relativeTime(d.created_at)}</span>
                    <span>
                      <span className="tl-action">
                        <span className="tl-actor">{d.actor}</span>
                        <span className="dim">{d.action.replace(/_/g, ' ')}</span>
                      </span>
                      <span className="tl-outcome">{d.outcome}</span>
                    </span>
                  </div>
                ))}
              </div>
            </div>
          ) : (
            <div className="empty">No decisions yet.</div>
          )}
        </Panel>

        <Panel title="Issues needing a human" flush>
          <table className="table">
            <thead>
              <tr>
                <th>Issue</th>
                <th>State</th>
                <th style={{ textAlign: 'right' }}>Price</th>
              </tr>
            </thead>
            <tbody>
              {rows
                .filter((r) =>
                  ['AWAITING_APPROVAL', 'IN_REVIEW', 'REWORK'].includes(r.state),
                )
                .slice(0, 6)
                .map((r) => (
                  <tr key={r.id}>
                    <td>
                      <Link to={`/issues/${r.id}`} className="row-link">
                        <div className="cell-title">{r.title}</div>
                        <div className="cell-repo">
                          {r.repo}#{r.number} &middot; {r.publisher_name}
                        </div>
                      </Link>
                    </td>
                    <td>
                      <StateBadge state={r.state} />
                    </td>
                    <td className="mono-num" style={{ textAlign: 'right' }}>
                      ${money(r.price_usdc)}
                    </td>
                  </tr>
                ))}
            </tbody>
          </table>
        </Panel>
      </div>
    </>
  )
}
