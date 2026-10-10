import { useRef, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate } from 'react-router-dom'
import { ApiError, api, unwrap } from '../lib/client'
import { useHealth, useMe } from '../lib/session'
import { Panel } from '../components/ui'

/**
 * A publisher names a GitHub issue and gets a price back. Nothing is committed here:
 * the publisher approves the acceptance criteria and then the price on the issue's
 * page, and only that second approval moves money.
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
  const [compliance, setCompliance] = useState(false)

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
            repo: repo.trim(),
            number: number ? Number(number) : null,
            title: title.trim(),
            summary: summary.trim(),
            labels: labels
              .split(',')
              .map((l) => l.trim())
              .filter(Boolean),
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
            Name the GitHub issue you want fixed. The pricing engine reads it and proposes a
            fixed price with its reasons. You approve the acceptance criteria, then the
            price, and only then are funds committed.
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
                    onChange={(e) => setRepo(e.target.value)}
                    placeholder="owner/name"
                    pattern="[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+"
                    required
                  />
                </label>
                <label className="field">
                  <span>Issue number</span>
                  <input
                    className="input"
                    value={number}
                    onChange={(e) => setNumber(e.target.value)}
                    inputMode="numeric"
                    pattern="[0-9]*"
                    placeholder={health.data?.simulated ? 'optional' : '123'}
                    required={!health.data?.simulated}
                  />
                </label>
              </div>
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
                  disabled={publish.isPending || !repo.trim() || !title.trim()}
                >
                  {publish.isPending ? 'Pricing…' : 'Publish and get a price'}
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
