"""Money types.

Arc exposes USDC through two interfaces over the same balance: a native view with
18 decimals used for gas, and an ERC-20 view with 6 decimals used for everything
else. Mixing them is the most likely way to lose real funds, so amounts carry
their unit in the type rather than in a variable name.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

USDC_DECIMALS = 6
NATIVE_DECIMALS = 18


@dataclass(frozen=True)
class Usdc:
    """An amount in the 6-decimal ERC-20 view. The default unit for this app."""

    base_units: int

    @classmethod
    def from_decimal(cls, value: str | Decimal) -> Usdc:
        scaled = Decimal(value).scaleb(USDC_DECIMALS)
        if scaled != scaled.to_integral_value():
            raise ValueError(f"{value} has more precision than USDC supports")
        return cls(int(scaled))

    @property
    def decimal(self) -> Decimal:
        return Decimal(self.base_units).scaleb(-USDC_DECIMALS)

    def __str__(self) -> str:
        return f"{self.decimal:.2f} USDC"

    def __add__(self, other: Usdc) -> Usdc:
        return Usdc(self.base_units + other.base_units)

    def __sub__(self, other: Usdc) -> Usdc:
        return Usdc(self.base_units - other.base_units)

    def __mul__(self, factor: float | Decimal) -> Usdc:
        return Usdc(int(Decimal(self.base_units) * Decimal(str(factor))))

    def __lt__(self, other: Usdc) -> bool:  # pragma: no cover - trivial
        return self.base_units < other.base_units

    def __le__(self, other: Usdc) -> bool:  # pragma: no cover - trivial
        return self.base_units <= other.base_units


@dataclass(frozen=True)
class NativeUsdc:
    """An amount in the 18-decimal native view. Gas only.

    Deliberately a different type so it cannot be passed where `Usdc` is expected.
    """

    wei: int

    @property
    def decimal(self) -> Decimal:
        return Decimal(self.wei).scaleb(-NATIVE_DECIMALS)


def format_usdc(amount: Usdc) -> str:
    return f"{amount.decimal:,.2f}"
