import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import { api, shortHash, unwrap } from '../lib/client'
import { signIn, useHealth, useMe } from '../lib/session'
import { browserProvider, browserWallet, demoWallet, type Signer } from '../lib/wallet'
import { circleWallet } from '../lib/circle-wallet'

export interface SignInGateProps {
  isOpen: boolean
  onClose: () => void
  onSuccess?: () => void
}

export function SignInGate({ isOpen, onClose, onSuccess }: SignInGateProps) {
  const me = useMe()
  const health = useHealth()
  const qc = useQueryClient()
  const navigate = useNavigate()

  const dialogRef = useRef<HTMLDivElement>(null)
  const previousFocus = useRef<HTMLElement | null>(null)

  // Step state
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Step 2 GitHub state
  const [githubLogin, setGithubLogin] = useState('')

  // Step 3 Role state
  const [role, setRole] = useState<'publisher' | 'contributor'>('publisher')
  const [partyName, setPartyName] = useState('')
  const [budgetUsdc, setBudgetUsdc] = useState('5000')

  const isSimulated = health.data?.simulated ?? true
  const hasBrowserWallet = Boolean(browserProvider())

  // Determine current step (1, 2, or 3)
  const isStep1Done = Boolean(me.data?.address)
  const isStep2Done = Boolean(me.data?.pending_github_login || me.data?.account?.github_login)
  const isStep3Done = Boolean(me.data?.account?.role)

  const activeStep = !isStep1Done ? 1 : !isStep2Done ? 2 : 3

  // Accessibility: store previous focus, move focus into dialog, handle Escape and focus trap
  useEffect(() => {
    if (isOpen) {
      previousFocus.current = document.activeElement as HTMLElement
      setError(null)
      // Focus dialog on open
      requestAnimationFrame(() => {
        dialogRef.current?.focus()
      })
    } else {
      previousFocus.current?.focus()
    }
  }, [isOpen])

  // If already finished, close and navigate
  useEffect(() => {
    if (isOpen && isStep3Done) {
      onClose()
      if (onSuccess) onSuccess()
      else if (window.location.pathname === '/' || window.location.pathname === '') navigate('/overview')
    }
  }, [isOpen, isStep3Done, onClose, onSuccess, navigate])

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Escape') {
      e.stopPropagation()
      onClose()
      return
    }

    if (e.key === 'Tab') {
      const focusable = dialogRef.current?.querySelectorAll<HTMLElement>(
        'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])',
      )
      if (!focusable || focusable.length === 0) return
      const first = focusable[0]
      const last = focusable[focusable.length - 1]

      if (e.shiftKey && document.activeElement === first) {
        last.focus()
        e.preventDefault()
      } else if (!e.shiftKey && document.activeElement === last) {
        first.focus()
        e.preventDefault()
      }
    }
  }

  // Action: Sign in with a wallet (Step 1)
  const handleConnectWallet = async (getSigner: () => Promise<Signer> | Signer) => {
    setBusy(true)
    setError(null)
    try {
      const signer = await getSigner()
      await signIn(signer)
      await qc.invalidateQueries({ queryKey: ['me'] })
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  // Action: Link GitHub (Step 2)
  const handleLinkGitHubOAuth = async () => {
    setBusy(true)
    setError(null)
    try {
      const res = unwrap(await api.POST('/api/v1/auth/github/start'))
      window.location.href = res.authorize_url
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
      setBusy(false)
    }
  }

  const handleLinkGitHubSimulated = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!githubLogin.trim()) return
    setBusy(true)
    setError(null)
    try {
      unwrap(
        await api.POST('/api/v1/auth/github/simulate', {
          body: { login: githubLogin.trim() },
        }),
      )
      await qc.invalidateQueries({ queryKey: ['me'] })
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  // Action: Choose side and finish (Step 3)
  const handleFinish = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!partyName.trim()) return
    setBusy(true)
    setError(null)
    try {
      unwrap(
        await api.POST('/api/v1/auth/role', {
          body: {
            role,
            name: partyName.trim(),
            budget_usdc: role === 'publisher' ? budgetUsdc.trim() || '5000' : '5000',
          },
        }),
      )
      await qc.invalidateQueries({ queryKey: ['me'] })
      onClose()
      if (onSuccess) onSuccess()
      else if (window.location.pathname === '/' || window.location.pathname === '') navigate('/overview')
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  if (!isOpen) return null

  return (
    <div className="gate-backdrop" onClick={onClose} role="presentation">
      <div
        className="gate-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="gate-title"
        tabIndex={-1}
        ref={dialogRef}
        onClick={(e) => e.stopPropagation()}
        onKeyDown={handleKeyDown}
      >
        <button
          type="button"
          className="gate-close-button"
          onClick={onClose}
          aria-label="Close sign in dialog"
        >
          &times;
        </button>

        <header className="gate-header">
          <div className="gate-kicker">Sign in to Misthos</div>
          <h2 id="gate-title" className="gate-title">
            One gate, three steps
          </h2>
          <p className="gate-subtitle">
            Prove a wallet, link your GitHub account, then choose your side.
          </p>
        </header>

        <nav aria-label="Sign-in progress" className="gate-steps-nav">
          <ol className="gate-steps-list">
            <li className={`gate-step-indicator ${isStep1Done ? 'done' : activeStep === 1 ? 'active' : ''}`}>
              <span className="step-num">{isStep1Done ? '✓' : '1'}</span>
              <span className="step-label">Wallet</span>
            </li>
            <li className={`gate-step-indicator ${isStep2Done ? 'done' : activeStep === 2 ? 'active' : ''}`}>
              <span className="step-num">{isStep2Done ? '✓' : '2'}</span>
              <span className="step-label">GitHub</span>
            </li>
            <li className={`gate-step-indicator ${isStep3Done ? 'done' : activeStep === 3 ? 'active' : ''}`}>
              <span className="step-num">{isStep3Done ? '✓' : '3'}</span>
              <span className="step-label">Side</span>
            </li>
          </ol>
        </nav>

        {error && (
          <div className="gate-error" role="alert">
            {error}
          </div>
        )}

        <div className="gate-content">
          {activeStep === 1 && (
            <section className="gate-step-panel" aria-labelledby="step1-heading">
              <h3 id="step1-heading" className="gate-panel-title">
                Step 1: Prove a wallet
              </h3>
              <p className="gate-panel-copy">
                Sign a standard SIWE message to prove control of your address. This costs nothing and moves no money.
              </p>

              <div className="gate-button-group">
                {hasBrowserWallet ? (
                  <button
                    type="button"
                    className="landing-primary-button gate-button"
                    disabled={busy}
                    onClick={() => handleConnectWallet(browserWallet)}
                  >
                    Connect MetaMask
                  </button>
                ) : (
                  <div className="gate-notice-box">
                    No wallet extension detected in this browser. You can still enter with a Circle wallet below.
                  </div>
                )}

                <button
                  type="button"
                  className="landing-ghost-button gate-button"
                  disabled={busy}
                  onClick={() => handleConnectWallet(circleWallet)}
                >
                  Circle wallet
                </button>

                {isSimulated && (
                  <div className="gate-demo-wallets">
                    <div className="gate-divider"><span>or simulation demo</span></div>
                    <button
                      type="button"
                      className="landing-ghost-button gate-button"
                      disabled={busy}
                      onClick={() => handleConnectWallet(() => demoWallet('publisher'))}
                    >
                      Demo publisher wallet
                    </button>
                    <button
                      type="button"
                      className="landing-ghost-button gate-button"
                      disabled={busy}
                      onClick={() => handleConnectWallet(() => demoWallet('contributor'))}
                    >
                      Demo contributor wallet
                    </button>
                  </div>
                )}
              </div>
            </section>
          )}

          {activeStep === 2 && (
            <section className="gate-step-panel" aria-labelledby="step2-heading">
              <h3 id="step2-heading" className="gate-panel-title">
                Step 2: Link your GitHub account
              </h3>
              <p className="gate-panel-copy">
                Wallet connected: <code>{shortHash(me.data?.address ?? '')}</code>
              </p>
              <p className="gate-panel-copy">
                Every action on Misthos is linked to your GitHub login. Link it now before choosing your side.
              </p>

              <form onSubmit={handleLinkGitHubSimulated} className="gate-form">
                <label htmlFor="github-login-input" className="gate-label">
                  Your GitHub login
                </label>
                <div className="gate-input-row">
                  <input
                    id="github-login-input"
                    type="text"
                    className="gate-input"
                    value={githubLogin}
                    onChange={(e) => setGithubLogin(e.target.value)}
                    placeholder="e.g. octocat"
                    autoFocus
                    required
                    disabled={busy}
                  />
                  <button
                    type="submit"
                    className="landing-primary-button gate-button"
                    disabled={busy || !githubLogin.trim()}
                  >
                    Link this login
                  </button>
                </div>
              </form>

              {!isSimulated && (
                <div className="gate-oauth-option">
                  <div className="gate-divider"><span>or through GitHub</span></div>
                  <button
                    type="button"
                    className="landing-ghost-button gate-button"
                    disabled={busy}
                    onClick={handleLinkGitHubOAuth}
                  >
                    Connect with GitHub OAuth
                  </button>
                </div>
              )}
            </section>
          )}

          {activeStep === 3 && (
            <section className="gate-step-panel" aria-labelledby="step3-heading">
              <h3 id="step3-heading" className="gate-panel-title">
                Step 3: Choose your side
              </h3>
              <p className="gate-panel-copy">
                Wallet: <code>{shortHash(me.data?.address ?? '')}</code> &middot; GitHub:{' '}
                <strong>{me.data?.pending_github_login ?? me.data?.account?.github_login}</strong>
              </p>

              <form onSubmit={handleFinish} className="gate-form">
                <fieldset className="gate-fieldset">
                  <legend className="gate-label">Choose your side</legend>
                  <label className="gate-radio-label">
                    <input
                      type="radio"
                      name="role"
                      value="publisher"
                      checked={role === 'publisher'}
                      onChange={() => setRole('publisher')}
                    />
                    <span>
                      <strong>Publisher: I post issues and set prices</strong>
                    </span>
                  </label>
                  <label className="gate-radio-label">
                    <input
                      type="radio"
                      name="role"
                      value="contributor"
                      checked={role === 'contributor'}
                      onChange={() => setRole('contributor')}
                    />
                    <span>
                      <strong>Contributor: I fix issues</strong>
                    </span>
                  </label>
                </fieldset>

                <div className="gate-field">
                  <label htmlFor="party-name-input" className="gate-label">
                    {role === 'publisher' ? 'Organisation name' : 'Your name'}
                  </label>
                  <input
                    id="party-name-input"
                    type="text"
                    className="gate-input"
                    value={partyName}
                    onChange={(e) => setPartyName(e.target.value)}
                    placeholder={role === 'publisher' ? 'e.g. Acme Widgets' : 'e.g. Alice Developer'}
                    autoFocus
                    required
                    disabled={busy}
                  />
                </div>

                {role === 'publisher' && (
                  <div className="gate-field">
                    <label htmlFor="budget-usdc-input" className="gate-label">
                      Budget (USDC)
                    </label>
                    <input
                      id="budget-usdc-input"
                      type="text"
                      className="gate-input"
                      value={budgetUsdc}
                      onChange={(e) => setBudgetUsdc(e.target.value)}
                      placeholder="5000"
                      disabled={busy}
                    />
                  </div>
                )}

                <button
                  type="submit"
                  className="landing-primary-button gate-button"
                  disabled={busy || !partyName.trim()}
                >
                  {role === 'publisher' ? 'Continue as a publisher' : 'Continue as a contributor'}
                </button>
              </form>
            </section>
          )}
        </div>
      </div>
    </div>
  )
}
