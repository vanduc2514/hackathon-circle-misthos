import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { api, money, unwrap, type IssueOut, type MeOut, type Publisher } from '../lib/client'
import {
  MAX_CRITERION,
  criteriaLines,
  sameAsStored,
  tooLong,
} from '../lib/criteria'

/** Every explicit action an issue page can take, as the API names them. */
export type Act =
  | { kind: 'criteria'; criteria: string[] }
  | { kind: 'fund' }
  | { kind: 'claim' }
  | { kind: 'submit'; prNumber: number }
  | { kind: 'review' }
  | { kind: 'merge' }
  | { kind: 'decline'; reason: string }
  | { kind: 'dispute'; reason: string }
  | { kind: 'approve-release'; approver: string }
  | { kind: 'step' }
  | { kind: 'complete' }

export async function perform(issueId: string, act: Act): Promise<IssueOut> {
  const path = { params: { path: { issue_id: issueId } } }
  switch (act.kind) {
    case 'criteria':
      return unwrap(
        await api.POST('/api/v1/issues/{issue_id}/criteria', {
          ...path,
          body: { criteria: act.criteria },
        }),
      )
    case 'fund':
      return unwrap(await api.POST('/api/v1/issues/{issue_id}/fund', path))
    case 'claim':
      return unwrap(await api.POST('/api/v1/issues/{issue_id}/claim', path))
    case 'submit':
      return unwrap(
        await api.POST('/api/v1/issues/{issue_id}/submit', {
          ...path,
          body: { pr_number: act.prNumber },
        }),
      )
    case 'review':
      return unwrap(await api.POST('/api/v1/issues/{issue_id}/review', path))
    case 'merge':
      return unwrap(await api.POST('/api/v1/demo/issues/{issue_id}/merge', path))
    case 'decline':
      return unwrap(
        await api.POST('/api/v1/issues/{issue_id}/decline', {
          ...path,
          body: { reason: act.reason },
        }),
      )
    case 'dispute':
      return unwrap(
        await api.POST('/api/v1/issues/{issue_id}/dispute', {
          ...path,
          body: { reason: act.reason },
        }),
      )
    case 'approve-release':
      return unwrap(
        await api.POST('/api/v1/issues/{issue_id}/approve-release', {
          ...path,
          body: { approver: act.approver },
        }),
      )
    case 'step':
      return unwrap(await api.POST('/api/v1/issues/{issue_id}/advance', path))
    case 'complete':
      return unwrap(await api.POST('/api/v1/issues/{issue_id}/complete', path))
  }
}

/** Who the viewer is to this issue, which decides what they may do. */
export function roleOn(issue: IssueOut, me: MeOut | null | undefined) {
  const account = me?.account ?? null
  const linked = Boolean(account?.github_login)
  return {
    signedIn: Boolean(me),
    account,
    linked,
    publisher: linked && account?.party_id === issue.publisher_id,
    claimant: linked && Boolean(issue.contributor_id) && account?.party_id === issue.contributor_id,
    contributor: linked && account?.role === 'contributor',
  }
}

