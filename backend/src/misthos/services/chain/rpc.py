"""JSON-RPC to Arc, and sending a transaction the platform is allowed to sign.

Reading needs nothing but the RPC. Sending is narrower than it looks: the platform
signs only what the contract lets it do (record a ceiling as owner, release or refund
as attestor). It never signs for a publisher; a commitment moves the publisher's
USDC, so the publisher's own wallet sends it.

Keys arrive through a loader called at signing time, so no key sits in this object,
and a loader that fails (no secret store, nothing at the reference) fails the send
before anything reaches the chain.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

import httpx
from eth_account import Account
from eth_utils import to_checksum_address

from misthos.domain.escrow import revert_name
from misthos.services.chain.base import ChainRevert

# A node asked for too much, too fast, answers 429 and means it. The burst that earns
# it is one scan's windows, so waiting is what makes the next attempt land; the pause
# grows because several calls can be in flight around one scan (#154).
THROTTLED_STATUS = 429
RETRY_AFTER_STATUSES = frozenset({429, 500, 502, 503, 504})
RPC_ATTEMPTS = 3
RPC_BACKOFF_SECONDS = 0.5


class ChainUnavailable(RuntimeError):
    """The RPC could not be read, or the deployment could not be trusted."""


class RpcError(ChainUnavailable):
    def __init__(self, method: str, error: dict) -> None:
        super().__init__(f"the Arc RPC refused {method}: {error}")
        self.error = error


class JsonRpc:
    def __init__(
        self,
        url: str,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 10.0,
    ) -> None:
        self.url = url
        self._transport = transport
        self._timeout = timeout

    def __call__(self, method: str, params: list) -> object:
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        # A read that was refused for being too fast is retried; a read that was refused
        # because it was wrong is not. The distinction is the status: a throttle or a
        # node having a moment is worth waiting out, anything else is the caller's.
        for attempt in range(RPC_ATTEMPTS):
            try:
                with httpx.Client(transport=self._transport, timeout=self._timeout) as http:
                    response = http.post(self.url, json=body)
                    response.raise_for_status()
                    answer = response.json()
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status not in RETRY_AFTER_STATUSES or attempt == RPC_ATTEMPTS - 1:
                    raise ChainUnavailable(f"the Arc RPC could not be read: {exc}") from exc
                time.sleep(self._retry_after(exc.response, attempt))
                continue
            except (httpx.HTTPError, ValueError) as exc:
                raise ChainUnavailable(f"the Arc RPC could not be read: {exc}") from exc
            if answer.get("error"):
                raise RpcError(method, answer["error"])
            return answer.get("result")
        raise ChainUnavailable(f"the Arc RPC could not be read: {method} was retried and refused")

    @staticmethod
    def _retry_after(response: httpx.Response, attempt: int) -> float:
        """How long to wait before trying again: the node's own advice, or our backoff.

        Honoured when a node sends `Retry-After`, because it knows its own limit; capped
        so a hostile or mistaken value cannot hold a request open.
        """
        sent = response.headers.get("retry-after")
        if sent:
            try:
                return min(float(sent), 10.0)
            except ValueError:
                pass
        return RPC_BACKOFF_SECONDS * (2**attempt)


def _revert_data(error: dict) -> str | None:
    data = error.get("data")
    if isinstance(data, dict):  # some nodes nest it
        data = data.get("data")
    return data if isinstance(data, str) else None


class Sender:
    """Signs and sends one kind of transaction with one key, and waits for it.

    Arc's finality is deterministic and sub-second, so a receipt is final; there is no
    confirmation count to wait out and no reorg to handle.
    """

    def __init__(
        self,
        rpc: JsonRpc,
        chain_id: int,
        key: Callable[[], str],
        *,
        receipt_timeout: float = 30.0,
        poll: float = 0.25,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._rpc = rpc
        self._chain_id = chain_id
        self._key = key
        self._receipt_timeout = receipt_timeout
        self._poll = poll
        self._sleep = sleep
        # One nonce sequence per key: two sends from this process must not race.
        self._lock = threading.Lock()

    def send(self, to: str, data: str) -> str:
        """Send `data` to `to`. Returns the hash of a mined, successful transaction.

        A call the contract would refuse is caught by gas estimation and raised as the
        contract's own error, before anything is signed or spent.
        """
        account = Account.from_key(self._key())
        to = to_checksum_address(to)
        call = {"from": account.address, "to": to, "data": data}
        try:
            gas = int(str(self._rpc("eth_estimateGas", [call])), 16)
        except RpcError as exc:
            raise ChainRevert(revert_name(_revert_data(exc.error))) from exc

        with self._lock:
            pending = self._rpc("eth_getTransactionCount", [account.address, "pending"])
            nonce = int(str(pending), 16)
            tip = int(str(self._rpc("eth_maxPriorityFeePerGas", [])), 16)
            base = int(str(self._rpc("eth_gasPrice", [])), 16)
            tx = {
                "type": 2,
                "chainId": self._chain_id,
                "nonce": nonce,
                "to": to,
                "data": data,
                "value": 0,
                # A fifth over the estimate: estimation runs against the current state,
                # and a release that runs out of gas still costs the gas.
                "gas": gas + gas // 5,
                "maxPriorityFeePerGas": tip,
                "maxFeePerGas": 2 * base + tip,
            }
            signed = account.sign_transaction(tx)
            raw = "0x" + bytes(signed.raw_transaction).hex()
            tx_hash = str(self._rpc("eth_sendRawTransaction", [raw]))
        return self._wait(tx_hash)

    def _wait(self, tx_hash: str) -> str:
        waited = 0.0
        while waited <= self._receipt_timeout:
            receipt = self._rpc("eth_getTransactionReceipt", [tx_hash])
            if isinstance(receipt, dict):
                if int(str(receipt.get("status", "0x0")), 16) != 1:
                    raise ChainRevert(f"transaction {tx_hash} reverted on chain")
                return tx_hash
            self._sleep(self._poll)
            waited += self._poll
        raise ChainUnavailable(f"no receipt for {tx_hash} after {self._receipt_timeout:g}s")
