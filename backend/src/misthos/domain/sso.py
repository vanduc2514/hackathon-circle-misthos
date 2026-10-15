"""Single sign-on for an Enterprise organisation (#53): what a connection to its identity
provider must say, and who that provider may sign in.

An organisation on Enterprise answers to its own identity provider for who works
there. Its staff sign in through it with OpenID Connect, and act as the organisation's
account, so the person who leaves the company loses the money controls the day the
provider turns them off, not the day someone remembers to tell us. The provider is the
organisation's, so it can assert any e-mail address it likes; the connection therefore
names the e-mail domains it speaks for, and a sign-in is admitted only for a verified
address in one of them. A domain anyone can register an address on is not an
organisation's, and is refused.

The client secret never enters the record. The connection names it by reference in the
secret store, as the attestor key is named (services/attestor.py), and Enterprise is
agreed with us, so the secret is put there during onboarding.

`required` is the organisation's choice that its account acts only through single
sign-on. It is a protection the organisation set, like its spending policy, so it is
not switched off when a plan lapses.

Pure values and checks; the store keeps the connection, services/sso.py speaks OIDC.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import urlsplit

MAX_DOMAINS = 10

# Addresses anyone can sign up for. A provider claiming one of these would let its
# owner speak for every user of a public mail service.
PUBLIC_MAIL_DOMAINS = frozenset(
    {
        "aol.com",
        "gmail.com",
        "gmx.com",
        "googlemail.com",
        "hotmail.com",
        "icloud.com",
        "live.com",
        "mail.com",
        "me.com",
        "outlook.com",
        "proton.me",
        "protonmail.com",
        "yahoo.com",
        "yandex.com",
    }
)

_DOMAIN = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_REFERENCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class SsoRefusal(Exception):
    """The connection cannot work as given, or the provider's answer is not admitted."""


@dataclass(frozen=True)
class SsoTerms:
    """A connection to an organisation's identity provider, checked and normalised."""

    issuer: str
    """The provider's issuer URL, without a trailing slash: OIDC discovery is read from
    `{issuer}/.well-known/openid-configuration`, and the ID token must name it exactly."""
    client_id: str
    client_secret_ref: str
    """The client secret's name in the secret store, never the secret."""
    domains: tuple[str, ...]
    required: bool = False


def normalise_domain(value: str) -> str:
    domain = value.strip().lower().removeprefix("@")
    if not _DOMAIN.match(domain):
        raise SsoRefusal(f"{value!r} is not an e-mail domain")
    if domain in PUBLIC_MAIL_DOMAINS:
        raise SsoRefusal(f"anyone can have an address at {domain}; name your own domain")
    return domain


def check_issuer(issuer: str, *, local_ok: bool) -> str:
    """The issuer, normalised. HTTPS only: the ID token's keys are fetched from it, and
    anyone on the path of a plain-HTTP fetch could sign for the organisation. A local
    provider on plain HTTP is accepted only where `local_ok` says so (the simulation)."""
    issuer = issuer.strip().rstrip("/")
    parts = urlsplit(issuer)
    if parts.query or parts.fragment or not parts.hostname:
        raise SsoRefusal(f"{issuer!r} is not an issuer URL")
    if parts.scheme == "https":
        return issuer
    if parts.scheme == "http" and local_ok and parts.hostname in _LOCAL_HOSTS:
        return issuer
    raise SsoRefusal(f"the issuer must be an https URL, not {issuer!r}")


def terms(
    *,
    issuer: str,
    client_id: str,
    client_secret_ref: str,
    domains: Iterable[str],
    required: bool,
    local_ok: bool,
) -> SsoTerms:
    client_id = client_id.strip()
    if not client_id or len(client_id) > 256:
        raise SsoRefusal("the client id is the one your identity provider issued for Misthos")
    if not _REFERENCE.match(client_secret_ref):
        raise SsoRefusal(
            "name the client secret by its reference in the secret store, not the secret"
        )
    named = tuple(dict.fromkeys(normalise_domain(d) for d in domains))
    if not named:
        raise SsoRefusal("name at least one e-mail domain your identity provider speaks for")
    if len(named) > MAX_DOMAINS:
        raise SsoRefusal(f"at most {MAX_DOMAINS} domains")
    return SsoTerms(
        issuer=check_issuer(issuer, local_ok=local_ok),
        client_id=client_id,
        client_secret_ref=client_secret_ref,
        domains=named,
        required=required,
    )


def domain_of(email: str) -> str | None:
    local, at, domain = email.strip().lower().rpartition("@")
    return domain if at and local and _DOMAIN.match(domain) else None


def admitted(domains: Iterable[str], email: object, verified: object) -> str:
    """The address a provider asserted, lowercase, if the connection admits it.

    The provider must say the address is verified: an unverified one is whatever the
    person typed into their profile. And it must be in a domain the connection names.
    """
    if not isinstance(email, str) or domain_of(email) is None:
        raise SsoRefusal("your identity provider did not say who you are by e-mail")
    if verified is not True:
        raise SsoRefusal(f"your identity provider has not verified {email}")
    address = email.strip().lower()
    if domain_of(address) not in set(domains):
        raise SsoRefusal(f"{address} is not in a domain this organisation's sign-on speaks for")
    return address
