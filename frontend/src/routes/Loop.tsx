import { useQuery } from '@tanstack/react-query'
import { Link, useSearchParams } from 'react-router-dom'
import { api, money, relativeTime, type LoopOut } from '../lib/client'
import { Panel, Stat } from '../components/ui'
import { valueMovedStat } from '../lib/value-moved'

/**
 * The loop in public: what was funded, settled and paid, for one repository or all
 * of them. A payout seen by a repository's watchers persuades more than a homepage.
 * The API serves no wallet or transfer here, only what the settlement comment shows.
 */
export default function Loop() {
  const [params] = useSearchParams()
  const repo = params.get('repo') ?? undefined

  const loop = useQuery({
    queryKey: ['loop', repo],
    queryFn: async () =>
      (await api.GET('/api/v1/loop', { params: { query: repo ? { repo } : {} } })).data as
        | LoopOut
        | undefined,
  })

  const l = loop.data

  return (
    <>
      <div className="page-head">
        <div>
          <h1 className="page-title">{repo ? `The loop in ${repo}` : 'The loop'}</h1>
          <p className="page-sub">
            Funded in public, fixed in public, paid in public. Every number here comes from
            the payment ledger.
          </p>
        </div>
        {repo && (
          <div className="btn-row">
            <Link className="btn" to="/loop">
              Every repository
            </Link>
          </div>
        )}
      </div>

      <div className="grid k4">
        <Stat label="Settled this week" value={l?.settled_issues_7d ?? '—'} />
        <Stat label="Settled in all" value={l?.settled_issues ?? '—'} />
        <Stat label="Paid to contributors" {...valueMovedStat(l?.value_moved)} />
        <Stat label="Funded and open" value={l?.funded_open ?? '—'} hint="Waiting for a fix" />
      </div>

      <Panel title="Recent settlements" flush>
        {l?.recent.length ? (
          <table className="table">
            <thead>
              <tr>
                <th>Issue</th>
                <th>Paid to</th>
                <th style={{ textAlign: 'right' }}>Amount</th>
                <th style={{ textAlign: 'right' }}>When</th>
              </tr>
            </thead>
            <tbody>
              {l.recent.map((s) => (
                <tr key={s.issue_id}>
                  <td>
                    <a href={s.github_url} target="_blank" rel="noreferrer" className="row-link">
                      <div className="cell-title">{s.title}</div>
                      <div className="cell-repo">
                        {s.repo}#{s.number}
                      </div>
                    </a>
                  </td>
                  <td>
                    <Link to={`/loop?repo=${encodeURIComponent(s.repo)}`} className="dim">
                      @{s.contributor}
                    </Link>
                  </td>
                  <td className="mono-num" style={{ textAlign: 'right' }}>
                    ${money(s.amount)}
                  </td>
                  <td className="dim" style={{ textAlign: 'right' }}>
                    {relativeTime(s.settled_at)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <div className="empty">{loop.isLoading ? 'Loading…' : 'Nothing has settled here yet.'}</div>
        )}
      </Panel>
    </>
  )
}
