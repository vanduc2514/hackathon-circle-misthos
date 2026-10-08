import { useState } from 'react'
import type { EIP1193Provider } from 'viem'
import {
  SOURCE_CHAINS,
  bridgeToArc,
  checkBridge,
  resumeBridge,
  type BridgeOutcome,
  type SourceChain,
} from '../lib/bridge'
import { Panel } from './ui'

function browserWallet(): EIP1193Provider | null {
  const injected = (window as unknown as { ethereum?: EIP1193Provider }).ethereum
  return injected ?? null
}

/**
 * Fund an issue from USDC held on another chain. The publisher's own wallet signs
 * the source-chain steps; the click on the enabled button is the confirmation.
 */
export function BridgeToArc({ suggested }: { suggested?: string }) {
  const [source, setSource] = useState<SourceChain>('Base_Sepolia')
  const [amount, setAmount] = useState(suggested ?? '')
  const [running, setRunning] = useState(false)
  const [outcome, setOutcome] = useState<BridgeOutcome | null>(null)
  const [error, setError] = useState<string | null>(null)

  const wallet = browserWallet()
  const check = checkBridge(source, amount)
  const label = SOURCE_CHAINS.find((c) => c.key === source)?.label ?? source
  const stopped = outcome !== null && outcome.state !== 'success'

  const run = async (step: () => Promise<BridgeOutcome>) => {
    setRunning(true)
    setError(null)
    try {
      setOutcome(await step())
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setRunning(false)
    }
  }

  const start = () => {
    if (!wallet || !check.ok) return
    setOutcome(null)
    void run(() => bridgeToArc(wallet, source, check.amount))
  }

  const resume = () => {
    if (!wallet || !outcome) return
    void run(() => resumeBridge(wallet, outcome))
  }

  return (
    <Panel title="Fund from another chain">
      <p className="dim" style={{ marginBottom: 12 }}>
        Move USDC you hold elsewhere to the same address on Arc over CCTP. Your wallet signs
        on the source chain; Circle's forwarder mints on Arc, so you need no USDC there for
        gas. Its fee comes out of the amount. The platform never holds the USDC.
      </p>
      <div className="field-row">
        <label className="field grow">
          From
          <select
            className="input"
            value={source}
            onChange={(e) => setSource(e.target.value as SourceChain)}
            disabled={running || stopped}
          >
            {SOURCE_CHAINS.map((c) => (
              <option key={c.key} value={c.key}>
                {c.label}
              </option>
            ))}
          </select>
        </label>
        <label className="field grow">
          USDC
          <input
            className="input mono-num"
            inputMode="decimal"
            placeholder="0.00"
            value={amount}
            onChange={(e) => setAmount(e.target.value)}
            disabled={running || stopped}
          />
        </label>
      </div>
      {check.ok ? (
        <p className="stat-hint" style={{ margin: '10px 0' }}>
          {check.amount} USDC from {label} to Arc testnet.
          {check.large && ' That is over 100 USDC: check the amount before you sign.'}
        </p>
      ) : (
        amount && (
          <p className="stat-hint" style={{ margin: '10px 0' }}>
            {check.reason}
          </p>
        )
      )}
      {stopped ? (
        <button className="btn primary" onClick={resume} disabled={!wallet || running}>
          {running ? 'Resuming…' : 'Resume the bridge'}
        </button>
      ) : (
        <button className="btn primary" onClick={start} disabled={!wallet || !check.ok || running}>
          {running ? 'Bridging…' : wallet ? 'Bridge to Arc' : 'No browser wallet found'}
        </button>
      )}
      {outcome && (
        <dl className="kv" style={{ marginTop: 12 }}>
          {outcome.steps.map((s) => (
            <div key={s.name} style={{ display: 'contents' }}>
              <dt>{s.name}</dt>
              <dd>
                {s.explorerUrl ? (
                  <a href={s.explorerUrl} target="_blank" rel="noreferrer">
                    {s.state}
                  </a>
                ) : (
                  s.state
                )}
              </dd>
            </div>
          ))}
        </dl>
      )}
      {stopped && (
        <div className="error-box" style={{ marginTop: 12 }}>
          The bridge stopped part way. Resume it rather than starting again: resuming picks up
          from the step that failed, and starting again could burn the USDC a second time.
        </div>
      )}
      {error && (
        <div className="error-box" style={{ marginTop: 12 }}>
          {error.slice(0, 200)}
        </div>
      )}
    </Panel>
  )
}
