import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import {
  ArrowDownRight,
  ArrowRight,
  ArrowUpRight,
  Check,
  CircleDollarSign,
  Clock3,
  Code2,
  GitPullRequest,
  ShieldCheck,
  Sparkles,
  Wallet,
} from 'lucide-react'
import { api, type MetricsOut } from '../lib/client'
import { useHealth, useMe } from '../lib/session'
import LandingIssues from '../components/LandingIssues'
import { useGate } from '../components/SignInGate'
import '../landing.css'

/**
 * The public front page: what Misthos is, the work on offer right now, and the way in.
 *
 * Ported from the landing-page-for-misthos prototype. Its snapshot and its issue table
 * were mock data; here both read the API, so the page says what this deployment holds.
 * The issue card in the hero stays an illustration of a funded issue.
 */

const REPO_URL = 'https://github.com/vanduc2514/hackathon-circle-misthos'

const steps = [
  {
    number: '01',
    title: 'Fund a real issue',
    description: 'Choose an issue, let Misthos propose the price, and attach money to it.',
    icon: CircleDollarSign,
  },
  {
    number: '02',
    title: 'A contributor claims it',
    description:
      'One clear price. One contributor at a time. No bidding, awkward negotiation, or guesswork.',
    icon: Code2,
  },
  {
    number: '03',
    title: 'Ship, review, get paid',
    description:
      'An agent checks the pull request. The maintainer makes the final call, then USDC is released.',
    icon: GitPullRequest,
  },
]

/** Say what the money is, in the words a visitor would look for. */
const moneyWords: Record<'test' | 'real' | 'unknown', string> = {
  test: 'test USDC',
  real: 'real USDC',
  unknown: 'not Arc: treat as real',
}

