"""What a single sign-on connection must say, and who it admits (#53), and the session a
sign-on issues."""

from __future__ import annotations

import pytest

from misthos.auth import sessions
from misthos.auth.sessions import Identity
from misthos.domain.sso import SsoRefusal, admitted, check_issuer, domain_of, terms

GOOD = {
    "issuer": "https://idp.acme.example/",
    "client_id": " misthos ",
    "client_secret_ref": "acme-oidc",
    "domains": ["Acme.example", "@acme.example", "labs.acme.example"],
    "required": False,
    "local_ok": False,
}


class TestTheConnection:
    def test_it_is_normalised_once_and_kept_that_way(self) -> None:
        checked = terms(**GOOD)  # type: ignore[arg-type]
        assert checked.issuer == "https://idp.acme.example"
        assert checked.client_id == "misthos"
        assert checked.domains == ("acme.example", "labs.acme.example")

    def test_a_plain_http_issuer_is_refused_except_a_local_one_in_the_simulation(self) -> None:
        with pytest.raises(SsoRefusal, match="https"):
            check_issuer("http://idp.acme.example", local_ok=True)
        with pytest.raises(SsoRefusal, match="https"):
            check_issuer("http://localhost:8080", local_ok=False)
        assert check_issuer("http://localhost:8080/", local_ok=True) == "http://localhost:8080"

    def test_an_issuer_with_a_query_is_not_an_issuer(self) -> None:
        with pytest.raises(SsoRefusal, match="not an issuer URL"):
            check_issuer("https://idp.acme.example/?tenant=1", local_ok=False)

    @pytest.mark.parametrize("domain", ["gmail.com", "Outlook.com", "@proton.me"])
    def test_a_domain_anyone_can_sign_up_on_is_not_an_organisations(self, domain: str) -> None:
        with pytest.raises(SsoRefusal, match="anyone can have an address"):
            terms(**{**GOOD, "domains": [domain]})  # type: ignore[arg-type]

    def test_the_secret_is_named_by_reference_never_given(self) -> None:
        for given in ("", "../secrets/acme", "has space", "a/b"):
            with pytest.raises(SsoRefusal, match="reference"):
                terms(**{**GOOD, "client_secret_ref": given})  # type: ignore[arg-type]

    def test_it_speaks_for_at_least_one_domain_and_at_most_ten(self) -> None:
        with pytest.raises(SsoRefusal, match="at least one"):
            terms(**{**GOOD, "domains": []})  # type: ignore[arg-type]
        many = [f"d{i}.example" for i in range(11)]
        with pytest.raises(SsoRefusal, match="at most 10"):
            terms(**{**GOOD, "domains": many})  # type: ignore[arg-type]


class TestWhoItAdmits:
    def test_a_verified_address_in_a_named_domain_is_admitted_lowercase(self) -> None:
        assert admitted(["acme.example"], "Dana@ACME.example", True) == "dana@acme.example"

    def test_a_subdomain_is_not_its_parent(self) -> None:
        with pytest.raises(SsoRefusal, match="not in a domain"):
            admitted(["acme.example"], "dana@labs.acme.example", True)

    @pytest.mark.parametrize("verified", [False, None, "true", 1])
    def test_only_a_provider_that_says_verified_is_believed(self, verified: object) -> None:
        with pytest.raises(SsoRefusal, match="has not verified"):
            admitted(["acme.example"], "dana@acme.example", verified)

    @pytest.mark.parametrize("email", [None, 42, "no-at-sign", "@acme.example"])
    def test_an_answer_without_an_address_admits_nobody(self, email: object) -> None:
        with pytest.raises(SsoRefusal, match="did not say who you are"):
            admitted(["acme.example"], email, True)

    def test_the_domain_of_an_address(self) -> None:
        assert domain_of(" Dana@Acme.Example ") == "acme.example"
        assert domain_of("dana@") is None and domain_of("dana") is None


class TestTheSession:
    def test_a_sign_on_session_names_the_organisation_and_the_person(self) -> None:
        token = sessions.issue(Identity.sso("PUB-104", "Dana@Acme.example"))
        assert sessions.identity_in(token) == Identity.sso("PUB-104", "dana@acme.example")

    def test_a_sign_on_session_without_its_person_is_no_session(self) -> None:
        import jwt

        claims = {"sub": "sso:PUB-104", "iss": sessions.ISSUER, "exp": 2**40}
        token = jwt.encode(claims, sessions._SECRET, algorithm="HS256")
        assert sessions.identity_in(token) is None
