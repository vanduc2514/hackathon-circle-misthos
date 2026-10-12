import { useRef, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate } from 'react-router-dom'
import { ApiError, api, unwrap } from '../lib/client'
import { MINIMUM_PRICE, contributorShare, parseAmount, platformFee } from '../lib/fee'
import { parseRepoLink } from '../lib/repo-link'
import { useHealth, useMe } from '../lib/session'
import { Panel } from '../components/ui'

/**
 * A publisher names a GitHub issue and the price they will pay for it, and gets its
 * acceptance criteria drafted. Nothing is committed here: the publisher approves the
 * criteria and then the price on the issue's page, and only that second approval moves
 * money. The price is theirs to set, and the engine's reading of the issue is offered
 * beside it as advice (#142).
 */
export default function Publish() {
  const me = useMe()
  const health = useHealth()
  const navigate = useNavigate()
  const qc = useQueryClient()

  const [repo, setRepo] = useState('')
  const [number, setNumber] = useState('')
  const [title, setTitle] = useState('')
  const [summary, setSummary] = useState('')
  const [labels, setLabels] = useState('')
  const [price, setPrice] = useState('')
  const [compliance, setCompliance] = useState(false)

  // What the publisher typed or pasted, read once: the request carries the
  // `owner/name` the API stores, and the field can say what it understood first.
  const link = parseRepoLink(repo)
  // A pasted issue link carries its number, so it stands in when the field is empty.
  const issueNumber = number.trim() || link.number || ''
  // The price is the publisher's, so the form is what makes it usable: a number, and
  // at least the platform's minimum.
  const named = parseAmount(price)
  const priced = !Number.isNaN(named) && named >= MINIMUM_PRICE

  const account = me.data?.account ?? null
  const ready = account?.role === 'publisher' && Boolean(account.github_login)
  // Set the moment the form is sent. The button is disabled by isPending, but that
  // reaches it a render later, and a double click inside that window published the
  // same GitHub issue twice (#120).
  const sending = useRef(false)

  const publish = useMutation({
    mutationFn: async () =>
      unwrap(
        await api.POST('/api/v1/issues', {
          body: {
            repo: link.repo,
            number: issueNumber ? Number(issueNumber) : null,
            title: title.trim(),
            summary: summary.trim(),
            labels: labels
              .split(',')
              .map((l) => l.trim())
              .filter(Boolean),
            // The publisher's own number, and the amount the escrow commits (#142).
            price: price.trim(),
            // The API publishes as the signed-in publisher whatever this says.
            publisher_id: account?.party_id ?? '',
            compliance_driven: compliance,
          },
        }),
      ),
    onSuccess: (issue) => {
      qc.invalidateQueries({ queryKey: ['issues'] })
      qc.invalidateQueries({ queryKey: ['metrics'] })
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
            Name the GitHub issue you want fixed and the price you will pay for it. The pricing
            engine reads the issue and shows you what work of this shape has settled for, which
            is advice and nothing more: the number you type is the number that binds. You approve
            the acceptance criteria, then the price, and only then are funds committed.
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
          <Link to="/account" style={{ textDecoration: 'underline' }}>
            Go to your account
          </Link>
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
                  <input
                    className="input"
                    value={repo}
                    onChange={(e) => {
                      const next = e.target.value
                      setRepo(next)
                      // A pasted issue link carries its number. Fill the field in
                      // rather than make the publisher read it off the URL.
                      const found = parseRepoLink(next)
                      if (found.number && !number) setNumber(found.number)
                    }}
                    placeholder="owner/name, or paste a GitHub link"
                    required
                  />
                  {repo.trim() !== '' && (
                    <span className={link.repo ? 'field-note' : 'field-note bad'}>
                      {link.repo
                        ? `Read as ${link.repo}${link.number ? ` issue ${link.number}` : ''}`
                        : 'That names no GitHub repository'}
                    </span>
                  )}
                </label>
                <label className="field">
                  <span>Issue number</span>
                  <input
                    className="input"
                    value={number}
                    onChange={(e) => setNumber(e.target.value)}
                    inputMode="numeric"
                    pattern="[0-9]*"
                    placeholder={health.data?.simulated ? 'optional' : 'empty opens a new issue'}
                  />
                </label>
              </div>
              {!health.data?.simulated && !issueNumber && (
                <p className="field-note">
                  Leaving the number empty opens the issue on GitHub with this title and
                  description, and prices the issue it creates.
                </p>
              )}
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
                  disabled={publish.isPending || !link.repo || !title.trim() || !priced}
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
