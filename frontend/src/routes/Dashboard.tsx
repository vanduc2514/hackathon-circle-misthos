import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { api, money, relativeTime, unwrap, type DashboardOut } from '../lib/client'
import { Panel, Stat, StateBadge } from '../components/ui'
import { useHealth, useMe } from '../lib/session'

const PUBLISHER_ACTION_LABELS: Record<string, string> = {
  approve_criteria: 'Approve Criteria',
  commit_funds: 'Fund Escrow',
  review_and_merge: 'Review PR',
  contact_approver: 'Check Approver',
}

export default function Dashboard() {
  const health = useHealth()
  const me = useMe()

  const address = me.data?.address
  const account = me.data?.account
  const role = account?.role
  const partyId = account?.party_id

  const dashQuery = useQuery({
    queryKey: ['dashboard', partyId, role],
    queryFn: async (): Promise<DashboardOut> => unwrap(await api.GET('/api/v1/dashboard')),
    enabled: Boolean(address && role && partyId),
  })

  const dashData = dashQuery.data
  const publisher = dashData?.publisher
  const contributor = dashData?.contributor

  const pubActionNeeded = publisher?.action_needed ?? []
  const pubAllIssues = publisher?.all_issues ?? []

  const conActionNeeded = contributor?.action_needed ?? []
  const conAllWork = contributor?.all_work ?? []

  const pageTitle = !address
    ? 'Personal Treasury Overview'
    : !role
      ? 'Onboarding: Select Role'
      : role === 'publisher'
        ? `${publisher?.name || 'Publisher'} Dashboard`
        : `@${contributor?.handle || 'contributor'} Dashboard`

  const pageSub = !address
    ? 'Connect your wallet to inspect your scoped treasury commitments, active bounties, and pending actions.'
    : !role
      ? 'Finish setting up your account profile to access your personalized commitments and workflow.'
      : role === 'publisher'
        ? 'Monitor your allocated budget, committed in-flight escrow, released funds, and pending governance decisions.'
        : 'Track your claimed work, review outcomes, incoming payouts, and lifetime earnings.'

  return (
    <>
      <div className="banner">
        {health.data?.money_note || `Live on ${health.data?.network_label ?? 'chain'}.`} The
        escrow holds the money on chain, and the review agent and the decision log are its
        record. GitHub is connected through the App.
      </div>

      <div className="page-head">
        <div>
          <h1 className="page-title">{pageTitle}</h1>
          <p className="page-sub">{pageSub}</p>
        </div>
      </div>

      {!address && (
        <Panel title="Sign in to view your dashboard">
          <div className="empty" style={{ padding: '36px 20px', textAlign: 'center' }}>
            <p style={{ marginBottom: 20, fontSize: 15, color: 'var(--text-dim, #666)' }}>
              The treasury overview is personal to each participant. Sign in with your wallet to manage your issues,
              track in-flight escrow, or inspect work requiring your attention.
            </p>
            <Link
              to="/account"
              className="btn"
              style={{ display: 'inline-flex', alignItems: 'center', minHeight: 44, padding: '0 24px' }}
            >
              Connect Wallet / Sign In
            </Link>
          </div>
        </Panel>
      )}

      {address && !role && (
        <Panel title="Role onboarding required">
          <div className="empty" style={{ padding: '36px 20px', textAlign: 'center' }}>
            <p style={{ marginBottom: 20, fontSize: 15, color: 'var(--text-dim, #666)' }}>
              You have signed in with wallet <code>{address.slice(0, 8)}…{address.slice(-6)}</code>, but have not
              selected a role. Choose whether you are acting as a Publisher or Contributor to personalize your overview.
            </p>
            <Link
              to="/account"
              className="btn"
              style={{ display: 'inline-flex', alignItems: 'center', minHeight: 44, padding: '0 24px' }}
            >
              Choose Role on Account Page
            </Link>
          </div>
        </Panel>
      )}

      {address && role && dashQuery.isLoading && (
        <div className="empty" style={{ marginTop: 24, padding: '36px 20px', textAlign: 'center' }}>
          Loading your personalized dashboard…
        </div>
      )}

      {address && role === 'publisher' && publisher && (
        <>
          <div className="grid k3">
            <Stat
              label="Remaining budget"
              value={`$${money(publisher.budget_remaining_usdc)}`}
              hint="Available allocation for new issue bounties"
            />
            <Stat
              label="Committed in-flight"
              value={`$${money(publisher.committed_escrow_usdc)}`}
              hint="Escrow held in active funded issues"
            />
            <Stat
              label="Released to date"
              value={`$${money(publisher.released_usdc)}`}
              hint="Total settled payouts to contributors"
            />
          </div>

          {publisher.connected_repos && publisher.connected_repos.length > 0 && (
            <div style={{ marginBottom: 20 }}>
              <span style={{ fontSize: 13, color: 'var(--text-dim, #666)', marginRight: 8 }}>
                Connected Repositories:
              </span>
              {publisher.connected_repos.map((repo) => (
                <Link
                  key={repo}
                  to={`/issues?repo=${encodeURIComponent(repo)}`}
                  className="chip"
                  style={{ marginRight: 6, textDecoration: 'none' }}
                >
                  {repo}
                </Link>
              ))}
            </div>
          )}

          <Panel title={`Needs Your Action (${pubActionNeeded.length})`} flush>
            {pubActionNeeded.length === 0 ? (
              <div className="empty">No issues currently require your action. Everything is in flight or settled.</div>
            ) : (
              <div style={{ overflowX: 'auto' }}>
                <table className="table" style={{ width: '100%', minWidth: 640 }}>
                  <thead>
                    <tr>
                      <th>Issue</th>
                      <th>State</th>
                      <th>Next Action</th>
                      <th style={{ textAlign: 'right' }}>Price</th>
                      <th style={{ textAlign: 'right' }}>Action</th>
                    </tr>
                  </thead>
                  <tbody>
                    {pubActionNeeded.map((item) => (
                      <tr key={item.id}>
                        <td>
                          <Link to={`/issues/${item.id}`} className="row-link">
                            <div className="cell-title">{item.title}</div>
                            <div className="cell-repo">
                              {item.repo}#{item.github_issue_number}
                            </div>
                          </Link>
                        </td>
                        <td>
                          <StateBadge state={item.state} />
                        </td>
                        <td>
                          <span style={{ fontSize: 13, fontWeight: 500 }}>{item.next_action_label}</span>
                        </td>
                        <td className="mono-num" style={{ textAlign: 'right' }}>
                          ${money(item.price_usdc)}
                        </td>
                        <td style={{ textAlign: 'right' }}>
                          <Link
                            to={`/issues/${item.id}`}
                            className="btn btn-sm"
                            style={{ minHeight: 36, display: 'inline-flex', alignItems: 'center' }}
                          >
                            {PUBLISHER_ACTION_LABELS[item.next_action] ?? 'Proceed'}
                          </Link>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Panel>

          <Panel title={`All Tracked Issues (${pubAllIssues.length})`} flush>
            {pubAllIssues.length === 0 ? (
              <div className="empty" style={{ padding: '32px 16px', textAlign: 'center' }}>
                <p style={{ marginBottom: 16 }}>No issues published yet. Create or fund an issue from the Issues tab.</p>
                <Link to="/publish" className="btn btn-sm" style={{ display: 'inline-flex', alignItems: 'center', minHeight: 36 }}>
                  Publish an Issue
                </Link>
              </div>
            ) : (
              <div style={{ overflowX: 'auto' }}>
                <table className="table" style={{ width: '100%', minWidth: 640 }}>
                  <thead>
                    <tr>
                      <th>Issue</th>
                      <th>State</th>
                      <th>Current Status / Next Step</th>
                      <th style={{ textAlign: 'right' }}>Price</th>
                      <th style={{ textAlign: 'right' }}>Updated</th>
                    </tr>
                  </thead>
                  <tbody>
                    {pubAllIssues.map((item) => (
                      <tr key={item.id}>
                        <td>
                          <Link to={`/issues/${item.id}`} className="row-link">
                            <div className="cell-title">{item.title}</div>
                            <div className="cell-repo">
                              {item.repo}#{item.github_issue_number}
                            </div>
                          </Link>
                        </td>
                        <td>
                          <StateBadge state={item.state} />
                        </td>
                        <td>
                          <span style={{ fontSize: 13, color: item.action_needed ? 'var(--accent, #0066cc)' : 'inherit' }}>
                            {item.next_action_label}
                          </span>
                        </td>
                        <td className="mono-num" style={{ textAlign: 'right' }}>
                          ${money(item.price_usdc)}
                        </td>
                        <td className="stat-hint" style={{ textAlign: 'right' }}>
                          {relativeTime(item.updated_at)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Panel>
        </>
      )}

      {address && role === 'contributor' && contributor && (
        <>
          {!contributor.payout_wallet_set ? (
            <div
              className="panel"
              style={{
                borderLeft: '4px solid var(--amber, #d97706)',
                marginBottom: 20,
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'space-between',
                gap: 12,
                flexWrap: 'wrap',
              }}
            >
              <div>
                <strong>Payout Destination Missing:</strong> You haven't set a payout wallet address yet.{' '}
                <Link to="/account" style={{ textDecoration: 'underline', fontWeight: 600 }}>
                  Configure your payout wallet on the Account page
                </Link>{' '}
                so settled earnings can reach you.
              </div>
              <Link to="/account" className="btn btn-sm" style={{ minHeight: 36, display: 'inline-flex', alignItems: 'center' }}>
                Set up wallet
              </Link>
            </div>
          ) : (
            contributor.payout_address && (
              <div style={{ marginBottom: 16, fontSize: 13, color: '#6b7280' }}>
                Payout address: <code style={{ fontSize: 12 }}>{contributor.payout_address}</code>
              </div>
            )
          )}

          <div className="grid k4">
            <Stat
              label="In-flight commitments"
              value={`$${money(contributor.owed_in_flight_usdc)}`}
              hint="Escrow secured for claimed or submitted work"
            />
            <Stat
              label="About to release"
              value={`$${money(contributor.about_to_release_usdc)}`}
              hint="Accepted pull requests awaiting publisher merge"
            />
            <Stat
              label="Paid this year"
              value={`$${money(contributor.paid_this_year_usdc)}`}
              hint={`${contributor.settled_count} settled bounty payouts`}
            />
            <Stat
              label="Total earned"
              value={`$${money(contributor.total_earned_usdc)}`}
              hint={`Reputation: ${contributor.reputation}`}
            />
          </div>

          <Panel title={`Needs Your Action (${conActionNeeded.length})`} flush>
            {conActionNeeded.length === 0 ? (
              <div className="empty">No tasks currently require your attention. Check the Issues tab for new bounties!</div>
            ) : (
              <div style={{ overflowX: 'auto' }}>
                <table className="table" style={{ width: '100%', minWidth: 640 }}>
                  <thead>
                    <tr>
                      <th>Work Item</th>
                      <th>State</th>
                      <th>Required Action</th>
                      <th style={{ textAlign: 'right' }}>Bounty</th>
                      <th style={{ textAlign: 'right' }}>Action</th>
                    </tr>
                  </thead>
                  <tbody>
                    {conActionNeeded.map((item) => (
                      <tr key={item.id}>
                        <td>
                          <Link to={`/issues/${item.id}`} className="row-link">
                            <div className="cell-title">{item.title}</div>
                            <div className="cell-repo">
                              {item.repo}#{item.github_issue_number}
                            </div>
                          </Link>
                        </td>
                        <td>
                          <StateBadge state={item.state} />
                        </td>
                        <td>
                          <span style={{ fontSize: 13, fontWeight: 500, color: 'var(--accent, #0066cc)' }}>
                            {item.next_action_label}
                          </span>
                        </td>
                        <td className="mono-num" style={{ textAlign: 'right' }}>
                          ${money(item.price_usdc)}
                        </td>
                        <td style={{ textAlign: 'right' }}>
                          <Link
                            to={`/issues/${item.id}`}
                            className="btn btn-sm"
                            style={{ minHeight: 36, display: 'inline-flex', alignItems: 'center' }}
                          >
                            {item.next_action === 'open_pr' ? 'Submit PR' : 'View findings'}
                          </Link>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Panel>

          <Panel title={`All Your Bounties (${conAllWork.length})`} flush>
            {conAllWork.length === 0 ? (
              <div className="empty" style={{ padding: '32px 16px', textAlign: 'center' }}>
                <p style={{ marginBottom: 16 }}>You haven't claimed any bounties yet. Browse open issues to get started.</p>
                <Link to="/issues" className="btn btn-sm" style={{ display: 'inline-flex', alignItems: 'center', minHeight: 36 }}>
                  Browse Available Issues
                </Link>
              </div>
            ) : (
              <div style={{ overflowX: 'auto' }}>
                <table className="table" style={{ width: '100%', minWidth: 640 }}>
                  <thead>
                    <tr>
                      <th>Work Item</th>
                      <th>State</th>
                      <th>Status / Next Step</th>
                      <th style={{ textAlign: 'right' }}>Bounty</th>
                      <th style={{ textAlign: 'right' }}>Updated</th>
                    </tr>
                  </thead>
                  <tbody>
                    {conAllWork.map((item) => (
                      <tr key={item.id}>
                        <td>
                          <Link to={`/issues/${item.id}`} className="row-link">
                            <div className="cell-title">{item.title}</div>
                            <div className="cell-repo">
                              {item.repo}#{item.github_issue_number}
                              {item.pull_request_number && ` · PR #${item.pull_request_number}`}
                            </div>
                          </Link>
                        </td>
                        <td>
                          <StateBadge state={item.state} />
                        </td>
                        <td>
                          <span style={{ fontSize: 13 }}>{item.next_action_label}</span>
                        </td>
                        <td className="mono-num" style={{ textAlign: 'right' }}>
                          ${money(item.price_usdc)}
                        </td>
                        <td className="stat-hint" style={{ textAlign: 'right' }}>
                          {relativeTime(item.updated_at)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Panel>
        </>
      )}
    </>
  )
}
