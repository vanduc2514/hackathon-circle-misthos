import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Link, useSearchParams } from 'react-router-dom'
import { api, shortHash, unwrap, type Account } from '../lib/client'
import { signIn, signOut, useHealth, useMe } from '../lib/session'
import { browserProvider, browserWallet, demoWallet, type Signer } from '../lib/wallet'
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

            {!account && <ChooseSide onDone={changed} />}
            {account && !account.github_login && (
              <LinkGitHub account={account} simulated={simulated} onDone={changed} />
            )}
          </>
        )}
      </div>
    </>
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
            <span>Budget in USDC, which caps every price you are offered</span>
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
  simulated,
  onDone,
}: {
  account: Account
  simulated: boolean
  onDone: () => void
}) {
  const [login, setLogin] = useState('')

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
      <div className="btn-row">
        <button className="btn primary" onClick={() => start.mutate()} disabled={start.isPending}>
          Link with GitHub
        </button>
      </div>
      {start.error && <div className="error-box form-error">{start.error.message}</div>}
      {simulated && (
        <form
          className="form"
          style={{ marginTop: 18 }}
          onSubmit={(e) => {
            e.preventDefault()
            simulate.mutate()
          }}
        >
          <label className="field">
            <span>Or, in the simulation, any GitHub login</span>
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
            <button className="btn" type="submit" disabled={simulate.isPending || !login.trim()}>
              Link without GitHub
            </button>
          </div>
          {simulate.error && <div className="error-box form-error">{simulate.error.message}</div>}
        </form>
      )}
    </Panel>
  )
}
