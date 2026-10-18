import { useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate } from 'react-router-dom'
import { ApiError, api, unwrap } from '../lib/client'
import { MINIMUM_PRICE, contributorShare, parseAmount, platformFee } from '../lib/fee'
import { useMe } from '../lib/session'
import { Panel } from '../components/ui'
import { GateLink } from '../components/SignInGate'

/**
 * A publisher names a GitHub issue and the price they will pay for it, and gets its
 * acceptance criteria drafted. Nothing is committed here: the publisher approves the
 * criteria and then the price on the issue's page, and only that second approval moves
 * money. The price is theirs to set, and the engine's reading of the issue is offered
 * beside it as advice (#142).
 */
export default function Publish() {
  const me = useMe()
  const navigate = useNavigate()
  const qc = useQueryClient()

  const [repo, setRepo] = useState('')
  const [number, setNumber] = useState('')
  const [creating, setCreating] = useState(false)
  const [title, setTitle] = useState('')
  const [summary, setSummary] = useState('')
  const [labels, setLabels] = useState('')
  const [price, setPrice] = useState('')
  const [escrowTerm, setEscrowTerm] = useState('14')
  const [compliance, setCompliance] = useState(false)

  // The price is the publisher's, so the form is what makes it usable: a number, and
  // at least the platform's minimum.
  const named = parseAmount(price)
  const priced = !Number.isNaN(named) && named >= MINIMUM_PRICE

  const account = me.data?.account ?? null
  const ready = account?.role === 'publisher' && Boolean(account.github_login)
  const publisherId = account?.party_id ?? ''

  // The repositories this publisher installed the App on. The picker offers what
  // GitHub already told us, rather than asking the publisher to spell it (#199).
  const repositories = useQuery({
    queryKey: ['repositories', publisherId],
    enabled: Boolean(publisherId),
    queryFn: async () =>
      unwrap(
        await api.GET('/api/v1/publishers/{publisher_id}/repositories', {
          params: { path: { publisher_id: publisherId } },
        }),
      ),
  })
  const connected = repositories.data?.repositories ?? []

  // The open issues in the chosen repository, with any listing they already have.
  const issues = useQuery({
    queryKey: ['github-issues', publisherId, repo],
    enabled: Boolean(publisherId && repo),
    queryFn: async () =>
      unwrap(
        await api.GET('/api/v1/publishers/{publisher_id}/github/issues', {
          params: { path: { publisher_id: publisherId }, query: { repo } },
        }),
      ),
  })
  const available = issues.data?.issues ?? []
  const chosen = available.find((issue) => String(issue.number) === number) ?? null
  // A chosen issue that already has a listing cannot be published twice; the form
  // points at the listing instead of letting the API refuse it after the fact.
  const linked = chosen && !chosen.listed_issue_id ? chosen : null
  // Set the moment the form is sent. The button is disabled by isPending, but that
  // reaches it a render later, and a double click inside that window published the
  // same GitHub issue twice (#120).
  const sending = useRef(false)

  const publish = useMutation({
    mutationFn: async () =>
      unwrap(
        await api.POST('/api/v1/issues', {
          body: {
            repo,
            // Linking a picker issue sends its number; creating sends none and lets
            // the API open it on GitHub, as the form used to (#199).
            number: creating ? null : Number(number),
            // GitHub is the source of truth for a linked issue, so the creation fields
            // are not sent with it.
            title: creating ? title.trim() : '',
            summary: creating ? summary.trim() : '',
            labels: creating
              ? labels
                  .split(',')
                  .map((l) => l.trim())
                  .filter(Boolean)
              : [],
            // The publisher's own number, and the amount the escrow commits (#142).
            price: price.trim(),
            // The escrow term in whole days, between 3 and 90 (#172).
            escrow_term: escrowTerm.trim() ? Number(escrowTerm.trim()) : 14,
            // The API publishes as the signed-in publisher whatever this says.
            publisher_id: account?.party_id ?? '',
            compliance_driven: compliance,
          },
        }),
      ),
    onSuccess: (issue) => {
      qc.invalidateQueries({ queryKey: ['issues'] })
      qc.invalidateQueries({ queryKey: ['metrics'] })
      // The listing now answers with an issue_id for the picker, so it cannot be
      // published a second time without saying so.
      qc.invalidateQueries({ queryKey: ['github-issues'] })
      navigate(`/issues/${issue.id}`)
    },
    onSettled: () => {
      sending.current = false
    },
  })
  // A GitHub issue that is already open on Misthos: the refusal names its listing.
  const listed = publish.error instanceof ApiError ? publish.error.issueId : null

  return (
    <>
      <div className="page-head">
        <div>
          <h1 className="page-title">Publish an issue</h1>
          <p className="page-sub">
            Choose the GitHub issue you want fixed, or create one, and name the price you will
            pay for it. The pricing engine reads the issue and shows you what work of this shape
            has settled for, which is advice and nothing more: the price you set is the price
            that binds. You approve the acceptance criteria, then the price, and only then are
            funds committed.
          </p>
        </div>
      </div>

      {me.isLoading ? (
        <div className="empty">Loading…</div>
      ) : !ready ? (
        <div className="banner">
          {!me.data
            ? 'Sign in as a publisher to publish an issue.'
            : account?.role === 'contributor'
              ? 'This wallet is a contributor. Publishing needs a publisher wallet.'
              : 'Choose the publisher side and link your GitHub account first.'}{' '}
          <GateLink to="/account" style={{ textDecoration: 'underline' }}>
            Go to your account
          </GateLink>
        </div>
      ) : (
        <div className="form-stack">
          <Panel title="The issue">
            <form
              className="form"
              onSubmit={(e) => {
                e.preventDefault()
                if (sending.current) return
                sending.current = true
                publish.mutate()
              }}
            >
              <div className="field-row">
                <label className="field grow">
                  <span>Repository</span>
                  <select
                    className="input"
                    aria-label="Repository"
                    value={repo}
                    onChange={(e) => {
                      setRepo(e.target.value)
                      // The issue belongs to the repository, so a new one clears it.
                      setNumber('')
                    }}
                    required
                  >
                    <option value="">
                      {repositories.isLoading ? 'Loading repositories…' : 'Choose a repository'}
                    </option>
                    {connected.map((connection) => (
                      <option key={connection.repo} value={connection.repo}>
                        {connection.repo}
                      </option>
                    ))}
                  </select>
                  {repositories.isError && (
                    <span className="field-note bad">
                      Could not load your connected repositories.
                    </span>
                  )}
                </label>
                {!creating && (
                  <label className="field grow">
                    <span>Issue</span>
                    <select
                      className="input"
                      aria-label="Issue"
                      value={number}
                      onChange={(e) => setNumber(e.target.value)}
                      disabled={!repo || issues.isLoading}
                      required
                    >
                      <option value="">
                        {!repo
                          ? 'Choose a repository first'
                          : issues.isLoading
                            ? 'Loading issues…'
                            : available.length
                              ? 'Choose an issue'
                              : 'No open issues'}
                      </option>
                      {available.map((issue) => (
                        <option
                          key={issue.number}
                          value={String(issue.number)}
                          disabled={Boolean(issue.listed_issue_id)}
                        >
                          #{issue.number} — {issue.title}
                          {issue.listed_issue_id ? ' (already on Misthos)' : ''}
                        </option>
                      ))}
                    </select>
                    {issues.isError && (
                      <span className="field-note bad">
                        Could not load this repository&apos;s issues.
                      </span>
                    )}
                    {chosen?.listed_issue_id && (
                      <span className="field-note">
                        This issue is already on Misthos as{' '}
                        <Link
                          to={`/issues/${chosen.listed_issue_id}`}
                          style={{ textDecoration: 'underline' }}
                        >
                          {chosen.listed_issue_id}
                        </Link>
                        .
                      </span>
                    )}
                  </label>
                )}
              </div>
              {!repositories.isLoading && connected.length === 0 && (
                <p className="field-note">
                  No repository is connected yet.{' '}
                  {(repositories.data?.settings_url ?? repositories.data?.install_url) && (
                    <a
                      href={repositories.data?.settings_url ?? repositories.data?.install_url ?? ''}
                      style={{ textDecoration: 'underline' }}
                    >
                      Connect one on GitHub
                    </a>
                  )}{' '}
                  and it will appear here.
                </p>
              )}
              <div className="btn-row">
                <button
                  className="btn"
                  type="button"
                  onClick={() => {
                    setCreating((was) => !was)
                    setNumber('')
                  }}
                  data-testid="toggle-new-issue"
                >
                  {creating ? 'Choose an existing issue' : 'Create new issue'}
                </button>
              </div>
              {creating && (
                <>
                  <label className="field">
                    <span>Title</span>
                    <input
                      className="input"
                      value={title}
                      onChange={(e) => setTitle(e.target.value)}
                      maxLength={300}
                      required
                    />
                  </label>
                  <label className="field">
                    <span>What is wrong, and what fixed looks like</span>
                    <textarea
                      className="input"
                      rows={4}
                      value={summary}
                      onChange={(e) => setSummary(e.target.value)}
                    />
                  </label>
                  <label className="field">
                    <span>Labels, comma separated</span>
                    <input
                      className="input"
                      value={labels}
                      onChange={(e) => setLabels(e.target.value)}
                      placeholder="bug, security"
                    />
                  </label>
                </>
              )}
              <label className="field">
                <span>What the work is worth, in USDC</span>
                <input
                  className="input"
                  value={price}
                  onChange={(e) => setPrice(e.target.value)}
                  inputMode="decimal"
                  pattern="[0-9]*[.,]?[0-9]*"
                  placeholder="250"
                  required
                />
                <span className={priced ? 'field-note' : 'field-note bad'}>
                  {priced
                    ? `You pay $${named.toFixed(2)}. The contributor receives $${contributorShare(named).toFixed(2)}, and Misthos keeps $${platformFee(named).toFixed(2)} when the work is accepted.`
                    : `Any amount from $${MINIMUM_PRICE}. You can change it on the issue's page until someone claims it.`}
                </span>
              </label>
              <label className="field">
                <span>Escrow term, in whole days</span>
                <input
                  className="input"
                  value={escrowTerm}
                  onChange={(e) => setEscrowTerm(e.target.value)}
                  inputMode="numeric"
                  pattern="[0-9]*"
                  placeholder="14"
                />
                <span className="field-note">
                  Between 3 and 90 days. Defaults to 14 days. How long the escrow holds funds before refund opens.
                </span>
              </label>
              <label className="check">
                <input
                  type="checkbox"
                  checked={compliance}
                  onChange={(e) => setCompliance(e.target.checked)}
                />{' '}
                Compliance-driven: a regulation or an audit needs this fixed
              </label>
              <div className="btn-row">
                <button
                  className="btn primary"
                  type="submit"
                  disabled={
                    publish.isPending ||
                    !repo ||
                    (creating ? !title.trim() : !linked) ||
                    !priced
                  }
                >
                  {publish.isPending ? 'Publishing…' : 'Publish at this price'}
                </button>
              </div>
              {publish.error && (
                <div className="error-box form-error">
                  {publish.error.message}
                  {listed && (
                    <>
                      {' '}
                      <Link to={`/issues/${listed}`} style={{ textDecoration: 'underline' }}>
                        Open {listed}
                      </Link>
                    </>
                  )}
                </div>
              )}
            </form>
          </Panel>
        </div>
      )}
    </>
  )
}