export default function IssueActions({
  issue,
  me,
  simulated,
}: {
  issue: IssueOut
  me: MeOut | null | undefined
  simulated: boolean
}) {
  const qc = useQueryClient()
  const who = roleOn(issue, me)

  const act = useMutation({
    mutationFn: (a: Act) => perform(issue.id, a),
    onSuccess: (updated) => {
      qc.setQueryData(['issue', issue.id], updated)
      for (const key of ['issue', 'timeline', 'issues', 'metrics', 'decisions', 'loop', 'spend']) {
        qc.invalidateQueries({ queryKey: [key] })
      }
    },
  })
  const busy = act.isPending
  const run = (a: Act) => act.mutate(a)

  let body: React.ReactNode
  switch (issue.state) {
    case 'AWAITING_APPROVAL':
      body = who.publisher ? (
        <ApproveAndFund issue={issue} busy={busy} run={run} />
      ) : (
        <Wait>
          {issue.publisher_name} approves the acceptance criteria, then the price. Funds are
          committed only on that second approval.
        </Wait>
      )
      break
    case 'FUNDED':
      body = who.contributor ? (
        <>
          <p className="dim action-hint">
            The first claim wins and holds the issue for 72 hours. Claiming commits you to
            nothing but trying.
          </p>
          <button className="btn primary" disabled={busy} onClick={() => run({ kind: 'claim' })}>
            Claim for ${money(issue.escrow?.amount ?? issue.proposal?.recommended)}
          </button>
        </>
      ) : (
        <SignInTo signedIn={who.signedIn} what="claim it as a contributor">
          Funded and open. The first contributor to claim it holds it.
        </SignInTo>
      )
      break
    case 'CLAIMED':
    case 'REWORK':
      body = who.claimant ? (
        <Submit issue={issue} simulated={simulated} busy={busy} run={run} />
      ) : (
        <Wait>
          {issue.state === 'REWORK'
            ? 'The review asked for changes. The claimant pushes to the same pull request.'
            : 'Claimed. Waiting for the claimant to open a pull request that fixes it.'}
        </Wait>
      )
      break
    case 'IN_REVIEW':
      body =
        who.publisher || who.claimant ? (
          <>
            <p className="dim action-hint">
              The review agent judges each criterion against the diff on its next pass. You
              can ask for it now.
            </p>
            <button className="btn primary" disabled={busy} onClick={() => run({ kind: 'review' })}>
              {busy ? 'Reviewing…' : 'Review it now'}
            </button>
          </>
        ) : (
          <Wait>The review agent is judging the pull request against the criteria.</Wait>
        )
      break
    case 'ACCEPTED':
      body = who.publisher ? (
        <Accepted issue={issue} simulated={simulated} busy={busy} run={run} />
      ) : (
        <Wait>
          {issue.awaiting_approver
            ? `The review passed and the release waits for one of ${issue.publisher_name}'s approvers.`
            : 'The review passed. Merging is acceptance and releases the payment; seven silent days release it too.'}
        </Wait>
      )
      break
    case 'REJECTED':
      body = who.claimant ? (
        <Reason
          label="Dispute the verdict"
          hint="A second reviewer looks again. Say which criteria are met and where the work shows it."
          busy={busy}
          onSubmit={(reason) => run({ kind: 'dispute', reason })}
        />
      ) : (
        <Wait>The review rejected the work. The claimant can dispute it once.</Wait>
      )
      break
    case 'PAID':
      body = <Wait>Closed. The contributor was paid ${money(issue.paid_usdc)}.</Wait>
      break
    case 'REFUNDED':
      body = <Wait>Closed. The funds went back to the publisher.</Wait>
      break
    default:
      body = <Wait>Nothing to do from {issue.state}.</Wait>
  }

  return (
    <>
      {body}
      {act.error && <div className="error-box form-error">{act.error.message}</div>}
      {simulated && !['PAID', 'REFUNDED'].includes(issue.state) && (
        <div className="demo-shortcuts">
          <span className="stat-hint">Demo shortcuts, acting for everyone at once:</span>
          <div className="btn-row">
            <button className="btn" disabled={busy} onClick={() => run({ kind: 'step' })}>
              One step
            </button>
            <button className="btn" disabled={busy} onClick={() => run({ kind: 'complete' })}>
              Run to settlement
            </button>
          </div>
        </div>
      )}
    </>
  )
}

type Step = { issue: IssueOut; busy: boolean; run: (a: Act) => void }

function Wait({ children }: { children: React.ReactNode }) {
  return <p className="dim">{children}</p>
}

function SignInTo({
  signedIn,
  what,
  children,
}: {
  signedIn: boolean
  what: string
  children: React.ReactNode
}) {
  return (
    <p className="dim">
      {children}{' '}
      {!signedIn && (
        <Link to="/account" style={{ textDecoration: 'underline' }}>
          Sign in to {what}.
        </Link>
      )}
    </p>
  )
}

function ApproveAndFund({ issue, busy, run }: Step) {
  const [draft, setDraft] = useState(issue.acceptance_criteria.join('\n'))
  const criteria = criteriaLines(draft)
  const approved = Boolean(issue.criteria_approved_at)
  // The server keeps the first MAX_CRITERION characters of each criterion, so the
  // comparison has to use the same shape; see lib/criteria.
  const overlong = tooLong(criteria)
  const unchanged = sameAsStored(criteria, issue.acceptance_criteria)

  return (
    <div className="form">
      <label className="field">
        <span>
          1. The acceptance criteria, one per line. The review judges the work against
          exactly these.
        </span>
        <textarea
          className="input"
          rows={Math.min(8, Math.max(3, criteria.length + 1))}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
        />
      </label>
      {overlong && (
        <p className="dim action-hint" style={{ marginTop: 6 }}>
          A criterion is longer than {MAX_CRITERION} characters. Only the first{' '}
          {MAX_CRITERION} are kept, so split it into two.
        </p>
      )}
      <div className="btn-row">
        <button
          className={`btn ${approved && unchanged ? '' : 'primary'}`}
          disabled={
            busy ||
            criteria.length === 0 ||
            criteria.length > 12 ||
            overlong ||
            (approved && unchanged)
          }
          onClick={() => run({ kind: 'criteria', criteria })}
        >
          {approved && unchanged ? 'Criteria approved' : 'Approve these criteria'}
        </button>
      </div>
      <p className="dim action-hint" style={{ marginTop: 6 }}>
        2. The price. Approving it commits ${money(issue.proposal?.recommended)} to the escrow
        contract, which only a merge, the grace period or the deadline can move.
      </p>
      <div className="btn-row">
        <button
          className="btn primary"
          disabled={busy || !approved || !unchanged}
          onClick={() => run({ kind: 'fund' })}
        >
          Approve the price and commit ${money(issue.proposal?.recommended)}
        </button>
      </div>
    </div>
  )
}

