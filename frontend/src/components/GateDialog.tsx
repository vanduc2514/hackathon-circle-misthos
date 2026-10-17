import { useCallback, useEffect, useId, useRef, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Check, X } from 'lucide-react'
import { api, shortHash, unwrap, type HealthOut, type MeOut } from '../lib/client'
import { signIn, signOut, useHealth, useMe } from '../lib/session'
import { browserProvider, browserWallet, type Signer } from '../lib/wallet'
import { circleSigner, type CircleApi } from '../lib/circle-wallet'
import { circleSignInId, gateStep, linkedLogin, STEPS, stepsDone, type GateStep } from '../lib/gate'
import { linking } from '../lib/github-link'
import '../gate.css'

/**
 * The sign-in gate's dialog: one step at a time, the first one not done (#181).
 *
 * A real dialog: it takes focus when it opens and gives it back when it closes, keeps
 * Tab inside it, and Esc, the close button or a click on the backdrop leave it. Leaving
 * loses nothing, because each step is kept by the API the moment it is done.
 */

const TITLES: Record<GateStep, string> = {
  wallet: 'Sign in with your wallet',
  github: 'Link your GitHub account',
  side: 'Choose your side',
  done: 'Signed in',
}

const LEDES: Record<GateStep, string> = {
  wallet:
    'Your wallet signs a message naming this site and a one-time code. Signing costs ' +
    'nothing and moves no money. The wallet you sign in with is the wallet the money ' +
    'moves through: a contributor is paid into it, and a publisher funds from it.',
  github:
    "A publisher's issues and a contributor's pull requests are matched to you by your " +
    'GitHub login, so Misthos asks for it before anything else. We keep your login and ' +
    'nothing else.',
  side:
    'A wallet publishes work or does it, not both, and the choice is made once. ' +
    'Choosing creates your account.',
  done: 'Opening the app.',
}

const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]), [tabindex="0"]'

export default function GateDialog({
  onClose,
  onDone,
  refusal,
}: {
  onClose: () => void
  onDone: () => void
  refusal: string | null
}) {
  const health = useHealth()
  const me = useMe()
  const qc = useQueryClient()
  const step = me.isLoading ? null : gateStep(me.data)
  const dialog = useRef<HTMLDivElement>(null)
  const heading = useRef<HTMLHeadingElement>(null)
  const titleId = useId()
  const ledeId = useId()
  // Every page reads who is asking, so a step done here re-reads everything.
  const changed = useCallback(() => qc.invalidateQueries(), [qc])

  // Esc leaves from anywhere, and Tab stays inside.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault()
        onClose()
        return
      }
      if (e.key !== 'Tab' || !dialog.current) return
      const inside = Array.from(dialog.current.querySelectorAll<HTMLElement>(FOCUSABLE))
      if (!inside.length) return
      const first = inside[0]
      const last = inside[inside.length - 1]
      const at = document.activeElement
      if (!dialog.current.contains(at)) {
        e.preventDefault()
        first.focus()
      } else if (e.shiftKey && (at === first || at === heading.current)) {
        e.preventDefault()
        last.focus()
      } else if (!e.shiftKey && at === last) {
        e.preventDefault()
        first.focus()
      }
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onClose])

  // The page behind does not scroll while the gate is open.
  useEffect(() => {
    const before = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {
      document.body.style.overflow = before
    }
  }, [])

  // Focus moves to the step's heading when the gate opens and whenever a step is done.
  useEffect(() => {
    heading.current?.focus()
  }, [step])

  // All three done: the gate closes and the app opens.
  useEffect(() => {
    if (step === 'done') onDone()
  }, [step, onDone])

  const done = stepsDone(me.data)
  const shown: GateStep = step ?? 'wallet'

  return (
    <div
      className="gate-backdrop"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose()
      }}
    >
      <div
        ref={dialog}
        className="gate"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={ledeId}
      >
        <button type="button" className="gate-close" aria-label="Close" onClick={onClose}>
          <X aria-hidden="true" />
        </button>
        <p className="gate-kicker">Sign in to Misthos</p>
        <h2 id={titleId} ref={heading} tabIndex={-1}>
          {step === null ? 'Signing in' : TITLES[shown]}
        </h2>
        <p id={ledeId} className="gate-lede">
          {step === null ? 'Checking whether you are signed in…' : LEDES[shown]}
        </p>

        <ol className="gate-steps" aria-label="The three steps">
          {STEPS.map(({ step: s, label }, i) => (
            <li
              key={s}
              className={done[s] ? 'is-done' : s === step ? 'is-current' : undefined}
              aria-current={s === step ? 'step' : undefined}
            >
              {done[s] && <Check aria-hidden="true" />}
              <span>
                {i + 1}. {label}
              </span>
              {done[s] && <span className="gate-sr">, done</span>}
            </li>
          ))}
        </ol>

        {step === 'wallet' && <WalletStep health={health.data} onSignedIn={changed} />}
        {step === 'github' && me.data && health.data && (
          <GitHubStep health={health.data} refusal={refusal} />
        )}
        {step === 'side' && me.data && <SideStep me={me.data} onChosen={changed} />}

        {me.data && step !== 'done' && <SignedInAs me={me.data} onLeft={changed} />}
      </div>
    </div>
  )
}

