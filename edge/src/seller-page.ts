/**
 * The seller page (#205): what Misthos sells over x402, what each call costs, where it
 * settles, and a call that works, for a person to read before pointing an agent at it.
 *
 * It is the catalog (`GET /paid`) rendered as HTML, so the page and what an agent reads
 * can never disagree about a price. Served when the request asks for HTML.
 */

const escape = (value: unknown): string =>
  String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;')

interface CatalogRoute {
  id: string
  method: string
  path: string
  price: { usdc?: string; base_units: string }
  description: string
  free_tier: boolean
  async: boolean
  capped: boolean
  available: boolean
  unavailable?: string
  note?: string
  poll?: string
  example?: { params?: Record<string, string>; query?: Record<string, string>; body?: unknown }
}

interface Catalog {
  networks: string[]
  payTo: string
  accepts: { network: string; asset: string }[]
  certificate_signer?: string | null
  routes: CatalogRoute[]
}

const NETWORK_NAMES: Record<string, string> = {
  'eip155:5042002': 'Arc testnet',
  'eip155:5042': 'Arc',
}

function exampleCall(route: CatalogRoute): string {
  let path = route.path
  for (const [name, value] of Object.entries(route.example?.params ?? {})) {
    path = path.replace(`:${name}`, encodeURIComponent(value))
  }
  const query = new URLSearchParams(route.example?.query ?? {}).toString()
  const url = `$MISTHOS_PAID_URL${path}${query ? `?${query}` : ''}`
  if (route.method === 'GET') return `curl -i '${url}'`
  return `curl -i -X POST '${url}' \\\n  -H 'Content-Type: application/json' \\\n  -d '${JSON.stringify(route.example?.body ?? {})}'`
}

export function sellerPage(catalog: Catalog): string {
  const networks = catalog.accepts.length
    ? catalog.accepts
        .map((a) => `${escape(NETWORK_NAMES[a.network] ?? a.network)} (<code>${escape(a.network)}</code>), USDC <code>${escape(a.asset)}</code>`)
        .join('<br>')
    : catalog.networks.map((n) => `<code>${escape(n)}</code>`).join(', ')
  const rows = catalog.routes
    .map(
      (r) => `
      <tr${r.available ? '' : ' class="off"'}>
        <td><code>${escape(r.method)} ${escape(r.path)}</code><div class="id">${escape(r.id)}</div></td>
        <td class="price">${r.available ? `$${escape(r.price.usdc)}` : 'off'}${r.capped ? '<div class="id">the cap, exact</div>' : ''}</td>
        <td>${escape(r.description)}${r.note ? `<p class="note">${escape(r.note)}</p>` : ''}${
          r.unavailable ? `<p class="note">${escape(r.unavailable)}</p>` : ''
        }${r.free_tier ? '<p class="note">Free to its owner, signed in.</p>' : ''}</td>
      </tr>`,
    )
    .join('')
  const example = catalog.routes.find((r) => r.available && r.method === 'GET') ?? catalog.routes[0]
  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Misthos paid API</title>
<style>
  :root { --ink: #111; --muted: #555; --line: #ddd; --bg: #fff; --code: #f4f4f4; }
  @media (prefers-color-scheme: dark) { :root { --ink: #eee; --muted: #aaa; --line: #333; --bg: #111; --code: #1c1c1c; } }
  body { margin: 0; background: var(--bg); color: var(--ink); font: 16px/1.5 Arial, sans-serif; }
  main { max-width: 960px; margin: 0 auto; padding: 32px 16px; }
  h1, h2 { font-family: Georgia, serif; font-weight: normal; }
  code, pre { font-family: ui-monospace, Menlo, monospace; font-size: 14px; background: var(--code); }
  code { padding: 1px 4px; overflow-wrap: anywhere; }
  pre { padding: 12px; overflow-x: auto; }
  table { width: 100%; border-collapse: collapse; }
  .scroll { overflow-x: auto; }
  th, td { text-align: left; vertical-align: top; padding: 10px 8px; border-bottom: 1px solid var(--line); }
  .price { white-space: nowrap; font-family: Georgia, serif; }
  .id, .note { color: var(--muted); font-size: 14px; margin: 4px 0 0; }
  .off { opacity: 0.6; }
</style>
</head>
<body>
<main>
<h1>Misthos paid API</h1>
<p>Read-only views of what Misthos knows, sold one call at a time over <a href="https://x402.org">x402</a>
and settled through Circle Gateway Nanopayments: the buyer pays no gas, and a call that fails is not charged.</p>
<h2>How to pay</h2>
<p>${networks}<br>Paid to <code>${escape(catalog.payTo)}</code>. Scheme <code>exact</code>, EOA signatures only.</p>
${catalog.certificate_signer ? `<p>Certificates and receipts are signed (EIP-191) by <code>${escape(catalog.certificate_signer)}</code>.</p>` : ''}
<h2>What is for sale</h2>
<div class="scroll"><table>
  <thead><tr><th>Route</th><th>Price</th><th>What it answers</th></tr></thead>
  <tbody>${rows}</tbody>
</table></div>
<h2>A call</h2>
<p>Unpaid, every route answers <code>402</code> with a <code>PAYMENT-REQUIRED</code> header. Sign it, send it back as
<code>PAYMENT-SIGNATURE</code>, and the answer comes with <code>PAYMENT-RESPONSE</code>.</p>
${example ? `<pre>${escape(exampleCall(example))}</pre>` : ''}
<p>The same list as JSON: <code>GET /paid</code> with <code>Accept: application/json</code>. An MCP server that pays for
you, with a spend cap, is in the repository's <code>mcp/</code> folder; how to fund a buyer wallet on Arc testnet is in
<code>docs/BUYING.md</code>.</p>
</main>
</body>
</html>`
}
