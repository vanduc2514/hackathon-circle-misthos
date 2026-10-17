import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useSearchParams } from 'react-router-dom'
import { api, shortHash, unwrap, type Account, type HealthOut } from '../lib/client'
import { signIn, signOut, useHealth, useMe } from '../lib/session'
import { browserProvider, browserWallet, demoWallet, type Signer } from '../lib/wallet'
import { linking } from '../lib/github-link'
import { Panel } from '../components/ui'

/**
 * Signing in, in the order the API asks for it: prove a wallet, take a side once,
 * link a GitHub account. Each step appears only once the one before it is done.
 */
export default function AccountPage() {
  const health = useHealth()
  const me = useMe()
  const [params] = useSearchParams()
  const qc = useQueryClient()
  // Budgets, spend and every action depend on who is asking.
  const changed = () => qc.invalidateQueries()

  const simulated = health.data?.simulated ?? false
  const account = me.data?.account ?? null

  const enter = useMutation({
    mutationFn: async (pick: () => Promise<Signer> | Signer) => signIn(await pick()),
    onSuccess: changed,
  })
  const leave = useMutation({ mutationFn: signOut, onSuccess: changed })

  return (
    <>
      <div className="page-head">
        <div>
          <h1 className="page-title">Account</h1>
          <p className="page-sub">
            Sign in with a wallet, choose whether you publish work or do it, and link your
            GitHub account, which is how your issues and pull requests are matched to you.
          </p>
        </div>
      </div>

      {params.get('linked') === 'github' && account?.github_login && (
        <div className="banner">Your GitHub account {account.github_login} is linked.</div>
      )}

      <div className="form-stack">
        {me.isLoading ? (
          <div className="empty">Loading…</div>
        ) : !me.data ? (
          <Panel title="Sign in">
            <p className="dim" style={{ marginBottom: 14 }}>
              Your wallet signs a message naming this site and a one-time code. Signing costs
              nothing and moves no money.
            </p>
            <div className="btn-row">
              <button
                className="btn primary"
                disabled={!browserProvider() || enter.isPending}
                onClick={() => enter.mutate(browserWallet)}
              >
                Sign in with your wallet
              </button>
              {simulated && (
                <>
                  <button
                    className="btn"
                    disabled={enter.isPending}
                    onClick={() => enter.mutate(() => demoWallet('publisher'))}
                  >
                    Demo publisher wallet
                  </button>
                  <button
                    className="btn"
                    disabled={enter.isPending}
                    onClick={() => enter.mutate(() => demoWallet('contributor'))}
                  >
                    Demo contributor wallet
                  </button>
                </>
              )}
            </div>
            {!browserProvider() && (
              <p className="stat-hint" style={{ marginTop: 10 }}>
                No wallet extension was found in this browser.
                {simulated && ' The demo wallets are keys kept in this browser, for the simulation only.'}
              </p>
            )}
            {enter.error && <div className="error-box form-error">{enter.error.message}</div>}
          </Panel>
        ) : (
          <>
            <Panel title="Signed in">
              <dl className="kv">
                <dt>Wallet</dt>
                <dd className="muted" title={me.data.address}>
                  {shortHash(me.data.address)}
                </dd>
                {account && (
                  <>
                    <dt>Side</dt>
                    <dd>{account.role}</dd>
                    <dt>Acting as</dt>
                    <dd>{account.party_id}</dd>
                    <dt>GitHub</dt>
                    <dd>{account.github_login ?? <span className="muted">not linked</span>}</dd>
                  </>
                )}
              </dl>
              <div className="btn-row" style={{ marginTop: 14 }}>
                {account?.role === 'publisher' && account.github_login && (
                  <>
                    <Link className="btn primary" to="/publish">
                      Publish an issue
                    </Link>
                    <Link className="btn" to={`/spend?publisher=${account.party_id}`}>
                      Your spend
                    </Link>
                  </>
                )}
                {account?.role === 'contributor' && account.github_login && (
                  <Link className="btn primary" to="/issues?state=FUNDED">
                    Find funded issues
                  </Link>
                )}
                <button className="btn" onClick={() => leave.mutate()} disabled={leave.isPending}>
                  Sign out
                </button>
              </div>
            </Panel>

            {/* Rendered for any publisher, but the install link inside waits for a
                linked login: a connection is attributed to the publisher whose *linked*
                login installed it, so linking comes first and this panel says so. */}
            {account?.role === 'publisher' && (
              <Repositories publisherId={account.party_id} linked={Boolean(account.github_login)} />
            )}
            {!account && <ChooseSide onDone={changed} />}
            {account && !account.github_login && health.data && (
              <LinkGitHub account={account} health={health.data} onDone={changed} />
            )}
          </>
        )}
      </div>
    </>
  )
}

