import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { api, shortHash, unwrap } from '../lib/client'
import { signOut, useMe } from '../lib/session'
import { gateStep, missing } from '../lib/gate'
import { useGate } from '../components/SignInGate'
import { Panel } from '../components/ui'

/**
 * The profile: who you are on Misthos, the wallet the money moves through, and the
 * repositories the App is on.
 *
 * Signing in is the sign-in gate's alone (#181), so nothing here asks for a wallet, a
 * login or a side. Until the gate is done the page says what is missing, in the gate's
 * own words, and opens the gate to finish it.
 */
export default function AccountPage() {
  const me = useMe()
  const gate = useGate()
  const qc = useQueryClient()
  const leave = useMutation({ mutationFn: signOut, onSuccess: () => qc.invalidateQueries() })
  const account = me.data?.account ?? null
  const unfinished = gateStep(me.data) !== 'done'

  return (
    <>
      <div className="page-head">
        <div>
          <h1 className="page-title">Profile</h1>
          <p className="page-sub">
            Who you are on Misthos, the wallet the money moves through, and, for a publisher,
            the repositories the Misthos App is on.
          </p>
        </div>
      </div>

      <div className="form-stack">
        {me.isLoading ? (
          <div className="empty">Loading…</div>
        ) : unfinished ? (
          <Panel title={me.data ? 'Finish signing in' : 'Sign in'}>
            <p className="dim" style={{ marginBottom: 14 }}>
              {missing(me.data)} Signing in takes a wallet, a GitHub account and a side, in
              that order, and whatever you have done already is kept.
            </p>
            <div className="btn-row">
              <button
                type="button"
                className="btn primary"
                aria-haspopup="dialog"
                onClick={gate.open}
              >
                {me.data ? 'Finish signing in' : 'Sign in'}
              </button>
              {me.data && (
                <button className="btn" onClick={() => leave.mutate()} disabled={leave.isPending}>
                  Sign out
                </button>
              )}
            </div>
          </Panel>
        ) : (
          me.data &&
          account && (
            <Panel title="Signed in">
              <dl className="kv">
                <dt>Wallet</dt>
                <dd className="muted" title={me.data.address}>
                  {shortHash(me.data.address)}
                </dd>
                <dt>Side</dt>
                <dd>{account.role}</dd>
                <dt>Acting as</dt>
                <dd>{account.party_id}</dd>
                <dt>GitHub</dt>
                <dd>{account.github_login}</dd>
              </dl>
              <p className="stat-hint" style={{ marginTop: 10 }}>
                {account.role === 'contributor'
                  ? 'Your payouts are released to the wallet you signed in with.'
                  : 'You fund issues from the wallet you signed in with.'}
              </p>
              <div className="btn-row" style={{ marginTop: 14 }}>
                {account.role === 'publisher' && (
                  <>
                    <Link className="btn primary" to="/publish">
                      Publish an issue
                    </Link>
                  </>
                )}
                {account.role === 'contributor' && (
                  <Link className="btn primary" to="/issues?state=FUNDED">
                    Find funded issues
                  </Link>
                )}
                <button className="btn" onClick={() => leave.mutate()} disabled={leave.isPending}>
                  Sign out
                </button>
              </div>
            </Panel>
          )
        )}

        {/* Rendered for any publisher, but the install link inside waits for a linked
            login: a connection is attributed to the publisher whose *linked* login
            installed it, which is why the gate links GitHub first. An account made
            before the gate may still be waiting on that link. */}
        {account?.role === 'publisher' && (
          <Repositories publisherId={account.party_id} linked={Boolean(account.github_login)} />
        )}
      </div>
    </>
  )
}

/**
 * The repositories a publisher installed the App on, and where to install it.
 *
 * One query key and one fetcher, so React Query makes one request for it.
 */
const repositoriesQuery = (publisherId: string) => ({
  queryKey: ['repositories', publisherId],
  queryFn: async () =>
    unwrap(
      await api.GET('/api/v1/publishers/{publisher_id}/repositories', {
        params: { path: { publisher_id: publisherId } },
      }),
    ),
})

/**
 * Repositories the publisher installed the GitHub App on (#6). There, an issue is
 * priced by a label and funded by a comment, without coming back here.
 */
function Repositories({ publisherId, linked }: { publisherId: string; linked: boolean }) {
  const { data } = useQuery(repositoriesQuery(publisherId))
  if (!data) return null
  return (
    <Panel title="Your repositories">
      {data.repositories.length ? (
        <ul className="findings" style={{ marginBottom: 12 }}>
          {data.repositories.map((r) => (
            <li key={r.repo}>
              <a href={`https://github.com/${r.repo}`} target="_blank" rel="noreferrer">
                {r.repo}
              </a>
            </li>
          ))}
        </ul>
      ) : (
        <p className="dim" style={{ marginBottom: 12 }}>
          {linked
            ? 'No repositories yet. Install the Misthos GitHub App on one, signed in to GitHub as the account you linked.'
            : 'Nothing connected yet. Finish signing in to link your GitHub account, then install the Misthos App on a repository — the repositories it covers appear here.'}
        </p>
      )}
      <p className="stat-hint">
        On a connected repository, add the <code>{data.label}</code> label to an issue to have
        it priced, then comment <code>/misthos approve</code> on it to commit the funds.
        Contributors claim it with <code>/misthos claim</code>.
      </p>
      {linked && (data.settings_url || data.install_url) && (
        // Offered here once GitHub is linked, which is the order attribution needs. Once
        // a repository is connected the App is installed and GitHub's page for that
        // installation is the only place to add or remove repositories, so that is what
        // the button becomes rather than offering an install the App is already on.
        <div className="btn-row" style={{ marginTop: 12 }}>
          {data.settings_url ? (
            <a className="btn" href={data.settings_url}>
              Open {data.app_name ?? 'App'} settings
            </a>
          ) : (
            <a className="btn" href={data.install_url ?? '#'}>
              Install the GitHub App
            </a>
          )}
        </div>
      )}
    </Panel>
  )
}