function Submit({ issue, simulated, busy, run }: Step & { simulated: boolean }) {
  const [prNumber, setPrNumber] = useState(issue.submission?.pr_number?.toString() ?? '')

  const open = useMutation({
    mutationFn: async () =>
      unwrap(
        await api.POST('/api/v1/demo/issues/{issue_id}/pull-request', {
          params: { path: { issue_id: issue.id } },
        }),
      ),
    onSuccess: (pr) => setPrNumber(String(pr.pr_number)),
  })

  return (
    <div className="form">
      <p className="dim action-hint">
        {issue.state === 'REWORK'
          ? `Push the requested changes to #${issue.submission?.pr_number}, then submit it again.`
          : `Open a pull request on ${issue.repo} that says "Fixes #${issue.number}", then submit it here. Only a pull request you opened counts.`}
      </p>
      {simulated && (
        <div className="btn-row">
          <button className="btn" disabled={open.isPending} onClick={() => open.mutate()}>
            {issue.state === 'REWORK'
              ? 'Push a new commit on the simulated GitHub'
              : 'Open it on the simulated GitHub'}
          </button>
        </div>
      )}
      {open.error && <div className="error-box form-error">{open.error.message}</div>}
      <form
        className="field-row"
        onSubmit={(e) => {
          e.preventDefault()
          run({ kind: 'submit', prNumber: Number(prNumber) })
        }}
      >
        <label className="field">
          <span>Pull request number</span>
          <input
            className="input"
            value={prNumber}
            onChange={(e) => setPrNumber(e.target.value)}
            inputMode="numeric"
            pattern="[0-9]+"
            required
          />
        </label>
        <button className="btn primary align-end" type="submit" disabled={busy || !prNumber}>
          Submit for review
        </button>
      </form>
    </div>
  )
}

function Accepted({ issue, simulated, busy, run }: Step & { simulated: boolean }) {
  const publishers = useQuery({
    queryKey: ['publishers'],
    enabled: issue.awaiting_approver,
    queryFn: async () => unwrap(await api.GET('/api/v1/publishers')) as Publisher[],
  })
  const approvers = publishers.data?.find((p) => p.id === issue.publisher_id)?.approvers ?? []
  const [approver, setApprover] = useState('')
  const pr = issue.submission?.pr_number

  if (issue.awaiting_approver) {
    return (
      <div className="form">
        <p className="dim action-hint">
          Merged. The payout is over your release threshold, so it waits for one of your named
          approvers.
        </p>
        <div className="field-row">
          <label className="field grow">
            <span>Approver</span>
            <select
              className="input"
              value={approver}
              onChange={(e) => setApprover(e.target.value)}
            >
              <option value="">Choose…</option>
              {approvers.map((a) => (
                <option key={a} value={a}>
                  {a}
                </option>
              ))}
            </select>
          </label>
          <button
            className="btn primary align-end"
            disabled={busy || !approver}
            onClick={() => run({ kind: 'approve-release', approver })}
          >
            Approve the release
          </button>
        </div>
      </div>
    )
  }

  return (
    <div className="form">
      <p className="dim action-hint">
        The review passed. Merging the pull request is your acceptance and releases the
        payment. If you do nothing for seven days, it releases anyway.
      </p>
      <div className="btn-row">
        {simulated ? (
          <button className="btn primary" disabled={busy} onClick={() => run({ kind: 'merge' })}>
            Merge #{pr} on the simulated GitHub
          </button>
        ) : (
          <a
            className="btn primary"
            href={`https://github.com/${issue.repo}/pull/${pr}`}
            target="_blank"
            rel="noreferrer"
          >
            Merge #{pr} on GitHub ↗
          </a>
        )}
      </div>
      <Reason
        label="Decline, with a reason"
        hint="Once per issue: the work goes back for rework, and after that the merge or the grace period settles it."
        busy={busy}
        onSubmit={(reason) => run({ kind: 'decline', reason })}
      />
    </div>
  )
}

function Reason({
  label,
  hint,
  busy,
  onSubmit,
}: {
  label: string
  hint: string
  busy: boolean
  onSubmit: (reason: string) => void
}) {
  const [open, setOpen] = useState(false)
  const [reason, setReason] = useState('')
  if (!open) {
    return (
      <div className="btn-row" style={{ marginTop: 10 }}>
        <button className="btn" onClick={() => setOpen(true)}>
          {label}
        </button>
      </div>
    )
  }
  return (
    <form
      className="form"
      style={{ marginTop: 10 }}
      onSubmit={(e) => {
        e.preventDefault()
        onSubmit(reason.trim())
      }}
    >
      <label className="field">
        <span>{hint}</span>
        <textarea
          className="input"
          rows={3}
          value={reason}
          maxLength={2000}
          onChange={(e) => setReason(e.target.value)}
          required
        />
      </label>
      <div className="btn-row">
        <button className="btn primary" type="submit" disabled={busy || !reason.trim()}>
          {label}
        </button>
        <button className="btn" type="button" onClick={() => setOpen(false)}>
          Cancel
        </button>
      </div>
    </form>
  )
}