/** $1,473.63 as "1.5k", the way the snapshot reads at a glance. */
function compactUsd(value: string | number | null | undefined): string {
  const n = Number(value ?? NaN)
  if (!Number.isFinite(n)) return '—'
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1).replace(/\.0$/, '')}m`
  if (n >= 1_000) return `${(n / 1_000).toFixed(1).replace(/\.0$/, '')}k`
  return n.toFixed(0)
}

function BrandMark() {
  return (
    <a className="lp-brand" href="#top" aria-label="Misthos home">
      <span className="brand-symbol" aria-hidden="true">
        m.
      </span>
      <span>misthos</span>
    </a>
  )
}

function IssuePreview() {
  return (
    <div id="marketplace" className="preview-wrap" aria-label="Illustrative funded issue preview">
      <div className="orbit orbit-one" aria-hidden="true" />
      <div className="orbit orbit-two" aria-hidden="true" />
      <div className="preview-card">
        <div className="preview-topline">
          <span className="live-dot" />
          <span>OPEN FOR CLAIM</span>
          <span className="issue-number">#248</span>
        </div>
        <div className="repo-line">
          <Code2 aria-hidden="true" />
          <span>acme / open-source-kit</span>
          <ArrowUpRight aria-hidden="true" />
        </div>
        <h2>Fix retry behavior when the request times out</h2>
        <p className="issue-copy">
          Retries currently stop after a transient network error. Add bounded exponential
          backoff and cover it with tests.
        </p>
        <div className="issue-labels">
          <span>bug</span>
          <span>good first issue</span>
          <span>TypeScript</span>
        </div>
        <div className="price-panel">
          <div>
            <span className="price-caption">FIXED REWARD</span>
            <strong>
              120 <small>USDC</small>
            </strong>
          </div>
          <div className="escrow-status">
            <ShieldCheck aria-hidden="true" />
            <span>Backed by escrow</span>
          </div>
        </div>
        <div className="preview-footer">
          <span>
            <Clock3 aria-hidden="true" /> 6 days to claim
          </span>
          <span className="acceptance">
            <Check aria-hidden="true" /> Criteria agreed
          </span>
        </div>
      </div>
      <div className="floating-note note-top">
        <span className="note-icon">
          <Sparkles aria-hidden="true" />
        </span>
        <span>
          <strong>Price, explained</strong>
          <small>Scope-aware estimate</small>
        </span>
      </div>
      <div className="floating-note note-bottom">
        <span className="note-icon note-icon-dark">
          <Wallet aria-hidden="true" />
        </span>
        <span>
          <strong>Paid on acceptance</strong>
          <small>Direct to the contributor</small>
        </span>
      </div>
    </div>
  )
}

/**
 * The way in: the sign-in gate until all three of its steps are done, and straight to
 * the app after (#181). Halfway through it says so, rather than offering the app.
 */
function HeaderAction() {
  const { data: me } = useMe()
  const gate = useGate()
  return gate.finished ? (
    <Link className="nav-cta preview-trigger" to="/overview">
      Open the app <ArrowRight aria-hidden="true" />
    </Link>
  ) : (
    <button
      type="button"
      className="nav-cta preview-trigger"
      aria-haspopup="dialog"
      onClick={gate.open}
    >
      {me ? 'Finish signing in' : 'Sign in'} <ArrowRight aria-hidden="true" />
    </button>
  )
}

/** This deployment's own numbers, and which money they are in. */
function Snapshot() {
  const { data: health } = useHealth()
  const { data: m } = useQuery({
    queryKey: ['metrics'],
    queryFn: async () => (await api.GET('/api/v1/metrics')).data as MetricsOut | undefined,
  })
  // Nothing is claimed about the network until the API has said which it is.
  const kicker = health ? `${health.network_label} · ${moneyWords[health.money]}` : 'Live numbers'

  return (
    <section className="metrics-section" aria-labelledby="metrics-heading">
      <div className="metrics-inner">
        <div className="metrics-heading">
          <span className="section-kicker">{kicker.toUpperCase()}</span>
          <h2 id="metrics-heading">Community snapshot</h2>
        </div>
        <dl className="metrics-grid">
          <div className="metric-item">
            <dt>Funded issues</dt>
            <dd>{m?.funded_issues_published ?? '—'}</dd>
          </div>
          <div className="metric-item">
            <dt>Claimed within 72h</dt>
            <dd>
              {m ? (
                <>
                  {Math.round(m.claim_rate_72h * 100)}
                  <small>%</small>
                </>
              ) : (
                '—'
              )}
            </dd>
          </div>
          <div className="metric-item">
            <dt>Settled issues</dt>
            <dd>{m?.settled_issues ?? '—'}</dd>
          </div>
          <div className="metric-item">
            <dt>Paid out</dt>
            <dd>
              {m ? (
                <>
                  <small>$</small>
                  {compactUsd(m.matched_volume_usdc)}
                </>
              ) : (
                '—'
              )}
            </dd>
          </div>
        </dl>
      </div>
    </section>
  )
}

export default function Landing() {
  return (
    <main id="top" className="landing">
      <header className="site-header">
        <div className="nav-shell">
          <BrandMark />
          <nav className="main-nav" aria-label="Main navigation">
            <a href="#how-it-works">How it works</a>
            <a href="#why-misthos">Why Misthos</a>
            <a href="#current-issues">Current issues</a>
          </nav>
          <HeaderAction />
        </div>
      </header>

      <Snapshot />

      <section className="lp-hero section-shell" aria-labelledby="hero-heading">
        <div className="hero-copy">
          <div className="eyebrow">
            <span /> OPEN SOURCE, BACKED BY REAL BUDGETS
          </div>
          <h1 id="hero-heading">
            Good issues deserve <em>a real price.</em>
          </h1>
          <p className="hero-description">
            Misthos turns GitHub issues into funded work. Maintainers set the terms,
            contributors know what they&apos;ll earn, and agents take care of the busywork in
            between.
          </p>
          <div className="hero-actions">
            <a className="button button-primary" href="#how-it-works">
              See how it works <ArrowRight aria-hidden="true" />
            </a>
            <a className="button button-secondary" href={REPO_URL} target="_blank" rel="noreferrer">
              <Code2 aria-hidden="true" /> View the project
            </a>
          </div>
          <div className="hero-proof">
            <div className="proof-avatars" aria-hidden="true">
              <span>m</span>
              <span>&lt;/&gt;</span>
              <span>◎</span>
            </div>
            <p>Built for the people who keep open source moving.</p>
          </div>
        </div>
        <IssuePreview />
        <div className="hero-bottom-note">
          <ArrowDownRight aria-hidden="true" /> A better way to fund the work you depend on
        </div>
      </section>

      <LandingIssues />

      <section className="trust-strip" aria-label="Platform details">
        <div className="trust-inner">
          <span>BUILT FOR OPEN SOURCE</span>
          <div>
            <ShieldCheck aria-hidden="true" />
            <strong>Escrow-backed</strong>
            <small>Funds stay protected</small>
          </div>
          <div>
            <CircleDollarSign aria-hidden="true" />
            <strong>USDC on Arc</strong>
            <small>Fast, low-cost settlement</small>
          </div>
          <div>
            <GitPullRequest aria-hidden="true" />
            <strong>GitHub-native</strong>
            <small>Issues to merged PRs</small>
          </div>
        </div>
      </section>

      <section className="how-section section-shell" id="how-it-works" aria-labelledby="how-heading">
        <div className="section-heading">
          <span className="section-kicker">FROM BACKLOG TO BREAKTHROUGH</span>
          <h2 id="how-heading">
            One issue. One fair price.
            <br />
            <em>Work that gets done.</em>
          </h2>
          <p>Everything around the work gets simpler. The work stays in the hands of people.</p>
        </div>
        <div className="steps-grid">
          {steps.map(({ number, title, description, icon: Icon }) => (
            <article className="step-card" key={number}>
              <div className="step-meta">
                <span>{number}</span>
                <Icon aria-hidden="true" />
              </div>
              <h3>{title}</h3>
              <p>{description}</p>
              {number !== '03' && (
                <div className="step-connector" aria-hidden="true">
                  <ArrowRight />
                </div>
              )}
            </article>
          ))}
        </div>
      </section>

      <section className="why-section" id="why-misthos" aria-labelledby="why-heading">
        <div className="why-inner section-shell">
          <div className="why-heading">
            <span className="section-kicker">PEOPLE SET THE DIRECTION. AI HANDLES THE BUSYWORK.</span>
            <h2 id="why-heading">
              A clear role
              <br />
              for <em>everyone.</em>
            </h2>
            <p>
              Misthos keeps the important decisions in human hands and lets AI take care of
              the repetitive work in between.
            </p>
          </div>
          <div className="responsibility-grid">
            <article className="responsibility-card human-card">
              <div className="responsibility-card-heading">
                <span className="responsibility-icon">
                  <Check aria-hidden="true" />
                </span>
                <div>
                  <span className="responsibility-label">YOUR CALL</span>
                  <h3>What humans do</h3>
                </div>
              </div>
              <ul>
                <li>
                  <Check aria-hidden="true" /> Approve the price
                </li>
                <li>
                  <Check aria-hidden="true" /> Approve the final outcome
                </li>
              </ul>
              <p>You stay in control of the budget and what counts as done.</p>
            </article>
            <article className="responsibility-card ai-card">
              <div className="responsibility-card-heading">
                <span className="responsibility-icon">
                  <Sparkles aria-hidden="true" />
                </span>
                <div>
                  <span className="responsibility-label">THE BUSYWORK</span>
                  <h3>What AI does for you</h3>
                </div>
              </div>
              <ul>
                <li>
                  <Sparkles aria-hidden="true" /> Reads the issue and code to propose a price
                </li>
                <li>
                  <Sparkles aria-hidden="true" /> Checks submitted work against agreed criteria
                </li>
                <li>
                  <Sparkles aria-hidden="true" /> Shares a clear summary to help you decide
                </li>
              </ul>
              <p>AI makes recommendations; people make the final calls.</p>
            </article>
          </div>
        </div>
      </section>

      <section className="final-cta" id="final-cta">
        <div className="cta-decoration" aria-hidden="true" />
        <div className="final-cta-inner">
          <span className="section-kicker">MAKE THE NEXT FIX POSSIBLE</span>
          <h2>
            Fund the work
            <br />
            the world runs on.
          </h2>
          <p>Bring a good issue. Give it a real reward. Let the community do what it does best.</p>
          <a className="button button-light" href={REPO_URL} target="_blank" rel="noreferrer">
            Explore Misthos on GitHub <ArrowUpRight aria-hidden="true" />
          </a>
        </div>
        <div className="cta-footnote">
          <span>OPEN SOURCE RUNS ON PEOPLE.</span>
          <span>LET&apos;S FUND THE PEOPLE.</span>
        </div>
      </section>

      <footer className="site-footer">
        <BrandMark />
        <p>Make open source work, work.</p>
        <a href={REPO_URL} target="_blank" rel="noreferrer">
          Built in the open <ArrowUpRight aria-hidden="true" />
        </a>
        <span>© 2026 Misthos</span>
      </footer>
    </main>
  )
}
