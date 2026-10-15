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
import { demoSsoSignIn, signIn, signInWithSso, signOut, useHealth, useMe } from '../lib/session'
import { browserProvider, browserWallet, demoWallet, type Signer } from '../lib/wallet'
import { setUpWallet } from '../lib/circle-wallet'
import { linking } from '../lib/github-link'
import { parseDomains, ssoStep } from '../lib/sso'
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
        ) : null}
        {!me.isLoading && !me.data && <SsoSignIn simulated={simulated} onDone={changed} />}
        {me.isLoading || !me.data ? null : (
          <>
            <Panel title="Signed in">
              <dl className="kv">
                {me.data.sso_email && (
                  <>
                    <dt>Signed in as</dt>
                    <dd>{me.data.sso_email}, through your organisation's single sign-on</dd>
                  </>
                )}
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
            {account?.role === 'publisher' && !me.data.sso_required && (
              <SingleSignOn me={me.data} publisherId={account.party_id} onDone={changed} />
            )}
            {account && <PayoutWallet account={account} />}
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

/**
 * The party's own Circle wallet. A contributor is paid into it; a publisher funds
 * from it. Setting it up opens Circle's frame for a PIN the platform never sees.
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
