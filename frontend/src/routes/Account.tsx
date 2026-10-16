import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useSearchParams } from 'react-router-dom'
import { api, shortHash, unwrap, type Account } from '../lib/client'
import { signOut, useMe } from '../lib/session'
import { setUpWallet } from '../lib/circle-wallet'
import { Panel } from '../components/ui'
import { SignInGate } from '../components/SignInGate'

/**
 * Account / Profile page (#181):
 * Shows user identity (connected wallet, linked GitHub, role, party ID).
 * For publishers, offers installing or managing the Misthos GitHub App.
 */
export default function AccountPage() {
  const me = useMe()
  const [params] = useSearchParams()
  const qc = useQueryClient()
  const changed = () => qc.invalidateQueries()

  const [gateOpen, setGateOpen] = useState(false)
  const account = me.data?.account ?? null

  const leave = useMutation({ mutationFn: signOut, onSuccess: changed })

  return (
    <>
      <SignInGate isOpen={gateOpen || (!me.isLoading && !account)} onClose={() => setGateOpen(false)} />
      <div className="page-head">
        <div>
          <h1 className="page-title">Account</h1>
          <p className="page-sub">
            Your Misthos identity: your connected wallet, your linked GitHub account, and your role.
          </p>
        </div>
      </div>

      {params.get('linked') === 'github' && account?.github_login && (
        <div className="banner">Your GitHub account {account.github_login} is linked.</div>
      )}

      <div className="form-stack">
        {me.isLoading ? (
          <div className="empty">Loading…</div>
        ) : !account ? (
          <Panel title="Sign in required">
            <p className="dim" style={{ marginBottom: 14 }}>
              Misthos requires completing the sign-in gate before accessing the app: prove a wallet,
              link your GitHub account, and choose your side.
            </p>
            <div className="btn-row">
              <button className="btn primary" onClick={() => setGateOpen(true)}>
                Finish signing in
              </button>
            </div>
          </Panel>
        ) : (
          <>
            <Panel title="Signed in">
              <dl className="kv">
                <dt>Wallet</dt>
                <dd className="muted" title={me.data?.address}>
                  {shortHash(me.data?.address ?? '')}
                </dd>
                <dt>Side</dt>
                <dd>{account.role}</dd>
                <dt>Acting as</dt>
                <dd>{account.party_id}</dd>
                <dt>GitHub</dt>
                <dd>{account.github_login ?? <span className="muted">not linked</span>}</dd>
              </dl>
              <div className="btn-row" style={{ marginTop: 14 }}>
                {account.role === 'publisher' && account.github_login && (
                  <>
                    <Link className="btn primary" to="/publish">
                      Publish an issue
                    </Link>
                    <Link className="btn" to={`/spend?publisher=${account.party_id}`}>
                      Your spend
                    </Link>
                  </>
                )}
                {account.role === 'contributor' && account.github_login && (
                  <Link className="btn primary" to="/issues?state=FUNDED">
                    Find funded issues
                  </Link>
                )}
                <button className="btn" onClick={() => leave.mutate()} disabled={leave.isPending}>
                  Sign out
                </button>
              </div>
            </Panel>

            {account.role === 'publisher' && (
              <Repositories publisherId={account.party_id} linked={Boolean(account.github_login)} />
            )}
            <PayoutWallet account={account} />
          </>
        )}
      </div>
    </>
  )
}

const repositoriesQuery = (publisherId: string) => ({
  queryKey: ['repositories', publisherId],
  queryFn: async () =>
    unwrap(
      await api.GET('/api/v1/publishers/{publisher_id}/repositories', {
        params: { path: { publisher_id: publisherId } },
      }),
    ),
})

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

function PayoutWallet({ account }: { account: Account }) {
  const qc = useQueryClient()
  const party = account.role as 'publisher' | 'contributor'
  const path = { params: { path: { party, party_id: account.party_id } } }
  const setUp = useMutation({
    mutationFn: () =>
      setUpWallet(
        async () => unwrap(await api.POST('/api/v1/wallets/{party}/{party_id}/session', path)),
        async () => unwrap(await api.POST('/api/v1/wallets/{party}/{party_id}/link', path)),
      ),
    onSuccess: () => {
      for (const key of ['me', 'contributors', 'publishers']) {
        qc.invalidateQueries({ queryKey: [key] })
      }
    },
  })

  return (
    <Panel title="Your Circle wallet">
      <p className="dim" style={{ marginBottom: 12 }}>
        {party === 'contributor'
          ? 'Your payouts are released to this wallet.'
          : 'The wallet you fund issues from.'}{' '}
        It is yours: you set a PIN in Circle's own window, and neither Misthos nor Circle can
        move the money in it without you.
      </p>
      <div className="btn-row">
        <button className="btn primary" onClick={() => setUp.mutate()} disabled={setUp.isPending}>
          {setUp.isPending ? 'Waiting for Circle…' : 'Set up or reconnect your wallet'}
        </button>
      </div>
      {setUp.data && (
        <p className="stat-hint" style={{ marginTop: 10 }}>
          {setUp.data === 'created'
            ? 'Your wallet was created and linked.'
            : 'Your wallet is linked.'}
        </p>
      )}
      {setUp.error && <div className="error-box form-error">{setUp.error.message}</div>}
    </Panel>
  )
}
