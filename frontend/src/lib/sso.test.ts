import { describe, expect, it } from 'vitest'
import { parseDomains, ssoStep } from './sso'
import type { SsoOut } from './client'

const connection = {
  publisher_id: 'PUB-104',
  issuer: 'https://idp.acme.example',
  client_id: 'misthos',
  client_secret_ref: 'acme-oidc',
  domains: ['acme.example'],
  required: false,
  configured_at: '2026-10-10T09:00:00Z',
}

function sso(over: Partial<SsoOut> = {}): SsoOut {
  return { connection: null, callback_url: 'http://localhost:5173/cb', entitled: true, ...over }
}

describe('parseDomains', () => {
  it('reads domains however they were separated, once each, without the @', () => {
    expect(parseDomains(' @Acme.example, labs.acme.example\nacme.example ')).toEqual([
      'acme.example',
      'labs.acme.example',
    ])
  })

  it('reads nothing from blank input', () => {
    expect(parseDomains(' , \n')).toEqual([])
  })
})

describe('ssoStep', () => {
  it('sends an organisation without the plan to upgrade', () => {
    expect(ssoStep(sso({ entitled: false }), { method: 'github' })).toBe('upgrade')
  })

  it('asks an entitled organisation to connect its provider', () => {
    expect(ssoStep(sso(), { method: 'github' })).toBe('connect')
  })

  it('offers the requirement only to a session that came through the sign-on', () => {
    expect(ssoStep(sso({ connection }), { method: 'github' })).toBe('try')
    expect(ssoStep(sso({ connection }), { method: 'sso' })).toBe('require')
  })

  it('says when it is already required, even once the plan lapsed', () => {
    const required = { ...connection, required: true }
    expect(ssoStep(sso({ connection: required, entitled: false }), { method: 'sso' })).toBe(
      'required',
    )
  })
})
