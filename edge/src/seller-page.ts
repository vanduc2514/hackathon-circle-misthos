/**
 * The seller page (#205): what Misthos sells over x402, what each call costs and where
 * it settles, for a person to read before pointing an agent at it.
 *
 * It is the catalog (`GET /paid`) rendered as HTML, so the page and what an agent reads
 * can never disagree about a price. It wears the web app's API page (#208): the same
 * editorial index, grouped by job, with the price as a column.
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
  name?: string
  summary?: string
  group?: string
  returns?: string
  price: { usdc?: string; base_units: string }
  description: string
  free_tier: boolean
  async: boolean
  capped: boolean
  available: boolean
  unavailable?: string
  note?: string
  example?: { params?: Record<string, string>; query?: Record<string, string>; body?: unknown }
}

interface Catalog {
  networks: string[]
  payTo: string
  accepts: { network: string; asset: string }[]
  certificate_signer?: string | null
  routes: CatalogRoute[]
}

const GROUPS = ['Core', 'Marketplace', 'Analytics', 'Trust']
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

const shownPath = (path: string) => path.replace(/:(\w+)/g, '{$1}')

function row(route: CatalogRoute): string {
  const price = route.available
    ? `<span class="api-pay"><span class="api-x402">x402</span><b>${escape(route.price.usdc)}</b> USDC</span>`
    : '<span class="api-off">Off</span>'
  const notes = [
    route.capped ? 'The price is the cap, charged exact: Gateway has no upto.' : '',
    route.async ? 'Runs as a job: 202 and a receipt, settled when it finishes.' : '',
    route.free_tier ? 'Free to its owner, signed in.' : '',
    route.note ?? '',
    route.unavailable ?? '',
  ].filter(Boolean)
  return `
      <details class="api-row paid${route.available ? '' : ' off'}">
        <summary>
          <span class="api-method">${escape(route.method)}</span>
          <span class="api-endpoint">
            <code>${escape(shownPath(route.path))}</code>
            <span class="api-line"><b>${escape(route.name ?? route.id)}</b> — ${escape(route.summary ?? '')}</span>
          </span>
          <span class="api-returns">${escape(route.returns ?? '')}</span>
          ${price}
          <span class="api-arrow" aria-hidden="true">→</span>
        </summary>
        <div class="api-more">
          <p>${escape(route.description)}</p>
          ${notes.map((n) => `<p class="api-note">${escape(n)}</p>`).join('')}
          <pre>${escape(exampleCall(route))}</pre>
        </div>
      </details>`
}

export function sellerPage(catalog: Catalog): string {
  const accept = catalog.accepts[0]
  const network = accept?.network ?? catalog.networks[0] ?? ''
  const groups = GROUPS.map((group) => {
    const rows = catalog.routes.filter((r) => (r.group ?? 'Analytics') === group)
    if (!rows.length) return ''
    return `
    <section class="api-group">
      <div class="api-group-head"><h2>${group}</h2><span>${rows.length} ${rows.length === 1 ? 'endpoint' : 'endpoints'}</span></div>
      <div class="api-table">${rows.map(row).join('')}</div>
    </section>`
  }).join('')
  const open = catalog.routes.filter((r) => r.available).length
  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Misthos paid API</title>
<style>
  :root {
    --api-bg: #f7f5ef; --api-card: #fffefa; --api-ink: #17271f; --api-green: #224735;
    --api-gold: #a97b3f; --api-gold-soft: #f3e8d4; --api-line: #e2dfd4; --api-line-soft: #ece9df;
    --api-muted: #5f6b63; --api-faint: #6f7a72;
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--api-bg); color: var(--api-ink); font: 14px/1.5 Arial, Helvetica, sans-serif; }
  header { display: flex; align-items: center; gap: 12px; padding: 18px 32px; background: #fff; border-bottom: 1px solid var(--api-line); }
  .mark { display: inline-grid; place-items: center; width: 32px; height: 32px; border-radius: 50%; background: #111; color: #fff; font: 700 15px Georgia, serif; }
  .brand { font: 700 20px Arial, sans-serif; letter-spacing: -0.03em; }
  main { max-width: 1180px; margin: 0 auto; padding: 0 32px 88px; }
  .api-toolbar { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 16px 24px; padding: 34px 0 20px; border-bottom: 1px solid var(--api-line); }
  .api-tab { display: inline-flex; align-items: center; gap: 7px; padding: 6px 12px; border: 1px solid #d7d4c8; border-radius: 999px; background: var(--api-card); font-size: 12px; font-weight: 600; }
  .api-tab span { color: var(--api-gold); font-size: 10px; }
  .api-base { display: flex; align-items: center; gap: 9px; color: var(--api-faint); font-size: 10px; font-weight: 700; letter-spacing: 0.12em; text-transform: uppercase; }
  .api-base code { padding: 3px 8px; border: 1px solid var(--api-line); border-radius: 2px; background: var(--api-card); color: #4c5c51; font: 11px ui-monospace, Menlo, Consolas, monospace; letter-spacing: 0; text-transform: none; overflow-wrap: anywhere; }
  .api-intro { margin: 18px 0 0; color: var(--api-muted); font-size: 13px; max-width: 760px; }
  .api-intro code { font: 12px ui-monospace, Menlo, Consolas, monospace; overflow-wrap: anywhere; }
  .api-group { margin-top: 34px; }
  .api-group-head { display: flex; align-items: baseline; justify-content: space-between; padding: 0 2px 10px; }
  .api-group-head h2 { margin: 0; font: 400 22px Georgia, 'Times New Roman', serif; letter-spacing: -0.03em; }
  .api-group-head span { color: var(--api-faint); font-size: 10px; font-weight: 700; letter-spacing: 0.1em; text-transform: uppercase; }
  .api-table { overflow: hidden; border: 1px solid var(--api-line); border-radius: 3px; background: var(--api-card); }
  .api-row { border-bottom: 1px solid var(--api-line-soft); }
  .api-row:last-child { border-bottom: 0; }
  .api-row.paid { box-shadow: inset 2px 0 0 var(--api-gold); }
  .api-row.off { opacity: 0.6; }
  .api-row summary { display: grid; grid-template-columns: 48px minmax(0, 1fr) 120px 120px 16px; gap: 12px; align-items: center; min-height: 74px; padding: 13px 15px; cursor: pointer; list-style: none; }
  .api-row summary::-webkit-details-marker { display: none; }
  .api-row summary:hover { background: #fbfaf5; }
  .api-method { display: inline-flex; justify-content: center; width: 44px; padding: 4px 0; border: 1px solid #cfdccf; border-radius: 2px; background: #f0f5ee; color: var(--api-green); font: 700 10px ui-monospace, Menlo, Consolas, monospace; letter-spacing: 0.04em; }
  .api-endpoint { min-width: 0; }
  .api-endpoint code { display: block; overflow: hidden; font: 12.5px ui-monospace, Menlo, Consolas, monospace; white-space: nowrap; text-overflow: ellipsis; }
  .api-line { display: block; margin-top: 5px; color: var(--api-muted); font-size: 12px; line-height: 1.45; }
  .api-line b { color: #3f4f44; font-weight: 600; }
  .api-returns { overflow: hidden; color: var(--api-faint); font: 10px ui-monospace, Menlo, Consolas, monospace; white-space: nowrap; text-overflow: ellipsis; }
  .api-pay { display: inline-flex; align-items: center; gap: 5px; color: #6d5836; font-size: 11px; font-weight: 600; white-space: nowrap; }
  .api-pay b { font-size: 12px; }
  .api-x402 { padding: 2px 5px; border-radius: 2px; background: var(--api-gold-soft); color: #7d561e; font-size: 9px; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase; }
  .api-off { color: var(--api-faint); font-size: 10px; font-weight: 700; letter-spacing: 0.1em; text-transform: uppercase; }
  .api-arrow { color: #8f9891; transition: transform 0.15s; }
  .api-row[open] .api-arrow { transform: rotate(90deg); }
  .api-more { padding: 0 15px 16px 75px; color: var(--api-muted); font-size: 12.5px; }
  .api-more p { margin: 0 0 8px; max-width: 760px; }
  .api-note { color: #6d5836; }
  .api-more pre { margin: 10px 0 0; padding: 12px; overflow-x: auto; border: 1px solid var(--api-line); background: #fbfaf5; color: var(--api-ink); font: 12px ui-monospace, Menlo, Consolas, monospace; }
  @media (max-width: 760px) {
    header { padding: 14px 16px; }
    main { padding: 0 16px 64px; }
    .api-row summary { grid-template-columns: 44px minmax(0, 1fr) 92px; }
    .api-returns, .api-arrow { display: none; }
    .api-more { padding: 0 15px 16px; }
  }
</style>
</head>
<body>
<header><span class="mark" aria-hidden="true">m.</span><span class="brand">misthos</span></header>
<main>
  <div class="api-toolbar">
    <span class="api-tab">x402 <span>${open}</span></span>
    <div class="api-base"><span>Network</span><code>${escape(NETWORK_NAMES[network] ?? network)}</code></div>
  </div>
  <p class="api-intro">Paid per call over x402, through Circle Gateway Nanopayments: no gas for the buyer, and a
  call that fails is not charged. USDC <code>${escape(accept?.asset ?? '')}</code>, paid to
  <code>${escape(catalog.payTo)}</code>${
    catalog.certificate_signer
      ? `; certificates signed by <code>${escape(catalog.certificate_signer)}</code>`
      : ''
  }. Unpaid, every route answers <code>402</code> with <code>PAYMENT-REQUIRED</code>; sign it and send it back as
  <code>PAYMENT-SIGNATURE</code>.</p>
  ${groups}
</main>
</body>
</html>`
}
