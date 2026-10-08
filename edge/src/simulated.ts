/**
 * `MISTHOS_SIMULATED`, read once for the whole edge, the way the API reads it.
 *
 * The API's `simulated` is a pydantic bool, and the API and the edge must never run in
 * different modes. The x402 gate and the wallet routes used to read the variable two
 * different ways: with `MISTHOS_SIMULATED=0` the API and the gate ran live while the
 * wallet routes stayed simulated, asked for no core token and handed out made-up
 * addresses, which the live API kept as payout wallets. Both now read it here.
 */

// Exactly what pydantic accepts: these words in any ASCII case, untrimmed. Checked
// against the backend's Settings (pydantic 2.13); backend/tests/unit/test_wallets.py
// pins the same table on that side.
const TRUE_SPELLINGS = ['1', 'true', 't', 'yes', 'y', 'on']
const FALSE_SPELLINGS = ['0', 'false', 'f', 'no', 'n', 'off']

/** Lowercase A-Z only, as pydantic's ASCII-only case folding does. */
function asciiLower(value: string): string {
  return value.replace(/[A-Z]/g, (c) => c.toLowerCase())
}

/** Whether the edge runs the simulation: what `MISTHOS_SIMULATED` says, read the way
 * the backend reads it, and simulated when it is unset, as the backend defaults. A
 * value the backend would refuse is refused here too, at startup, rather than guessed
 * at: a value neither service understands is a deployment mistake, and the safe answer
 * to a mistake about money is to stop. */
export function simulatedFrom(value: string | undefined): boolean {
  if (value === undefined) return true
  const raw = asciiLower(value)
  if (TRUE_SPELLINGS.includes(raw)) return true
  if (FALSE_SPELLINGS.includes(raw)) return false
  throw new Error(
    `MISTHOS_SIMULATED is not a boolean: ${JSON.stringify(value)}. ` +
      'Use true/false, 1/0, yes/no, on/off, t/f or y/n, with no spaces.',
  )
}
