import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useSearchParams } from 'react-router-dom'
import {
  api,
  shortHash,
  unwrap,
  type Account,
  type HealthOut,
  type MeOut,
  type SsoOut,
} from '../lib/client'
import {
  connectWallet,
  demoGitHubSignIn,
  demoSsoSignIn,
  signIn,
  signInWithGitHub,
  signInWithSso,
  signOut,
  useHealth,
  useMe,
} from '../lib/session'
import { browserProvider, browserWallet, demoWallet, demoWalletFor, type Signer } from '../lib/wallet'
import { setUpWallet } from '../lib/circle-wallet'
import { linking } from '../lib/github-link'
import { signedInAs } from '../lib/identity'
import { parseDomains, ssoStep } from '../lib/sso'
import { supportStatus } from '../lib/support'
import { Panel } from '../components/ui'

/**
 * Signing in, in the order the API asks for it: sign in with GitHub, or with a wallet;
 * take a side once; then connect what is missing. An account that signed in with
 * GitHub connects a wallet before money moves, and one that signed in with a wallet
 * links GitHub before it publishes or claims. Each step appears once the one before it
 * is done.
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

  const leave = useMutation({ mutationFn: signOut, onSuccess: changed })

  return (
    <>
      <div className="page-head">
        <div>
          <h1 className="page-title">Account</h1>
          <p className="page-sub">
            Sign in with GitHub, which is how your issues and pull requests are matched to
            you, or with a wallet. Choose whether you publish work or do it, and connect a
            wallet when money is about to move: a publisher funds from it, a contributor is
            paid to it.
          </p>
        </div>
      </div>

      {params.get('signed_in') === 'github' && me.data?.github_login && (
        <div className="banner">You are signed in with GitHub as {me.data.github_login}.</div>
      )}
      {params.get('linked') === 'github' && account?.github_login && (
        <div className="banner">Your GitHub account {account.github_login} is linked.</div>
      )}
      {params.get('signed_in') === 'sso' && me.data?.sso_email && (
        <div className="banner">
          You are signed in through your organisation's single sign-on as {me.data.sso_email}.
        </div>
      )}
      {me.data?.sso_required && account && (
        <div className="error-box" role="note">
          {account.party_id} acts only through its single sign-on. Sign out, then sign in with
          your work e-mail to act for it.
        </div>
      )}

      <div className="form-stack">
        {me.isLoading ? (
          <div className="empty">Loading…</div>
        ) : !me.data ? (
          <SignIn health={health.data} simulated={simulated} onDone={changed} />
        ) : (
          <>
            <Panel title="Signed in">
              <dl className="kv">
                <dt>Signed in with</dt>
                <dd>
                  {me.data.method === 'github'
                    ? 'GitHub'
                    : me.data.method === 'sso'
                      ? "your organisation's single sign-on"
                      : 'a wallet'}
                  , as {signedInAs(me.data)}
                </dd>
                <dt>Wallet</dt>
                <dd className="muted" title={me.data.address ?? undefined}>
                  {me.data.address ? shortHash(me.data.address) : 'none connected'}
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

            {account?.role === 'publisher' && account.github_login && (
              <Repositories publisherId={account.party_id} />
            )}
            {account?.role === 'publisher' && !me.data.sso_required && (
              <SingleSignOn me={me.data} publisherId={account.party_id} onDone={changed} />
            )}
            {account?.role === 'publisher' && !me.data.sso_required && (
              <Support publisherId={account.party_id} />
            )}
            {!account && <ChooseSide github={me.data.method === 'github'} onDone={changed} />}
            {!account && me.data.method === 'github' && (
              <ConnectWallet me={me.data} account={null} simulated={simulated} onDone={changed} />
            )}
            {account && !me.data.wallet && (
              <ConnectWallet me={me.data} account={account} simulated={simulated} onDone={changed} />
            )}
            {account && <PayoutWallet account={account} />}
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
 * The two ways in. GitHub first, where the server has an OAuth App, and in the
 * simulation a typed login standing in for it; then a wallet, the browser's own or,
 * in the simulation, a demo key kept in this browser.
 */
