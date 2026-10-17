import { lazy, Suspense } from 'react'
import { Link, NavLink, Outlet, Route, Routes } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { ArrowRight, ArrowUpRight } from 'lucide-react'
import { api, type IssueOut } from './lib/client'
import { useHealth, useMe } from './lib/session'
import { GateProvider, useGate } from './components/SignInGate'
import Dashboard from './routes/Dashboard'
import Issues from './routes/Issues'
import IssueDetail from './routes/IssueDetail'
import Landing from './routes/Landing'
import Publish from './routes/Publish'
import PublicApi from './routes/PublicApi'

// The profile loads on demand, as the sign-in gate's dialog does (components/SignInGate).
const AccountPage = lazy(() => import('./routes/Account'))

const REPO_URL = 'https://github.com/vanduc2514/hackathon-circle-misthos'

/** Say what the money is, in the words a user would look for. */
const moneyChip: Record<'test' | 'real' | 'unknown', string> = {
  test: 'USDC',
  real: 'real USDC',
  unknown: 'not Arc: treat as real',
}

/** The app's own pages, under one nav. The landing page at `/` has its own. */
function Layout() {
  const { data: health } = useHealth()
  const { data: me } = useMe()
  const gate = useGate()
  const account = me?.account ?? null

  return (
    <div className="shell">
      <nav className="nav">
        <Link to="/" className="brand" aria-label="Misthos home">
          <span className="brand-mark" aria-hidden="true">
            m.
          </span>
          <span>misthos</span>
        </Link>
        <div className="nav-links">
          <NavLink to="/overview" className={({ isActive }) => (isActive ? 'active' : '')}>
            Overview
          </NavLink>
          <NavLink to="/issues" className={({ isActive }) => (isActive ? 'active' : '')}>
            Issues
          </NavLink>
          {account?.role === 'publisher' && (
            <NavLink to="/publish" className={({ isActive }) => (isActive ? 'active' : '')}>
              Publish
            </NavLink>
          )}
          {/* The public API page (#208), not Swagger: anonymous routes only, with prices. */}
          <NavLink to="/public-api" className={({ isActive }) => (isActive ? 'active' : '')}>
            API
          </NavLink>
        </div>
        <div className="nav-right">
          {/* Nothing is claimed about the network until the API has said which it is. */}
          <span className="chip">{health?.network_label ?? '…'}</span>
          {health && (
            <span
              className={
                health.money === 'test' ? 'chip accent' : 'chip warn'
              }
            >
              <i className="dot" />
              {moneyChip[health.money]}
            </span>
          )}
          {/* The way in is the sign-in gate, the same one the landing page opens (#181). */}
          {gate.finished && account ? (
            <Link to="/account" className="chip">
              {`${account.role} · ${account.github_login ?? account.party_id}`}
            </Link>
          ) : (
            <button type="button" className="nav-cta" aria-haspopup="dialog" onClick={gate.open}>
              {me ? 'Finish signing in' : 'Sign in'} <ArrowRight aria-hidden="true" />
            </button>
          )}
        </div>
      </nav>
      <main className="main">
        <Outlet />
      </main>
      <footer className="footer">
        <Link to="/" className="brand" aria-label="Misthos home">
          <span className="brand-mark" aria-hidden="true">
            m.
          </span>
          <span>misthos</span>
        </Link>
        <span className="footer-note">
          Fixed price per issue, settled in USDC on Arc. {health?.money_note ?? ''}
        </span>
        <a href={REPO_URL} target="_blank" rel="noreferrer" className="footer-link">
          Built in the open <ArrowUpRight aria-hidden="true" />
        </a>
        <span>© 2026 Misthos</span>
      </footer>
    </div>
  )
}

function NotFound() {
  const { data: issues } = useQuery({
    queryKey: ['issues'],
    queryFn: async () => (await api.GET('/api/v1/issues')).data as IssueOut[] | undefined,
  })
  return (
    <div className="empty">
      Nothing here. {issues ? `${issues.length} issues are waiting on the Issues page.` : ''}
    </div>
  )
}

export default function App() {
  return (
    <GateProvider>
      <Routes>
        <Route index element={<Landing />} />
        <Route element={<Layout />}>
          <Route path="/overview" element={<Dashboard />} />
          <Route path="/issues" element={<Issues />} />
          <Route path="/issues/:issueId" element={<IssueDetail />} />
          <Route path="/publish" element={<Publish />} />
          <Route path="/public-api" element={<PublicApi />} />
          <Route
            path="/account"
            element={
              <Suspense fallback={<div className="empty">Loading…</div>}>
                <AccountPage />
              </Suspense>
            }
          />
          <Route path="*" element={<NotFound />} />
        </Route>
      </Routes>
    </GateProvider>
  )
}
