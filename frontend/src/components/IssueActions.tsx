import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, money, unwrap, type IssueOut, type MeOut, type Publisher } from '../lib/client'
import {
  MAX_CRITERION,
  criteriaLines,
  sameAsStored,
  tooLong,
} from '../lib/criteria'
import { MINIMUM_PRICE, contributorShare, parseAmount, platformFee } from '../lib/fee'
import { fundFromWallet, type Wallet } from '../lib/fund'
import { GateLink } from './SignInGate'

function browserWallet(): Wallet | null {
  return (window as unknown as { ethereum?: Wallet }).ethereum ?? null
}

/** Every explicit action an issue page can take, as the API names them. */
export type Act =
  | { kind: 'criteria'; criteria: string[] }
  | { kind: 'price'; amount: string }
  | { kind: 'fund' }
  | { kind: 'claim' }
  | { kind: 'submit'; prNumber: number }
  | { kind: 'review' }
  | { kind: 'decline'; reason: string }
  | { kind: 'dispute'; reason: string }
  | { kind: 'approve-release' }

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
    case 'price':
      // The publisher's own number, movable until the money is committed (#142).
      return unwrap(
        await api.PATCH('/api/v1/issues/{issue_id}/price', {
          ...path,
          body: { amount: act.amount },
        }),
      )
    case 'fund':
      // The approval records the ceiling and waits for the publisher's own wallet to
      // commit (see lib/fund). The
      // amount is the price the publisher set when they published the issue, so there is
      // none to pass: a second place to name it would be a second thing to audit. The
      // escrow readback is what decides whether the wallet is asked at all, because a
      // refusal's own words may not survive the trip here (#147).
      return fundFromWallet(
        {
          fund: async () => unwrap(await api.POST('/api/v1/issues/{issue_id}/fund', path)),
          plan: async () => unwrap(await api.GET('/api/v1/issues/{issue_id}/commitment', path)),
          escrow: async () => unwrap(await api.GET('/api/v1/issues/{issue_id}/escrow', path)),
        },
        browserWallet(),
      )
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
      // No name in the body: the approver is whoever is signed in, by their linked
      // GitHub login, and the server checks the organisation named it.
      return unwrap(await api.POST('/api/v1/issues/{issue_id}/approve-release', path))
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
}: {
  issue: IssueOut
  me: MeOut | null | undefined
}) {
  const qc = useQueryClient()
  const who = roleOn(issue, me)

  const act = useMutation({
    mutationFn: (a: Act) => perform(issue.id, a),
    onSuccess: (updated) => {
      qc.setQueryData(['issue', issue.id], updated)
      for (const key of ['issue', 'escrow', 'timeline', 'issues', 'metrics', 'decisions']) {
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
            Claim for ${money(issue.escrow?.amount ?? issue.price_usdc)}
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
        <Submit issue={issue} busy={busy} run={run} />
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
      if (issue.awaiting_approver) {
        // Anyone the organisation named approves, signed in as themselves; the
        // contributor being paid never does.
        body =
          who.linked && !who.claimant ? (
            <ApproveRelease
              issue={issue}
              login={who.account?.github_login ?? ''}
              own={who.publisher}
              busy={busy}
              run={run}
            />
          ) : (
            <SignInTo signedIn={who.signedIn} what="approve it if you are one of them">
              The review passed and the release waits for one of {issue.publisher_name}'s named
              approvers.
            </SignInTo>
          )
      } else {
        body = who.publisher ? (
          <Accepted issue={issue} busy={busy} run={run} />
        ) : (
          <Wait>
            The review passed. Merging is acceptance and releases the payment; seven silent days
            release it too.
          </Wait>
        )
      }
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
        <GateLink to="/account" style={{ textDecoration: 'underline' }}>
          Sign in to {what}.
        </GateLink>
      )}
    </p>
  )
}

function ApproveAndFund({ issue, busy, run }: Step) {
  const [draft, setDraft] = useState(issue.acceptance_criteria.join('\n'))
  // The price is the publisher's, named when they published the issue. This step approves
  // what they set rather than picking a number: the engine's reading of the issue sits
  // beside it as advice, and nothing it says can refuse a price (#142).
  const price = Number(issue.price_usdc ?? 0)
  const priced = issue.price_usdc != null
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
          exactly these, so each must name something a reviewer can check: a test, a file,
          an output or a number.
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
        2. The price. ${money(issue.price_usdc)} is what you said this work is worth, and
        approving it commits that to the escrow contract, which only a merge, the grace period
        or the deadline can move. The contributor receives $
        {contributorShare(price).toFixed(2)} of it; Misthos keeps $
        {platformFee(price).toFixed(2)}.
        {' Your wallet sends the commitment itself: it asks you to sign two transactions, ' +
          'letting the escrow take the amount, then committing it. The platform never holds it.'}
      </p>
      <ChangePrice issue={issue} busy={busy} run={run} />
      <div style={{ marginTop: 12 }}>
        <div className="price-total">
          <span className="dim">The contributor receives</span>
          <span className="mono-num">${contributorShare(price).toFixed(2)}</span>
        </div>
        <div className="price-total">
          <span className="dim">Misthos keeps (10%)</span>
          <span className="mono-num">${platformFee(price).toFixed(2)}</span>
        </div>
      </div>
      <div className="btn-row">
        <button
          className="btn primary"
          disabled={busy || !approved || !unchanged || !priced}
          onClick={() => run({ kind: 'fund' })}
        >
          {priced ? `Approve and commit $${money(issue.price_usdc)}` : 'No price set yet'}
        </button>
      </div>
    </div>
  )
}

