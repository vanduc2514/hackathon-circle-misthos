"""The acceptance attestation key boundary.

Release is the only call that moves committed money, and it needs a signature from a
key the review agent must not be able to reach. If the agent could sign, the per-issue
ceiling would still cap the loss, but nothing would cap the agent. The key therefore
lives in a managed secret store, this module is its only reader, and it is fetched by
reference at signing time rather than sitting in the environment of a process an agent
reads. `assert_key_is_not_in_the_environment` turns the common mistake — exporting the
key and letting settings ignore it — into a refusal to start.

Rotation is an operational step rather than a redeploy, and the procedure with its
verification and rollback is in `docs/runbooks/attestor-rotation.md`.

Nothing in the current build signs yet: the settlement path is `SimulatedChain`, which
attests nothing, so the reader of this key is the Arc client that implements
`services/chain/base.py:ChainGateway` (#69). The seam is deliberate — that client has
one module to call, and no other code path can reach the key.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

# The names a key must never arrive under. Settings ignores unknown variables, so a
# key exported under one of these would sit in the process environment unremarked,
# which is precisely the copy this module exists to prevent.
FORBIDDEN_KEY_ENV_VARS = ("MISTHOS_ATTESTOR_PRIVATE_KEY", "MISTHOS_ATTESTOR_KEY")

KEY_BYTES = 32
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")


class AttestorKeyError(RuntimeError):
    """The key is missing, wrongly sourced, or not key material at all."""


class SecretStore(Protocol):
    """A secret store addressed by reference, so the value never reaches a config file.

    The deployment supplies the managed store; `UnavailableSecretStore` is what the
    offline simulation gets.
    """

    def read(self, reference: str) -> str | None:
        """Return the secret at `reference`, or `None` when the store holds nothing there."""


class UnavailableSecretStore:
    """The offline default. A simulated run releases nothing, so it needs no key."""

    def read(self, reference: str) -> str | None:
        return None


class LocalSecretStore:
    """A mounted directory, for local testing only.

    Not the managed store the runbook names, but the same interface, so the loader
    and its guards are exercised by the tests instead of by a mock. Refuses any file
    another user can read: a key that leaked to a second reader has already failed.
    """

    def __init__(self, directory: str | Path) -> None:
        self._directory = Path(directory)

    def read(self, reference: str) -> str | None:
        """The secret at `reference`, refusing anything another user can read.

        Opened with `O_NOFOLLOW` and then checked and read through the same file
        descriptor. Checking a path and then reading it is two lookups, so a symlink
        dropped at the reference between them would be read after a mode check that
        never saw it — and a symlink is exactly what `_assert_bare_reference` cannot
        refuse, because the reference itself looks bare.
        """
        _assert_bare_reference(reference)
        path = self._directory / reference
        try:
            # Windows has no O_NOFOLLOW; there the reference is read as before.
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise AttestorKeyError(f"{path} cannot be read: {exc.strerror}") from exc
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise AttestorKeyError(f"{path} is not a regular file")
            if info.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
                raise AttestorKeyError(f"{path} is readable by another user; chmod 600 it")
            handle = os.fdopen(fd, encoding="utf-8")
        except BaseException:
            os.close(fd)
            raise
        with handle:
            return handle.read().strip()


@dataclass(frozen=True)
class AttestorKey:
    """A loaded key. Only its reference is printable; the material stays out of logs."""

    reference: str
    material: str = field(repr=False)

    def __str__(self) -> str:
        return f"attestor key from {self.reference}"


def load_attestor_key(store: SecretStore, reference: str) -> AttestorKey:
    """Fetch the attestation key by reference. The only key reader in the process."""
    if not reference:
        raise AttestorKeyError(
            "no attestor secret reference is configured, so nothing can attest acceptance"
        )
    if is_key_material(reference):
        raise AttestorKeyError("the reference is key material; name the secret, not the key")
    material = store.read(reference)
    if material is None:
        raise AttestorKeyError(f"the secret store holds nothing at {reference}")
    if not is_key_material(material):
        raise AttestorKeyError(f"the secret at {reference} is not a 32-byte hex key")
    return AttestorKey(reference=reference, material=material)


def assert_key_is_not_in_the_environment(environment: Mapping[str, str] | None = None) -> None:
    """Refuse to run when key material has been placed in this process's environment."""
    env = os.environ if environment is None else environment
    leaked = sorted(name for name in FORBIDDEN_KEY_ENV_VARS if env.get(name))
    if leaked:
        raise AttestorKeyError(
            f"{', '.join(leaked)} is set; the acceptance attestation key belongs in the "
            "managed secret store, not in the environment of a process an agent can read"
        )


def assert_secrets_are_configured(simulated: bool, reference: str) -> None:
    """Refuse a live deployment that never said where the key lives.

    The simulation signs nothing, so it needs no reference. A deployment that settles
    for real does, and discovering that at the first release is too late.
    """
    if not simulated and not reference:
        raise AttestorKeyError(
            "MISTHOS_SIMULATED is off and MISTHOS_ATTESTOR_SECRET_REF is unset, so no "
            "release could ever be attested"
        )


def is_key_material(value: str) -> bool:
    """True for a 32-byte hex key, with or without the `0x` prefix."""
    body = value[2:] if value.startswith(("0x", "0X")) else value
    return len(body) == KEY_BYTES * 2 and all(character in _HEX_DIGITS for character in body)


def _assert_bare_reference(reference: str) -> None:
    if not reference or reference.startswith(".") or "/" in reference or "\\" in reference:
        raise AttestorKeyError(f"{reference!r} is not a secret reference")