function SignIn({
  health,
  simulated,
  onDone,
}: {
  health: HealthOut | undefined
  simulated: boolean
  onDone: () => void
}) {
  const [login, setLogin] = useState('')
  const way = health ? linking(health) : null

  // GitHub asks the user, then sends them back to /account?signed_in=github.
  const github = useMutation({ mutationFn: signInWithGitHub })
  const demo = useMutation({ mutationFn: () => demoGitHubSignIn(login.trim()), onSuccess: onDone })
  const wallet = useMutation({
    mutationFn: async (pick: () => Promise<Signer> | Signer) => signIn(await pick()),
    onSuccess: onDone,
  })

  return (
    <>
      <Panel title="Sign in with GitHub">
        <p className="dim" style={{ marginBottom: 14 }}>
          Your GitHub account is how your issues and pull requests are matched to you. We
          keep your GitHub user id and login, and nothing else. No wallet is needed until
          money is about to move.
        </p>
        {way?.oauth && (
          <div className="btn-row">
            <button
              className="btn primary"
              onClick={() => github.mutate()}
              disabled={github.isPending}
            >
              Sign in with GitHub
            </button>
          </div>
        )}
        {github.error && <div className="error-box form-error">{github.error.message}</div>}
        {way?.notSetUp && health && (
          <div className={way.notSetUp === 'deployment' ? 'error-box' : 'banner'} role="note">
            GitHub sign-in isn't set up on this server
            {way.notSetUp === 'simulation'
              ? ', so in the simulation the login you type below stands in for it.'
              : ', so sign in with a wallet below.'}{' '}
            To set it up, whoever runs the server creates a GitHub OAuth App with the callback{' '}
            <code>{health.github_oauth_callback_url}</code>, sets{' '}
            <code>MISTHOS_GITHUB_OAUTH_CLIENT_ID</code> and{' '}
            <code>MISTHOS_GITHUB_OAUTH_CLIENT_SECRET</code>, and restarts the API.
          </div>
        )}
        {way?.byLogin && (
          <form
            className="form"
            style={{ marginTop: 14 }}
            onSubmit={(e) => {
              e.preventDefault()
              demo.mutate()
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
                disabled={demo.isPending || !login.trim()}
              >
                Sign in as this login
              </button>
            </div>
            {demo.error && <div className="error-box form-error">{demo.error.message}</div>}
          </form>
        )}
      </Panel>

      <SsoSignIn simulated={simulated} onDone={onDone} />

      <Panel title="Or sign in with a wallet">
        <p className="dim" style={{ marginBottom: 14 }}>
          Your wallet signs a message naming this site and a one-time code. Signing costs
          nothing and moves no money. You link your GitHub account next.
        </p>
        <div className="btn-row">
          <button
            className="btn"
            disabled={!browserProvider() || wallet.isPending}
            onClick={() => wallet.mutate(browserWallet)}
          >
            Sign in with your wallet
          </button>
          {simulated && (
            <>
              <button
                className="btn"
                disabled={wallet.isPending}
                onClick={() => wallet.mutate(() => demoWallet('publisher'))}
              >
                Demo publisher wallet
              </button>
              <button
                className="btn"
                disabled={wallet.isPending}
                onClick={() => wallet.mutate(() => demoWallet('contributor'))}
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
        {wallet.error && <div className="error-box form-error">{wallet.error.message}</div>}
      </Panel>
    </>
  )
}

/**
 * Connect a wallet by signing one message, which proves it is yours (#131). With an
 * account, it becomes where a publisher funds from or a contributor is paid. Signed in
 * with GitHub before choosing a side, it is the way back to an account that signed in
 * with that wallet before GitHub sign-in existed: the two are joined.
 */
function ConnectWallet({
  me,
  account,
  simulated,
  onDone,
}: {
  me: MeOut
  account: Account | null
  simulated: boolean
  onDone: () => void
}) {
  const connect = useMutation({
    mutationFn: async (pick: () => Promise<Signer> | Signer) => connectWallet(await pick()),
    onSuccess: onDone,
  })
  // An account's own demo key, never a side's sign-in key, which may be an account of
  // its own here already. Joining an old account needs the key that account used.
  const demo = account ? demoWalletFor(account.party_id) : null

  return (
    <Panel title={account ? 'Connect a wallet' : 'Signed in with a wallet before?'}>
      <p className="dim" style={{ marginBottom: 14 }}>
        {!account
          ? `Connect the wallet you signed in with instead of choosing a side. Its account is joined to ${signedInAs(me)}, with everything it did.`
          : account.role === 'publisher'
            ? 'Publishing and getting a price need no wallet. Approving a price does: the escrow takes the commitment from this wallet alone, and refunds it there.'
            : 'Claiming needs somewhere to pay you: connect a wallet, or set up your Circle wallet below.'}{' '}
        The wallet signs one message, which costs nothing and moves no money.
      </p>
      <div className="btn-row">
        <button
          className={account ? 'btn primary' : 'btn'}
          disabled={!browserProvider() || connect.isPending}
          onClick={() => connect.mutate(browserWallet)}
        >
          Connect your browser wallet
        </button>
        {simulated && demo && (
          <button className="btn" disabled={connect.isPending} onClick={() => connect.mutate(() => demo)}>
            Use a demo wallet
          </button>
        )}
        {simulated && !account && (
          <>
            <button
              className="btn"
              disabled={connect.isPending}
              onClick={() => connect.mutate(() => demoWallet('publisher'))}
            >
              Demo publisher wallet
            </button>
            <button
              className="btn"
              disabled={connect.isPending}
              onClick={() => connect.mutate(() => demoWallet('contributor'))}
            >
              Demo contributor wallet
            </button>
          </>
        )}
      </div>
      {!browserProvider() && (
        <p className="stat-hint" style={{ marginTop: 10 }}>
          No wallet extension was found in this browser.
        </p>
      )}
      {connect.error && <div className="error-box form-error">{connect.error.message}</div>}
    </Panel>
  )
}

/**
 * Repositories the publisher installed the GitHub App on (#6). There, an issue is
 * priced by a label and funded by a comment, without coming back here.
 */
function Repositories({ publisherId }: { publisherId: string }) {
  const { data } = useQuery({
    queryKey: ['repositories', publisherId],
    queryFn: async () =>
      unwrap(
        await api.GET('/api/v1/publishers/{publisher_id}/repositories', {
          params: { path: { publisher_id: publisherId } },
        }),
      ),
  })
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
          No repositories yet. Install the Misthos GitHub App on one, signed in to GitHub as
          the account you linked.
        </p>
      )}
      <p className="stat-hint">
        On a connected repository, add the <code>{data.label}</code> label to an issue to have
        it priced, then comment <code>/misthos approve</code> on it to commit the funds.
        Contributors claim it with <code>/misthos claim</code>.
      </p>
      {data.install_url && (
        <div className="btn-row" style={{ marginTop: 12 }}>
          <a className="btn" href={data.install_url} target="_blank" rel="noreferrer">
            Install the GitHub App
          </a>
        </div>
      )}
    </Panel>
  )
}

/**
 * The party's own Circle wallet. A contributor is paid into it. A publisher keeps it
 * beside the wallet they signed in with, which is still the one they fund from: the
 * escrow takes a commitment only from the wallet the approval names, and the browser
 * commits from the signed-in one. Setting it up opens Circle's frame for a PIN the
 * platform never sees.
 */
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
        {party === 'contributor' ? (
          'Your payouts are released to this wallet.'
        ) : (
          <>
            A wallet of your own on Arc, kept beside the one you fund from.{' '}
            {account.address ? (
              <>
                You still fund issues and pay for plans from{' '}
                <span className="mono-num" title={account.address}>
                  {shortHash(account.address)}
                </span>
                : the escrow takes a commitment only from that wallet and refunds it there, so
                move USDC to it before you fund.
              </>
            ) : (
              'It does not fund issues: the escrow takes a commitment only from the wallet you connect above, and refunds it there.'
            )}
          </>
        )}{' '}
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

function ChooseSide({ github, onDone }: { github: boolean; onDone: () => void }) {
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
        An account publishes work or does it, not both, and the choice is made once.
        {github && ' A contributor is known by their GitHub login.'}
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

/**
 * The way in for an Enterprise organisation's staff (#53): the work e-mail's domain
 * finds the organisation's identity provider. In the simulation a typed address stands
 * in for the provider.
 */
function SsoSignIn({ simulated, onDone }: { simulated: boolean; onDone: () => void }) {
  const [email, setEmail] = useState('')
  // The provider asks the user, then sends them back to /account?signed_in=sso.
  const start = useMutation({ mutationFn: () => signInWithSso(email.trim()) })
  const demo = useMutation({ mutationFn: () => demoSsoSignIn(email.trim()), onSuccess: onDone })

  return (
    <Panel title="Or sign in with single sign-on">
      <p className="dim" style={{ marginBottom: 14 }}>
        If your organisation signs in to Misthos through its identity provider, use your work
        e-mail. You act for the organisation, and what you do is recorded under your address.
      </p>
      <form
        className="form"
        onSubmit={(e) => {
          e.preventDefault()
          start.mutate()
        }}
      >
        <label className="field">
          <span>Work e-mail</span>
          <input
            className="input"
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            maxLength={254}
            placeholder="you@company.com"
            required
          />
        </label>
        <div className="btn-row">
          <button className="btn" type="submit" disabled={start.isPending || !email.trim()}>
            Continue with single sign-on
          </button>
          {simulated && (
            <button
              className="btn"
              type="button"
              disabled={demo.isPending || !email.trim()}
              onClick={() => demo.mutate()}
            >
              Sign in as this address (simulation)
            </button>
          )}
        </div>
        {start.error && <div className="error-box form-error">{start.error.message}</div>}
        {demo.error && <div className="error-box form-error">{demo.error.message}</div>}
      </form>
    </Panel>
  )
}

/**
 * The organisation's connection to its own identity provider (#53). Enterprise only.
 * Requiring it is offered only to a session that came through it, as the API insists:
 * a provider that has never signed anyone in cannot lock the organisation out.
 */
function SingleSignOn({
  me,
  publisherId,
  onDone,
}: {
  me: MeOut
  publisherId: string
  onDone: () => void
}) {
  const path = { params: { path: { publisher_id: publisherId } } }
  const sso = useQuery({
    queryKey: ['sso', publisherId],
    queryFn: async () => unwrap(await api.GET('/api/v1/publishers/{publisher_id}/sso', path)),
  })
  if (!sso.data) return null
  const step = ssoStep(sso.data, me)

  return (
    <Panel title="Single sign-on">
      {step === 'upgrade' ? (
        <p className="dim">
          Let your staff sign in through your own identity provider, and turn them off there
          the day they leave. Single sign-on is in the Enterprise plan.{' '}
          <Link to="/plans">See the plans</Link>.
        </p>
      ) : (
        <SsoForm
          sso={sso.data}
          step={step}
          publisherId={publisherId}
          onDone={onDone}
        />
      )}
    </Panel>
  )
}

function SsoForm({
  sso,
  step,
  publisherId,
  onDone,
}: {
  sso: SsoOut
  step: ReturnType<typeof ssoStep>
  publisherId: string
  onDone: () => void
}) {
  const kept = sso.connection
  const [issuer, setIssuer] = useState(kept?.issuer ?? '')
  const [clientId, setClientId] = useState(kept?.client_id ?? '')
  const [secretRef, setSecretRef] = useState(kept?.client_secret_ref ?? '')
  const [domains, setDomains] = useState(kept?.domains.join(', ') ?? '')
  const path = { params: { path: { publisher_id: publisherId } } }

  const save = useMutation({
    mutationFn: async (required: boolean) =>
      unwrap(
        await api.PUT('/api/v1/publishers/{publisher_id}/sso', {
          ...path,
          body: {
            issuer: issuer.trim(),
            client_id: clientId.trim(),
            client_secret_ref: secretRef.trim(),
            domains: parseDomains(domains),
            required,
          },
        }),
      ),
    onSuccess: onDone,
  })
  const remove = useMutation({
    mutationFn: async () => {
      const result = await api.DELETE('/api/v1/publishers/{publisher_id}/sso', path)
      if (!result.response.ok) unwrap(result)
    },
    onSuccess: onDone,
  })
  const required = kept?.required ?? false

  return (
    <>
      <p className="dim" style={{ marginBottom: 14 }}>
        Register Misthos with your identity provider as an OpenID Connect web application with
        the redirect URI <code>{sso.callback_url}</code>. The client secret goes into our secret
        store during onboarding; name it here by its reference.
      </p>
      {step === 'try' && (
        <div className="banner" role="note">
          Connected. Sign out and sign in with your work e-mail to try it: requiring single
          sign-on is offered only to a session that came through it.
        </div>
      )}
      {step === 'required' && (
        <div className="banner" role="note">
          Required: this organisation is acted for only through its single sign-on.
        </div>
      )}
      <form
        className="form"
        onSubmit={(e) => {
          e.preventDefault()
          save.mutate(required)
        }}
      >
        <label className="field">
          <span>Issuer URL</span>
          <input
            className="input"
            value={issuer}
            onChange={(e) => setIssuer(e.target.value)}
            placeholder="https://company.okta.com"
            required
          />
        </label>
        <label className="field">
          <span>Client id</span>
          <input
            className="input"
            value={clientId}
            onChange={(e) => setClientId(e.target.value)}
            maxLength={256}
            required
          />
        </label>
        <label className="field">
          <span>Client secret reference</span>
          <input
            className="input"
            value={secretRef}
            onChange={(e) => setSecretRef(e.target.value)}
            maxLength={128}
            placeholder="company-oidc"
            required
          />
        </label>
        <label className="field">
          <span>E-mail domains it speaks for</span>
          <input
            className="input"
            value={domains}
            onChange={(e) => setDomains(e.target.value)}
            placeholder="company.com, labs.company.com"
            required
          />
        </label>
        <div className="btn-row">
          <button className="btn primary" type="submit" disabled={save.isPending}>
            {kept ? 'Save the connection' : 'Connect your provider'}
          </button>
          {step === 'require' && (
            <button className="btn" type="button" onClick={() => save.mutate(true)}>
              Require single sign-on
            </button>
          )}
          {step === 'required' && (
            <button className="btn" type="button" onClick={() => save.mutate(false)}>
              Stop requiring it
            </button>
          )}
          {kept && (
            <button className="btn" type="button" onClick={() => remove.mutate()}>
              Remove single sign-on
            </button>
          )}
        </div>
        {save.error && <div className="error-box form-error">{save.error.message}</div>}
        {remove.error && <div className="error-box form-error">{remove.error.message}</div>}
      </form>
    </>
  )
}

/**
 * The Enterprise support commitment (#53): what the plan promises, a way to ask, and
 * each request against its deadline. The record stays visible after a plan lapses.
 */
function Support({ publisherId }: { publisherId: string }) {
  const qc = useQueryClient()
  const path = { params: { path: { publisher_id: publisherId } } }
  const [severity, setSeverity] = useState<'urgent' | 'normal'>('normal')
  const [subject, setSubject] = useState('')
  const [body, setBody] = useState('')
  const support = useQuery({
    queryKey: ['support', publisherId],
    queryFn: async () => unwrap(await api.GET('/api/v1/publishers/{publisher_id}/support', path)),
  })
  const ask = useMutation({
    mutationFn: async () =>
      unwrap(
        await api.POST('/api/v1/publishers/{publisher_id}/support', {
          ...path,
          body: { severity, subject: subject.trim(), body: body.trim() },
        }),
      ),
    onSuccess: () => {
      setSubject('')
      setBody('')
      qc.invalidateQueries({ queryKey: ['support', publisherId] })
    },
  })
  const data = support.data
  if (!data || (!data.entitled && !data.requests.length)) return null

  return (
    <Panel title="Support">
      <p className="dim" style={{ marginBottom: 14 }}>
        Your plan's commitment: {data.commitment}.
      </p>
      {data.entitled && (
        <form
          className="form"
          onSubmit={(e) => {
            e.preventDefault()
            ask.mutate()
          }}
        >
          <div className="radio-row">
            <label>
              <input
                type="radio"
                checked={severity === 'urgent'}
                onChange={() => setSeverity('urgent')}
              />{' '}
              Urgent: money cannot move
            </label>
            <label>
              <input
                type="radio"
                checked={severity === 'normal'}
                onChange={() => setSeverity('normal')}
              />{' '}
              Anything else
            </label>
          </div>
          <label className="field">
            <span>Subject</span>
            <input
              className="input"
              value={subject}
              onChange={(e) => setSubject(e.target.value)}
              maxLength={200}
              required
            />
          </label>
          <label className="field">
            <span>What is happening</span>
            <textarea
              className="input"
              value={body}
              onChange={(e) => setBody(e.target.value)}
              maxLength={5000}
              rows={4}
              required
            />
          </label>
          <div className="btn-row">
            <button
              className="btn primary"
              type="submit"
              disabled={ask.isPending || !subject.trim() || !body.trim()}
            >
              Ask for support
            </button>
          </div>
          {ask.error && <div className="error-box form-error">{ask.error.message}</div>}
        </form>
      )}
      {data.requests.length > 0 && (
        <ul className="findings" style={{ marginTop: 14 }}>
          {data.requests.map((r) => (
            <li key={r.id}>
              <strong>{r.id}</strong> {r.subject}{' '}
              <span className={r.overdue ? 'error-text' : 'dim'}>· {supportStatus(r)}</span>
              {r.first_response && (
                <div className="stat-hint">
                  {r.responder}: {r.first_response}
                </div>
              )}
            </li>
          ))}
        </ul>
      )}
    </Panel>
  )
}