/**
 * The repositories a publisher installed the App on, and where to install it.
 *
 * Shared by the panel below and by the linking panel, which offers installing as the
 * way to link: one query key and one fetcher, so React Query makes one request.
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
            : 'Nothing connected yet. Link your GitHub account above, then install the Misthos App on a repository — the repositories it covers appear here.'}
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

function ChooseSide({ onDone }: { onDone: () => void }) {
  const [role, setRole] = useState<'publisher' | 'contributor'>('publisher')
  const [name, setName] = useState('')
  const [budget, setBudget] = useState('5000')

  const choose = useMutation({
    mutationFn: async () =>
      unwrap(
        await api.POST('/api/v1/auth/role', {
          body: { role, name: name.trim(), budget_usdc: budget },
        }),
      ),
    onSuccess: onDone,
  })

  return (
    <Panel title="Choose your side">
      <p className="dim" style={{ marginBottom: 14 }}>
        A wallet publishes work or does it, not both, and the choice is made once.
      </p>
      <form
        className="form"
        onSubmit={(e) => {
          e.preventDefault()
          choose.mutate()
        }}
      >
        <div className="radio-row">
          <label>
            <input
              type="radio"
              checked={role === 'publisher'}
              onChange={() => setRole('publisher')}
            />{' '}
            Publisher: I fund fixes
          </label>
          <label>
            <input
              type="radio"
              checked={role === 'contributor'}
              onChange={() => setRole('contributor')}
            />{' '}
            Contributor: I fix issues
          </label>
        </div>
        <label className="field">
          <span>{role === 'publisher' ? 'Organisation name' : 'Your name'}</span>
          <input
            className="input"
            value={name}
            onChange={(e) => setName(e.target.value)}
            maxLength={200}
            required
          />
        </label>
        {role === 'publisher' && (
          <label className="field">
            <span>Budget in USDC, for your own reporting</span>
            <input
              className="input"
              value={budget}
              onChange={(e) => setBudget(e.target.value)}
              inputMode="decimal"
              pattern="[0-9]+(\.[0-9]{1,6})?"
              required
            />
          </label>
        )}
        <div className="btn-row">
          <button className="btn primary" type="submit" disabled={choose.isPending || !name.trim()}>
            {choose.isPending ? 'Working…' : `Continue as a ${role}`}
          </button>
        </div>
        {choose.error && <div className="error-box form-error">{choose.error.message}</div>}
      </form>
    </Panel>
  )
}

function LinkGitHub({
  account,
  health,
  onDone,
}: {
  account: Account
  health: HealthOut
  onDone: () => void
}) {
  const [login, setLogin] = useState('')
  const way = linking(health)

  const start = useMutation({
    mutationFn: async () => unwrap(await api.POST('/api/v1/auth/github/start')),
    // GitHub asks the user, then sends them back to /account?linked=github.
    onSuccess: (data) => window.location.assign(data.authorize_url),
  })
  const simulate = useMutation({
    mutationFn: async () =>
      unwrap(await api.POST('/api/v1/auth/github/simulate', { body: { login: login.trim() } })),
    onSuccess: onDone,
  })

  return (
    <Panel title="Link your GitHub account">
      <p className="dim" style={{ marginBottom: 14 }}>
        {account.role === 'publisher'
          ? 'Publishing needs a GitHub account: it is how an issue is tied to you.'
          : 'Claiming needs a GitHub account: only pull requests you open count as your work.'}{' '}
        We keep your login and nothing else.
      </p>
      {way.oauth && (
        <div className="btn-row">
          <button className="btn primary" onClick={() => start.mutate()} disabled={start.isPending}>
            Link with GitHub
          </button>
        </div>
      )}
      {start.error && <div className="error-box form-error">{start.error.message}</div>}
      {way.notSetUp && (
        <div className={way.notSetUp === 'deployment' ? 'error-box' : 'banner'} role="note">
          GitHub linking isn't set up on this server
          {way.notSetUp === 'simulation'
            ? ', so in the simulation the login you type below is taken as yours.'
            : ', so no GitHub account can be linked here yet, and publishing and claiming wait on it.'}{' '}
          {way.notSetUp === 'simulation' ? 'To link your real account' : 'To fix it'}, whoever runs the
          server creates a GitHub OAuth App with the callback{' '}
          <code>{health.github_oauth_callback_url}</code>, sets{' '}
          <code>MISTHOS_GITHUB_OAUTH_CLIENT_ID</code> and{' '}
          <code>MISTHOS_GITHUB_OAUTH_CLIENT_SECRET</code>, and restarts the API.
        </div>
      )}
      {way.byLogin && (
        <form
          className="form"
          style={{ marginTop: way.byLogin === 'other' ? 18 : 14 }}
          onSubmit={(e) => {
            e.preventDefault()
            simulate.mutate()
          }}
        >
          <label className="field">
            <span>
              {way.byLogin === 'main'
                ? 'Your GitHub login'
                : 'Or, in the simulation, any GitHub login'}
            </span>
            <input
              className="input"
              value={login}
              onChange={(e) => setLogin(e.target.value)}
              pattern="[A-Za-z0-9\-]+"
              maxLength={39}
              placeholder="octocat"
              required
            />
          </label>
          <div className="btn-row">
            <button
              className={way.byLogin === 'main' ? 'btn primary' : 'btn'}
              type="submit"
              disabled={simulate.isPending || !login.trim()}
            >
              Link this login
            </button>
          </div>
          {simulate.error && <div className="error-box form-error">{simulate.error.message}</div>}
        </form>
      )}
    </Panel>
  )
}