/** The publisher's price is theirs to move until the money is committed (#142). */
function ChangePrice({ issue, busy, run }: Step) {
  const [draft, setDraft] = useState('')
  const named = parseAmount(draft)
  const usable = !Number.isNaN(named) && named >= MINIMUM_PRICE

  return (
    <div className="field-row" style={{ marginTop: 12 }}>
      <label className="field grow">
        <span>Change the price, in USDC</span>
        <input
          className="input"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          inputMode="decimal"
          pattern="[0-9]*[.,]?[0-9]*"
          placeholder={issue.price_usdc ?? ''}
        />
        <span className={usable || draft.trim() === '' ? 'field-note' : 'field-note bad'}>
          {draft.trim() === ''
            ? `Any amount from $${MINIMUM_PRICE}. Nothing is committed until you approve it below.`
            : usable
              ? `You would pay $${named.toFixed(2)}: $${contributorShare(named).toFixed(2)} to the contributor, $${platformFee(named).toFixed(2)} to Misthos.`
              : `Any amount from $${MINIMUM_PRICE}.`}
        </span>
      </label>
      <div className="btn-row">
        <button
          className="btn"
          disabled={busy || !usable}
          onClick={() => run({ kind: 'price', amount: draft.trim() })}
        >
          Set the price
        </button>
      </div>
    </div>
  )
}

function Submit({ issue, busy, run }: Step) {
  const [prNumber, setPrNumber] = useState(issue.submission?.pr_number?.toString() ?? '')
  // The commit the review already judged. A commit gets one verdict, so after a rework
  // verdict or a decline the work goes back as a new commit. The same one is refused:
  // in review it would wait for a review that never comes (#119).
  const judged = issue.state === 'REWORK' ? (issue.review?.head_sha ?? null) : null

  return (
    <div className="form">
      <p className="dim action-hint">
        {issue.state === 'REWORK'
          ? `Push the requested changes to #${issue.submission?.pr_number} as a new commit, then submit it again.${judged ? ` The review already judged ${judged.slice(0, 7)}, and a commit is judged once.` : ''}`
          : `Open a pull request on ${issue.repo} that says "Fixes #${issue.number}", then submit it here. Only a pull request you opened counts.`}
      </p>
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
        <button
          className="btn primary align-end"
          type="submit"
          disabled={busy || !prNumber}
        >
          Submit for review
        </button>
      </form>
    </div>
  )
}

function ApproveRelease({
  issue,
  login,
  own,
  busy,
  run,
}: Step & { login: string; own: boolean }) {
  // The policy is the organisation's to see, so only its own account lists the names.
  const publishers = useQuery({
    queryKey: ['publishers'],
    enabled: own,
    queryFn: async () => unwrap(await api.GET('/api/v1/publishers')) as Publisher[],
  })
  const approvers = publishers.data?.find((p) => p.id === issue.publisher_id)?.approvers ?? []

  return (
    <div className="form">
      <p className="dim action-hint">
        The work is accepted, and the payout is over{' '}
        {own ? 'your' : `${issue.publisher_name}'s`} release threshold, so it waits for one of
        the named approvers
        {own && approvers.length ? `: ${approvers.map((a) => `@${a}`).join(', ')}` : ''}. Approving
        signs as @{login}, the GitHub login linked to your account, and counts only if that
        login is named.
      </p>
      <div className="btn-row">
        <button
          className="btn primary"
          disabled={busy || !login}
          onClick={() => run({ kind: 'approve-release' })}
        >
          Approve the release as @{login}
        </button>
      </div>
    </div>
  )
}

function Accepted({ issue, busy, run }: Step) {
  const pr = issue.submission?.pr_number

  return (
    <div className="form">
      <p className="dim action-hint">
        The review passed. Merging the pull request is your acceptance and releases the
        payment. If you do nothing for seven days, it releases anyway.
      </p>
      <div className="btn-row">
        <a
          className="btn primary"
          href={`https://github.com/${issue.repo}/pull/${pr}`}
          target="_blank"
          rel="noreferrer"
        >
          Merge #{pr} on GitHub ↗
        </a>
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
