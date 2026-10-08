"""Arc: a payment is the USDC `Transfer` event in a transaction receipt.

The publisher pays from their own wallet, so we never hold a key that can move their
money; we only read what the chain says happened. Arc's finality is deterministic,
so a successful receipt is final, and USDC's ERC-20 view at the fixed predeploy
address has six decimals, which is our base unit (see ARCHITECTURE, the decimals
trap).
"""

from __future__ import annotations

import re

import httpx

from misthos.domain.money import Usdc
from misthos.services.billing.base import PaymentError, Received

# keccak256("Transfer(address,address,uint256)")
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
_TX = re.compile(r"^0x[0-9a-fA-F]{64}$")


def _address(topic: str) -> str:
    return "0x" + topic[-40:].lower()


class ArcRail:
    name = "arc"

    def __init__(
        self,
        rpc_url: str,
        usdc_address: str,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 10.0,
    ) -> None:
        self.rpc_url = rpc_url
        self.usdc = usdc_address.lower()
        self._transport = transport
        self._timeout = timeout

    def received(self, tx_hash: str, *, payer: str, payee: str) -> Received | None:
        if not _TX.match(tx_hash):
            raise PaymentError("that is not a transaction hash")
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_getTransactionReceipt",
            "params": [tx_hash],
        }
        try:
            with httpx.Client(transport=self._transport, timeout=self._timeout) as http:
                response = http.post(self.rpc_url, json=body)
                response.raise_for_status()
                answer = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise PaymentError(f"the Arc RPC could not be read: {exc}") from exc
        if answer.get("error"):
            raise PaymentError(f"the Arc RPC refused: {answer['error']}")
        receipt = answer.get("result")
        if not receipt or int(str(receipt.get("status", "0x0")), 16) != 1:
            return None
        total = 0
        for log in receipt.get("logs") or []:
            topics = [str(t).lower() for t in log.get("topics") or []]
            if (
                str(log.get("address", "")).lower() != self.usdc
                or len(topics) != 3
                or topics[0] != TRANSFER_TOPIC
            ):
                continue
            if _address(topics[1]) == payer.lower() and _address(topics[2]) == payee.lower():
                total += int(str(log.get("data", "0x0")), 16)
        if total == 0:
            return None
        return Received(tx_hash=tx_hash, payer=payer, payee=payee, amount=Usdc(total))
