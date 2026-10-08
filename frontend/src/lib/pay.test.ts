import { describe, expect, it } from 'vitest'
import { baseUnits, transferData } from './pay'

describe('paying for a plan', () => {
  it('turns a decimal amount into base units without floating point', () => {
    expect(baseUnits('249.00')).toBe(249_000_000n)
    expect(baseUnits('0.000001')).toBe(1n)
    expect(() => baseUnits('1.0000001')).toThrow()
    expect(() => baseUnits('-1')).toThrow()
  })

  it('encodes an ERC-20 transfer to the platform wallet', () => {
    const data = transferData('0x000000000000000000000000000000000000C0DE', 249_000_000n)
    expect(data.slice(0, 10)).toBe('0xa9059cbb')
    expect(data.length).toBe(10 + 64 + 64)
    expect(data.slice(10, 74)).toBe('000000000000000000000000000000000000000000000000000000000000c0de')
    expect(BigInt('0x' + data.slice(74))).toBe(249_000_000n)
  })
})