/** The Circle routes, for this browser's own sign-in id. */
function circleApi(): CircleApi {
  const sign_in_id = circleSignInId()
  return {
    session: async () =>
      unwrap(await api.POST('/api/v1/auth/circle/session', { body: { sign_in_id } })),
    wallet: async () =>
      unwrap(await api.POST('/api/v1/auth/circle/wallet', { body: { sign_in_id } })),
    sign: async (message) =>
      unwrap(await api.POST('/api/v1/auth/circle/sign', { body: { sign_in_id, message } })),
  }
}

/** Step 1: two doors to one session, MetaMask first. */
function WalletStep({ health, onSignedIn }: { health?: HealthOut; onSignedIn: () => void }) {
  const enter = useMutation({
    mutationFn: async (pick: () => Promise<Signer> | Signer) => signIn(await pick()),
    onSuccess: onSignedIn,
  })
  const extension = Boolean(browserProvider())
  const circle = health?.circle_sign_in ?? false

  return (
    <>
      <section className="gate-section" aria-label="MetaMask">
        <h3>MetaMask</h3>
        {extension ? (
          <div className="gate-actions">
            <button
              type="button"
              className="gate-btn primary"
              disabled={enter.isPending}
              onClick={() => enter.mutate(browserWallet)}
            >
              Sign in with MetaMask
            </button>
          </div>
        ) : (
          <p className="gate-note" role="note">
            No wallet extension was found in this browser.{' '}
            <a href="https://metamask.io/download/" target="_blank" rel="noreferrer">
              Install MetaMask
            </a>{' '}
            and open this again, or sign in with a Circle wallet below.
          </p>
        )}
      </section>

      <section className="gate-section" aria-label="A Circle wallet">
        <h3>A Circle wallet</h3>
        {circle ? (
          <>
            <p className="gate-hint">
              A wallet Circle keeps for you behind a PIN you set in Circle&apos;s own window.
              Neither Misthos nor Circle can move its money without that PIN.
            </p>
            <div className="gate-actions">
              <button
                type="button"
                className={extension ? 'gate-btn' : 'gate-btn primary'}
                disabled={enter.isPending}
                onClick={() => enter.mutate(() => circleSigner(circleApi()))}
              >
                Sign in with a Circle wallet
              </button>
            </div>
          </>
        ) : (
          <p className="gate-note" role="note">
            Circle wallets aren&apos;t set up on this server yet, so MetaMask is the way in here.
          </p>
        )}
      </section>

      {enter.isPending && (
        <p className="gate-hint" role="status">
          Waiting for your wallet…
        </p>
      )}
      {enter.error && (
        <div className="gate-error" role="alert">
          {enter.error.message}
        </div>
      )}
    </>
  )
}

