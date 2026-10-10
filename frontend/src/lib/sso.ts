import type { MeOut, SsoOut } from './client'

/** Domains as typed: separated by commas, spaces or new lines, an `@` allowed in front. */
export function parseDomains(text: string): string[] {
  const named = text
    .split(/[\s,]+/)
    .map((d) => d.trim().toLowerCase().replace(/^@/, ''))
    .filter(Boolean)
  return [...new Set(named)]
}

export type SsoStep = 'upgrade' | 'connect' | 'try' | 'require' | 'required'

/**
 * Where an organisation is with its single sign-on (#53), in the order the API lets it
 * go: the plan first, then a connection, then a sign-in through it, which is the only
 * session that may make it required.
 */
export function ssoStep(sso: SsoOut, me: Pick<MeOut, 'method'>): SsoStep {
  const connection = sso.connection
  if (!connection) return sso.entitled ? 'connect' : 'upgrade'
  if (connection.required) return 'required'
  return me.method === 'sso' ? 'require' : 'try'
}
