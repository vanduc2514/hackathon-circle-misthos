import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useParams } from 'react-router-dom'
import {
  api,
  money,
  relativeTime,
  shortHash,
  shortTime,
  type IssueOut,
  type TimelineEntry,
} from '../lib/client'
import { Bar, Panel, StateBadge, Stepper } from '../components/ui'

/** The one step the demo runner can take from the current state. */
function nextAction(state: string): { label: string; hint: string } | null {
  switch (state) {
    case 'AWAITING_APPROVAL':
      return {
        label: 'Approve price and commit funds',
        hint: 'Human checkpoint: the agent cannot commit money.',
      }
    case 'FUNDED':
      return { label: 'Claim the issue', hint: 'First claim wins, held for 72 hours.' }
    case 'CLAIMED':
      return { label: 'Submit a pull request', hint: 'Runs the project’s own checks.' }
    case 'IN_REVIEW':
      return { label: 'Issue the verdict', hint: 'The platform reviews; no human confirms it.' }
    case 'REWORK':
      return { label: 'Resubmit after rework', hint: 'Rework rounds are bounded.' }
    case 'ACCEPTED':
      return { label: 'Merge and release', hint: 'Merge is acceptance. Silence for 7 days releases too.' }
    default:
      return null
  }
}

export default function IssueDetail() {
  const { issueId = '' } = useParams()
  const qc = useQueryClient()

  const { data: issue, isLoading, error } = useQuery({
    queryKey: ['issue', issueId],
    queryFn: async () => (await api.GET('/api/v1/issues/{issue_id}', { params: { path: { issue_id: issueId } } })).data as IssueOut | undefined,
  })

  const { data: timeline } = useQuery({
    queryKey: ['timeline', issueId],
    queryFn: async () =>
      (await api.GET('/api/v1/issues/{issue_id}/timeline', { params: { path: { issue_id: issueId } } })).data as TimelineEntry[] | undefined,
  })

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ['issue', issueId] })
    qc.invalidateQueries({ queryKey: ['timeline', issueId] })
    qc.invalidateQueries({ queryKey: ['issues'] })
    qc.invalidateQueries({ queryKey: ['metrics'] })
    qc.invalidateQueries({ queryKey: ['decisions'] })
  }

  const step = useMutation({
    mutationFn: async () =>
      (await api.POST('/api/v1/issues/{issue_id}/advance', { params: { path: { issue_id: issueId } } })).data,
    onSuccess: invalidate,
  })

  const complete = useMutation({
    mutationFn: async () =>
      (await api.POST('/api/v1/issues/{issue_id}/complete', { params: { path: { issue_id: issueId } } })).data,
    onSuccess: invalidate,
  })

  if (isLoading) return <div className="empty">Loading…</div>
  if (error || !issue)
    return (
      <div className="error-box">
        Could not load {issueId}. <Link to="/issues">Back to issues</Link>
      </div>
    )

  const p = issue.proposal
  const action = nextAction(issue.state)
  const isSettled = ['PAID', 'REFUNDED'].includes(issue.state)
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
                  {issue.submission.checks_passed ? (
                    <span className="chip ok">passing</span>
                  ) : (
                    <span className="chip bad">failing</span>
                  )}
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
                Confidence: {p.confidence}. {p.comparables_note}
              </div>
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
            </Panel>
          )}

          <Panel title="Next step">
            {isSettled ? (
              <p className="dim">
                This issue is closed. {issue.state === 'PAID' ? 'The contributor was paid.' : 'Funds returned to the publisher.'}
              </p>
            ) : action ? (
              <>
                <p className="dim" style={{ marginBottom: 12 }}>
                  {action.hint}
                </p>
                <div className="btn-row">
                  <button
                    className="btn primary"
                    onClick={() => step.mutate()}
                    disabled={step.isPending}
                  >
                    {step.isPending ? 'Working…' : action.label}
                  </button>
                  <button
                    className="btn"
                    onClick={() => complete.mutate()}
                    disabled={complete.isPending}
                  >
                    {complete.isPending ? 'Working…' : 'Run to settlement'}
                  </button>
                </div>
              </>
            ) : (
              <p className="dim">Nothing to do from {issue.state}.</p>
            )}
            {(step.error || complete.error) && (
              <div className="error-box" style={{ marginTop: 12 }}>
                {String((step.error ?? complete.error) as Error)?.slice(0, 200)}
              </div>
            )}
          </Panel>

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