/** Step 2: GitHub, before the side. The login is held for the wallet until then. */
function GitHubStep({
  health,
  refusal,
}: {
  health: HealthOut
  refusal: string | null
}) {
  const way = linking(health)
  const start = useMutation({
    mutationFn: async () => unwrap(await api.POST('/api/v1/auth/github/start')),
    // GitHub asks the account holder, then sends them back to the gate.
    onSuccess: (data) => window.location.assign(data.authorize_url),
  })
  return (
    <div className="gate-section">
      {refusal && (
        <div className="gate-error" role="alert">
          {refusal}
        </div>
      )}
      {way.oauth && (
        <div className="gate-actions">
          <button
            type="button"
            className="gate-btn primary"
            disabled={start.isPending}
            onClick={() => start.mutate()}
          >
            Link with GitHub
          </button>
        </div>
      )}
      {start.error && (
        <div className="gate-error" role="alert">
          {start.error.message}
        </div>
      )}
      {way.notSetUp && (
        <div className="gate-error" role="note">
          GitHub linking isn&apos;t set up on this server, so no GitHub account can be linked
          here yet, and signing in waits on it. To fix it, whoever runs the server creates
          a GitHub OAuth App with the callback{' '}
          <code>{health.github_oauth_callback_url}</code>, sets{' '}
          <code>MISTHOS_GITHUB_OAUTH_CLIENT_ID</code> and{' '}
          <code>MISTHOS_GITHUB_OAUTH_CLIENT_SECRET</code>, and restarts the API.
        </div>
      )}
    </div>
  )
}

/** Step 3: the side, which creates the account carrying the login. */
function SideStep({ me, onChosen }: { me: MeOut; onChosen: () => void }) {
  const login = linkedLogin(me) ?? ''
  const [role, setRole] = useState<'publisher' | 'contributor'>('publisher')
  const [name, setName] = useState('')
  const [budget, setBudget] = useState('5000')
  // A contributor's name on Misthos is their GitHub login: it is who a payout is
  // announced to on the pull request, and who may not approve their own.
  const named = role === 'contributor' ? login : name.trim()

  const choose = useMutation({
    mutationFn: async () =>
      unwrap(
        await api.POST('/api/v1/auth/role', {
          body: { role, name: named, budget_usdc: budget.trim() || '5000' },
        }),
      ),
    onSuccess: onChosen,
  })

  return (
    <form
      className="gate-form gate-section"
      onSubmit={(e) => {
        e.preventDefault()
        choose.mutate()
      }}
    >
      <fieldset className="gate-sides">
        <legend>Your side</legend>
        <label className="gate-side">
          <input
            type="radio"
            name="gate-side"
            checked={role === 'publisher'}
            onChange={() => setRole('publisher')}
          />
          Publisher: I fund fixes
        </label>
        <label className="gate-side">
          <input
            type="radio"
            name="gate-side"
            checked={role === 'contributor'}
            onChange={() => setRole('contributor')}
          />
          Contributor: I fix issues
        </label>
      </fieldset>
      {role === 'publisher' ? (
        <>
          <label className="gate-field">
            <span>Organisation name</span>
            <input
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              maxLength={200}
              autoComplete="organization"
              required
            />
          </label>
          <label className="gate-field">
            <span>Budget in USDC, for your own reporting</span>
            <input
              type="text"
              value={budget}
              onChange={(e) => setBudget(e.target.value)}
              inputMode="decimal"
              pattern="[0-9]+(\.[0-9]{1,6})?"
            />
          </label>
        </>
      ) : (
        <label className="gate-field">
          <span>Your name</span>
          <input type="text" value={login} readOnly aria-describedby="gate-name-hint" />
          <span id="gate-name-hint" className="gate-hint" style={{ fontWeight: 400 }}>
            A contributor is known by their GitHub login: it is who a payout is announced to.
          </span>
        </label>
      )}
      <div className="gate-actions">
        <button type="submit" className="gate-btn primary" disabled={choose.isPending || !named}>
          {choose.isPending ? 'Working…' : `Continue as a ${role}`}
        </button>
      </div>
      {choose.error && (
        <div className="gate-error" role="alert">
          {choose.error.message}
        </div>
      )}
    </form>
  )
}

/** Who the gate is signing in, and the way to start again with another wallet. */
function SignedInAs({ me, onLeft }: { me: MeOut; onLeft: () => void }) {
  const leave = useMutation({ mutationFn: signOut, onSuccess: onLeft })
  const login = linkedLogin(me)
  return (
    <div className="gate-who">
      <span>
        Signed in as <code title={me.address}>{shortHash(me.address)}</code>
        {login && (
          <>
            {' '}
            · GitHub <strong>{login}</strong>
          </>
        )}
      </span>
      <button
        type="button"
        className="gate-link"
        disabled={leave.isPending}
        onClick={() => leave.mutate()}
      >
        Use another wallet
      </button>
    </div>
  )
}
