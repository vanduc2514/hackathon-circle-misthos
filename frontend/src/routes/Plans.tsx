import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { api, money, shortHash, shortTime, unwrap, type ApiError } from '../lib/client'
import { browserProvider } from '../lib/eip1193'
import { payFromWallet } from '../lib/pay'
import { useHealth, useMe } from '../lib/session'
import { Panel } from '../components/ui'

/**
 * The plans and, for a signed-in publisher, buying one. Team is bought here without a
 * conversation: choose it, send USDC on Arc from your wallet, and the API reads the
 * transaction before switching the plan on. Enterprise is agreed with us.
 */
export default function Plans() {
  const qc = useQueryClient()
  const health = useHealth()
  const me = useMe()
  const simulated = health.data?.simulated ?? false
  const account = me.data?.account ?? null
  const pid = account?.role === 'publisher' ? account.party_id : null

  const catalogue = useQuery({
    queryKey: ['plans'],
    queryFn: async () => unwrap(await api.GET('/api/v1/plans')),
  })
  const subscription = useQuery({
    queryKey: ['subscription', pid],
    enabled: Boolean(pid),
    queryFn: async () =>
      unwrap(
        await api.GET('/api/v1/publishers/{publisher_id}/subscription', {
          params: { path: { publisher_id: pid as string } },
        }),
      ),
  })
  const changed = () => {
    qc.invalidateQueries({ queryKey: ['subscription'] })
    qc.invalidateQueries({ queryKey: ['spend'] })
    qc.invalidateQueries({ queryKey: ['publishers'] })
  }
  const path = { params: { path: { publisher_id: pid as string } } }

  const choose = useMutation({
    mutationFn: async (plan: 'open' | 'team') =>
      unwrap(await api.POST('/api/v1/publishers/{publisher_id}/subscription', { ...path, body: { plan } })),
    onSuccess: changed,
  })
  const confirm = useMutation({
    mutationFn: async (txHash: string) => {
      // Arc is final as soon as the transaction is in a block, which can take a moment
      // to reach the RPC, so a payment just sent is asked about a few times.
      for (let attempt = 0; ; attempt++) {
        const result = await api.POST('/api/v1/publishers/{publisher_id}/subscription/payment', {
          ...path,
          body: { tx_hash: txHash },
        })
        if (result.response.status !== 422 || attempt >= 5) return unwrap(result)
        await new Promise((r) => setTimeout(r, 3000))
      }
    },
    onSuccess: changed,
  })
  const walletPay = useMutation({
    mutationFn: async () => {
      const ask = subscription.data?.pending
      if (!ask) throw new Error('Choose a plan first.')
      return confirm.mutateAsync(await payFromWallet(ask))
    },
  })
  const simulatedPay = useMutation({
    mutationFn: async () =>
      unwrap(await api.POST('/api/v1/demo/publishers/{publisher_id}/subscription/pay', path)),
    onSuccess: changed,
  })

  const sub = subscription.data
  const busy = choose.isPending || confirm.isPending || walletPay.isPending || simulatedPay.isPending
  const failure = (choose.error ?? confirm.error ?? walletPay.error ?? simulatedPay.error) as
    | ApiError
    | Error
    | null

  return (
    <>
      <div className="page-head">
        <div>
          <h1 className="page-title">Plans</h1>
          <p className="page-sub">
            Every plan funds issues at a fixed price. The paid plans add the organisation's
            own controls and records, and a thinner take rate. Plans are paid in USDC on Arc,
            from your own wallet, a month at a time.
          </p>
        </div>
      </div>

      {!pid && (
        <div className="banner">
          {account
            ? 'Plans are for publishers. This wallet is a contributor.'
            : 'Sign in as a publisher to choose a plan.'}{' '}
          {!account && (
            <Link to="/account" style={{ textDecoration: 'underline' }}>
              Go to your account
            </Link>
          )}
        </div>
      )}

      {sub && <Standing sub={sub} />}

      <div className="grid k3">
        {catalogue.data?.map((plan) => {
          const current = sub?.plan === plan.id
          return (
            <Panel key={plan.id} title={plan.name}>
              <div className="price-value">
                {plan.price_from && <span className="dim plan-from">from </span>}$
                {money(plan.monthly_usdc)}
                <span className="dim plan-per"> / month</span>
              </div>
              <p className="dim" style={{ margin: '6px 0 12px' }}>
                {plan.audience}
              </p>
              <dl className="kv prose" style={{ marginBottom: 12 }}>
                <dt>Take rate</dt>
                <dd>{plan.take_rate_percent}% of each settled issue</dd>
                <dt>Smallest fix</dt>
                <dd>${money(plan.minimum_fix_usdc)}</dd>
                <dt>Support</dt>
                <dd>{plan.support}</dd>
              </dl>
              <ul className="findings" style={{ marginBottom: 14 }}>
                <li>Fixed-price issues, reviewed and settled in USDC</li>
                {plan.features.map((f) => (
                  <li key={f}>{f}</li>
                ))}
              </ul>
              {current ? (
                <span className="chip accent">your plan</span>
              ) : !pid ? null : plan.id === 'team' ? (
                <button className="btn primary" disabled={busy} onClick={() => choose.mutate('team')}>
                  Choose Team
                </button>
              ) : plan.id === 'open' && sub && sub.plan === 'team' && !sub.cancel_at_period_end ? (
                <button className="btn" disabled={busy} onClick={() => choose.mutate('open')}>
                  Move to Open when this period ends
                </button>
              ) : plan.id === 'enterprise' ? (
                <p className="stat-hint">Agreed with us in a contract, not bought here.</p>
              ) : null}
            </Panel>
          )
        })}
      </div>

      {sub?.pending && (
        <Panel title="Pay for the plan">
          <p className="dim action-hint">
            Send <strong>{money(sub.pending.amount_usdc)} USDC</strong> on {sub.pending.chain}{' '}
            (chain {sub.pending.chain_id}) from the wallet you signed in with and fund issues
            from, <span className="mono-num">{shortHash(sub.pending.payer)}</span>, to{' '}
            <span className="mono-num" title={sub.pending.pay_to}>
              {sub.pending.pay_to}
            </span>
            . This request is open until {shortTime(sub.pending.expires_at)}. We read the
            transaction from the chain before the plan switches on.
          </p>
          <div className="btn-row" style={{ marginBottom: 12 }}>
            {simulated ? (
              <button className="btn primary" disabled={busy} onClick={() => simulatedPay.mutate()}>
                Pay on the simulated rail
              </button>
            ) : (
              <button
                className="btn primary"
                disabled={busy || !browserProvider()}
                onClick={() => walletPay.mutate()}
              >
                {walletPay.isPending ? 'Waiting for the wallet…' : 'Pay from your wallet'}
              </button>
            )}
          </div>
          <ConfirmByHash busy={busy} onConfirm={(tx) => confirm.mutate(tx)} />
        </Panel>
      )}

      {failure && <div className="error-box form-error">{failure.message}</div>}

      {sub?.payments && sub.payments.length > 0 && (
        <Panel title="Payments" flush>
          <table className="table">
            <thead>
              <tr>
                <th>Paid</th>
                <th>Plan</th>
                <th>Period</th>
                <th>Transaction</th>
                <th style={{ textAlign: 'right' }}>Amount</th>
              </tr>
            </thead>
            <tbody>
              {sub.payments.map((p) => (
                <tr key={p.tx_hash}>
                  <td className="dim">{shortTime(p.paid_at)}</td>
                  <td>{p.plan}</td>
                  <td className="dim">
                    {shortTime(p.period_start)} to {shortTime(p.period_end)}
                  </td>
                  <td className="muted">{shortHash(p.tx_hash)}</td>
                  <td className="mono-num" style={{ textAlign: 'right' }}>
                    ${money(p.amount_usdc)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Panel>
      )}
    </>
  )
}

function Standing({
  sub,
}: {
  sub: {
    plan: string
    status: string
    period_end?: string | null
    grace_ends?: string | null
    cancel_at_period_end?: boolean
  }
}) {
  const until = sub.period_end ? shortTime(sub.period_end) : null
  let text: string
  switch (sub.status) {
    case 'active':
      text = sub.cancel_at_period_end
        ? `On ${sub.plan} until ${until}, then Open.`
        : `On ${sub.plan}, paid until ${until}. Renew any time; a payment adds a period.`
      break
    case 'past_due':
      text = `The ${sub.plan} period ended ${until}. It stays on until ${
        sub.grace_ends ? shortTime(sub.grace_ends) : 'the grace period ends'
      } if it is paid.`
      break
    case 'contract':
      text = `On ${sub.plan}, agreed with us.`
      break
    case 'none':
      text = 'On Open: nothing to pay, and fixes priced at the Open take rate.'
      break
    case 'pending':
      text = 'A plan is chosen and waiting for its payment, below.'
      break
    case 'lapsed':
    case 'cancelled':
      text = `Back on Open since ${until}. Your spending policy is still enforced.`
      break
    default:
      text = `On ${sub.plan}.`
  }
  return <div className="banner">{text}</div>
}

function ConfirmByHash({ busy, onConfirm }: { busy: boolean; onConfirm: (tx: string) => void }) {
  const [tx, setTx] = useState('')
  return (
    <form
      className="field-row"
      onSubmit={(e) => {
        e.preventDefault()
        onConfirm(tx.trim())
      }}
    >
      <label className="field grow">
        <span>Paid another way? The transaction hash</span>
        <input
          className="input"
          value={tx}
          onChange={(e) => setTx(e.target.value)}
          pattern="0x[0-9a-fA-F]{64}"
          placeholder="0x…"
          required
        />
      </label>
      <button className="btn align-end" type="submit" disabled={busy || !tx.trim()}>
        Confirm the payment
      </button>
    </form>
  )
}
