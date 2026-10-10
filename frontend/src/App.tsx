import { lazy, Suspense } from 'react'
import { Link, NavLink, Route, Routes } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { api, type IssueOut } from './lib/client'
import { accountLabel } from './lib/identity'
import { useHealth, useMe } from './lib/session'
import Dashboard from './routes/Dashboard'
import Issues from './routes/Issues'
import IssueDetail from './routes/IssueDetail'
import Loop from './routes/Loop'
import Plans from './routes/Plans'
import Publish from './routes/Publish'
import Spend from './routes/Spend'

// Signing carries the curve arithmetic, so it loads only where someone signs in.
const AccountPage = lazy(() => import('./routes/Account'))

/** Say what the money is, in the words a user would look for. */
const moneyChip: Record<'simulated' | 'test' | 'real' | 'unknown', string> = {
  simulated: 'simulated',
  test: 'test USDC',
  real: 'real USDC',
  unknown: 'not Arc: treat as real',
}

function Layout({ children }: { children: React.ReactNode }) {
  const { data: health } = useHealth()
  const { data: me } = useMe()
  const account = me?.account ?? null

  return (
    <div className="shell">
      <nav className="nav">
        <div className="brand">
          <span className="brand-name">Misthos</span>
          <span className="brand-tag">priced OSS work</span>
        </div>
        <div className="nav-links">
          <NavLink to="/" end className={({ isActive }) => (isActive ? 'active' : '')}>
            Overview
          </NavLink>
          <NavLink to="/issues" className={({ isActive }) => (isActive ? 'active' : '')}>
            Issues
          </NavLink>
          <NavLink to="/loop" className={({ isActive }) => (isActive ? 'active' : '')}>
            Loop
          </NavLink>
          <NavLink to="/spend" className={({ isActive }) => (isActive ? 'active' : '')}>
            Spend
          </NavLink>
          <NavLink to="/plans" className={({ isActive }) => (isActive ? 'active' : '')}>
            Plans
          </NavLink>
          {account?.role === 'publisher' && (
            <NavLink to="/publish" className={({ isActive }) => (isActive ? 'active' : '')}>
              Publish
            </NavLink>
          )}
          <a href="/docs" target="_blank" rel="noreferrer">
            API
          </a>
        </div>
        <div className="nav-right">
          {/* Nothing is claimed about the network until the API has said which it is. */}
          <span className="chip">{health?.network_label ?? '…'}</span>
          {health && (
            <span
              className={
                health.money === 'simulated' || health.money === 'test' ? 'chip accent' : 'chip warn'
              }
            >
              <i className="dot" />
              {moneyChip[health.money]}
            </span>
          )}
          <Link to="/account" className={`chip ${me ? '' : 'accent'}`}>
            {!me
              ? 'Sign in'
              : account
                ? `${account.role} · ${accountLabel(account)}`
                : 'Choose a side'}
          </Link>
        </div>
      </nav>
      <main className="main">{children}</main>
      <footer className="footer">
        <span>
          Misthos &middot; fixed price per issue, settled in USDC on Arc.{' '}
          {health?.money_note ?? ''}
        </span>
        <span>{health ? `${health.seeded_issues} seeded issues` : ''}</span>
      </footer>
    </div>
  )
}

export default function App() {
  const { data: issues } = useQuery({
    queryKey: ['issues'],
    queryFn: async () => (await api.GET('/api/v1/issues')).data as IssueOut[] | undefined,
  })

  return (
    <Layout>
      <Routes>
        <Route path="/" element={<Dashboard />} />
        <Route path="/issues" element={<Issues />} />
        <Route path="/issues/:issueId" element={<IssueDetail />} />
        <Route path="/loop" element={<Loop />} />
        <Route path="/spend" element={<Spend />} />
        <Route path="/publish" element={<Publish />} />
        <Route path="/plans" element={<Plans />} />
        <Route
          path="/account"
          element={
            <Suspense fallback={<div className="empty">Loading…</div>}>
              <AccountPage />
            </Suspense>
          }
        />
        <Route
          path="*"
          element={
            <div className="empty">
              Nothing here. {issues ? `${issues.length} issues are waiting on the Issues page.` : ''}
            </div>
          }
        />
      </Routes>
    </Layout>
  )
}
