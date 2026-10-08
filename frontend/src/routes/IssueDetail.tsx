import { useQuery } from '@tanstack/react-query'
import { Link, useParams } from 'react-router-dom'
import {
  api,
  money,
  relativeTime,
  shortHash,
  shortTime,
  type EscrowReadback,
  type IssueOut,
  type TimelineEntry,
} from '../lib/client'
import { Bar, ChecksChip, Panel, StateBadge, Stepper } from '../components/ui'
import IssueActions, { roleOn } from '../components/IssueActions'
import { BridgeToArc } from '../components/BridgeToArc'
import { useHealth, useMe } from '../lib/session'

export default function IssueDetail() {
  const { issueId = '' } = useParams()
  const me = useMe()
  const health = useHealth()

  const { data: issue, isLoading, error } = useQuery({
    queryKey: ['issue', issueId],
    queryFn: async () => (await api.GET('/api/v1/issues/{issue_id}', { params: { path: { issue_id: issueId } } })).data as IssueOut | undefined,
  })

  // What the escrow itself reports, as opposed to the platform's record of it.
  const { data: onChain } = useQuery({
    queryKey: ['escrow', issueId],
    queryFn: async () =>
      (await api.GET('/api/v1/issues/{issue_id}/escrow', { params: { path: { issue_id: issueId } } })).data as EscrowReadback | undefined,
  })

  const { data: timeline } = useQuery({
    queryKey: ['timeline', issueId],
    queryFn: async () =>
      (await api.GET('/api/v1/issues/{issue_id}/timeline', { params: { path: { issue_id: issueId } } })).data as TimelineEntry[] | undefined,
  })

  if (isLoading) return <div className="empty">Loading…</div>
  if (error || !issue)
    return (
      <div className="error-box">
        Could not load {issueId}. <Link to="/issues">Back to issues</Link>
      </div>
    )

  const p = issue.proposal
  const signals = p ? Object.entries(p.signals) : []

  // Place the recommended price inside the band for the visual.
  const low = Number(p?.band_low?.usdc ?? 0)
  const high = Number(p?.band_high?.usdc ?? 0)
  const rec = Number(p?.recommended?.usdc ?? 0)
  const span = Math.max(high, 1)
  const rangeLeft = (low / span) * 100
  const rangeWidth = ((high - low) / span) * 100
  const markerLeft = Math.min(99.5, (rec / span) * 100)

  return (
    <>
      <div className="detail-head">
        <div className="detail-meta">
          <Link to="/issues" className="chip">
            ← Issues
          </Link>
          <span className="chip">{issue.repo}#{issue.number}</span>
          <StateBadge state={issue.state} />
          {issue.compliance_driven && <span className="chip warn">compliance-driven</span>}
          {issue.labels.map((l) => (
            <span className="chip" key={l}>
              {l}
            </span>
          ))}
          <a
            className="chip"
            href={issue.github_url}
            target="_blank"
            rel="noreferrer"
            style={{ marginLeft: 'auto' }}
          >
            Open on GitHub ↗
          </a>
        </div>
        <h1 className="detail-title">{issue.title}</h1>
        <p className="detail-summary">{issue.summary}</p>
        <div style={{ marginTop: 18 }}>
          <Stepper state={issue.state} />
        </div>
      </div>

      <div className="detail-cols">
        {/* ------------------------------------------------------- left column */}
        <div className="col">
          <Panel title="Acceptance criteria">
            <p className="stat-hint" style={{ marginBottom: 10 }}>
              {issue.criteria_approved_at
                ? `Approved by the publisher ${shortTime(issue.criteria_approved_at)}. The review judges the work against these.`
                : 'Drafted by the agent. The publisher approves them before any money is committed.'}
            </p>
            <ul className="criteria">
              {issue.acceptance_criteria.map((c) => (
                <li key={c}>{c}</li>
              ))}
            </ul>
          </Panel>

          {issue.submission && (
            <Panel title="Submission">
              <dl className="kv">
                <dt>Pull request</dt>
                <dd>
                  #{issue.submission.pr_number} &middot; {issue.submission.files_changed} files
                  &middot; <span style={{ color: 'var(--ok)' }}>+{issue.submission.additions}</span>{' '}
                  <span style={{ color: 'var(--bad)' }}>-{issue.submission.deletions}</span>
                </dd>
                <dt>Checks</dt>
                <dd>
                  <ChecksChip passed={issue.submission.checks_passed} />
                </dd>
                <dt>Commit</dt>
                <dd className="muted">{shortHash(issue.submission.head_sha)}</dd>
              </dl>
            </Panel>
          )}

          {issue.review && (
            <Panel title="Review">
              <dl className="kv" style={{ marginBottom: 14 }}>
                <dt>Verdict</dt>
                <dd>
                  <span className={`chip ${issue.review.verdict === 'accept' ? 'ok' : 'warn'}`}>
                    {issue.review.verdict}
                  </span>
                </dd>
                <dt>Decided</dt>
                <dd className="muted">{shortTime(issue.review.decided_at)}</dd>
              </dl>
              <ul className="findings">
                {issue.review.findings.map((f) => (
                  <li key={f}>{f}</li>
                ))}
              </ul>
            </Panel>
          )}

          <Panel title="Decision log" flush>
            {timeline?.length ? (
              <div style={{ padding: '4px 18px 10px' }}>
                <div className="timeline">
                  {[...timeline].reverse().map((t) => (
                    <div className="tl-item" key={t.id}>
                      <span className="tl-time">{relativeTime(t.created_at)}</span>
                      <span>
                        <span className="tl-action">
                          <span className="tl-actor">{t.actor}</span>
                          <span className="dim">{t.action.replace(/_/g, ' ')}</span>
                          {t.cost_usdc && t.cost_usdc !== '0.00' && (
                            <span className="chip" style={{ padding: '1px 6px' }}>
                              ${t.cost_usdc}
                            </span>
                          )}
                        </span>
                        <span className="tl-outcome">{t.outcome}</span>
                      </span>
                    </div>
                  ))}
                </div>
              </div>
            ) : (
              <div className="empty">Nothing logged yet.</div>
            )}
          </Panel>
        </div>

        {/* ------------------------------------------------------ right column */}
        <div className="col">
          {p ? (
            <Panel title="Proposed price">
              <div className="price-value">${money(p.recommended)}</div>
              <div className="band">
                <div className="band-track">
                  <span
                    className="band-range"
                    style={{ left: `${rangeLeft}%`, width: `${rangeWidth}%` }}
                  />
                  <span className="band-marker" style={{ left: `${markerLeft}%` }} />
                </div>
                <div className="band-labels">
                  <span>${money(p.band_low)}</span>
                  <span>band</span>
                  <span>${money(p.band_high)}</span>
                </div>
              </div>

              <div style={{ marginTop: 16 }}>
                <div className="price-total">
                  <span className="dim">Fix price</span>
                  <span className="mono-num">${money(p.recommended)}</span>
                </div>
                <div className="price-total">
                  <span className="dim">Effort estimate</span>
                  <span className="mono-num">{p.estimated_hours}h</span>
                </div>
              </div>

              <div className="justify">{p.justification}</div>
              <div className="confidence-note">
                Confidence: {p.confidence}.{' '}
                {p.comparables?.length
                  ? 'Compared with settled issues of similar shape:'
                  : p.comparables_note}
              </div>
              {p.comparables && p.comparables.length > 0 && (
                <ul className="comparables">
                  {p.comparables.map((c) => (
                    <li key={c.issue_id}>
                      <Link to={`/issues/${c.issue_id}`}>
                        <span className="cell-title">{c.title}</span>
                        <span className="cell-repo">
                          {c.repo} &middot; settled {shortTime(c.settled_at)}
                        </span>
                      </Link>
                      <span className="mono-num">${money(c.price_usdc)}</span>
                    </li>
                  ))}
                </ul>
              )}
            </Panel>
          ) : (
            <Panel title="Proposed price">
              <div className="empty">No proposal yet.</div>
            </Panel>
          )}

          {signals.length > 0 && (
            <Panel title="Complexity signals">
              <div className="bars">
                {signals.map(([name, value]) => (
                  <Bar key={name} label={name} value={value} max={5} cool />
                ))}
              </div>
              <p className="stat-hint" style={{ marginTop: 12 }}>
                Scored 1 to 5. Complexity score {p?.complexity_score}, weighted.
              </p>
            </Panel>
          )}

          {issue.escrow && (
            <Panel title="Escrow">
              <dl className="kv">
                <dt>Committed</dt>
                <dd className="mono-num">${money(issue.escrow.amount)}</dd>
                <dt>Chain</dt>
                <dd>{issue.escrow.chain}</dd>
                <dt>Contract</dt>
                <dd className="muted">{shortHash(issue.escrow.contract)}</dd>
                <dt>Tx</dt>
                <dd className="muted">{shortHash(issue.escrow.tx_hash)}</dd>
                <dt>Deadline</dt>
                <dd>{issue.deadline ? relativeTime(issue.deadline) : '—'}</dd>
                <dt>Status</dt>
                <dd>
                  {issue.escrow.released ? (
                    <span className="chip ok">released</span>
                  ) : issue.escrow.refunded ? (
                    <span className="chip">refunded</span>
                  ) : (
                    <span className="chip accent">held</span>
                  )}
                </dd>
                {onChain && (
                  <>
                    <dt>Escrow says</dt>
                    <dd>
                      <a href={onChain.explorer_url} target="_blank" rel="noreferrer">
                        {onChain.status}
                      </a>{' '}
                      <span className={onChain.source === 'chain' ? 'chip ok' : 'chip'}>
                        {onChain.source === 'chain' ? 'read from the contract' : 'simulated'}
                      </span>
                    </dd>
                  </>
                )}
              </dl>
              <p className="stat-hint" style={{ marginTop: 12 }}>
                Held by a contract, not by us. We are never in a position to keep it.
              </p>
            </Panel>
          )}

          {issue.paid_usdc && (
            <Panel title="Settlement">
              <div className="price-total">
                <span className="dim">Paid to contributor</span>
                <span className="mono-num" style={{ color: 'var(--ok)' }}>
                  ${money(issue.paid_usdc)}
                </span>
              </div>
              {issue.platform_fee_usdc && (
                <div className="price-total" style={{ marginTop: 8 }}>
                  <span className="dim">Platform take rate</span>
                  <span className="mono-num">${money(issue.platform_fee_usdc)}</span>
                </div>
              )}
            </Panel>
          )}

          <Panel title="Next step">
            // Keyed by issue id: React Router changes the `issue` prop without unmounting this
          // component when only the id in the route changes, and every panel below seeds
          // state from that prop. Without the key the criteria box, the pull request
          // number and the decline reason all survive into the next issue.
          <IssueActions
            key={issue.id}
            issue={issue}
            me={me.data}
            simulated={health.data?.simulated ?? false}
          />
          </Panel>

          {issue.state === 'AWAITING_APPROVAL' && roleOn(issue, me.data).publisher && (
            <BridgeToArc suggested={p ? String(p.recommended.usdc) : undefined} />
          )}

          <Panel title="Parties">
            <dl className="kv">
              <dt>Publisher</dt>
              <dd>{issue.publisher_name}</dd>
              <dt>Contributor</dt>
              <dd>{issue.contributor_id ?? '— not claimed'}</dd>
              <dt>Opened</dt>
              <dd className="muted">{shortTime(issue.created_at)}</dd>
            </dl>
          </Panel>
        </div>
      </div>
    </>
  )
}
