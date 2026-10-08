/** The wallet extension in this browser, such as MetaMask (EIP-1193). */
export type Eip1193 = { request: (args: { method: string; params?: unknown[] }) => Promise<unknown> }

export function browserProvider(): Eip1193 | null {
  const injected = (globalThis as { ethereum?: Eip1193 }).ethereum
  return injected && typeof injected.request === 'function' ? injected : null
}
