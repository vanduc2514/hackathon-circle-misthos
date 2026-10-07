"""The attestation key boundary.

Release is the one call that moves committed money, so where its key lives is a
money path rather than a config detail. These tests hold the two promises the
backlog makes: the key is fetched from a secret store by reference and from
nowhere else, and rotating it is a contract call rather than a redeploy.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from misthos.config import Settings
from misthos.main import create_app
from misthos.services.attestor import (
    FORBIDDEN_KEY_ENV_VARS,
    AttestorKeyError,
    LocalSecretStore,
    UnavailableSecretStore,
    assert_key_is_not_in_the_environment,
    assert_secrets_are_configured,
    is_key_material,
    load_attestor_key,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
ESCROW = REPO_ROOT / "contracts" / "src" / "MisthosEscrow.sol"
RUNBOOK = REPO_ROOT / "docs" / "runbooks" / "attestor-rotation.md"

REFERENCE = "attestor-2026-10"
KEY = "0x" + "ab" * 32


def _mounted_secret(directory: Path, name: str = REFERENCE, value: str = KEY) -> Path:
    path = directory / name
    path.write_text(value + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


class TestReadingTheKey:
    def test_the_offline_default_holds_no_key(self) -> None:
        """The simulation releases nothing, so it must not carry a signer either."""
        with pytest.raises(AttestorKeyError, match=REFERENCE):
            load_attestor_key(UnavailableSecretStore(), REFERENCE)

    def test_the_offline_store_never_returns_material(self) -> None:
        assert UnavailableSecretStore().read(REFERENCE) is None

    def test_the_key_is_read_from_the_store_by_reference(self, tmp_path: Path) -> None:
        _mounted_secret(tmp_path)

        key = load_attestor_key(LocalSecretStore(tmp_path), REFERENCE)

        assert key.reference == REFERENCE
        assert key.material == KEY

    def test_an_unset_reference_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(AttestorKeyError, match="no attestor secret reference"):
            load_attestor_key(LocalSecretStore(tmp_path), "")

    def test_a_key_pasted_into_the_reference_is_refused(self, tmp_path: Path) -> None:
        """The reference names the secret; accepting key material there defeats it."""
        with pytest.raises(AttestorKeyError, match="name the secret"):
            load_attestor_key(LocalSecretStore(tmp_path), KEY)

    def test_a_second_reader_of_the_secret_file_is_refused(self, tmp_path: Path) -> None:
        _mounted_secret(tmp_path).chmod(0o644)

        with pytest.raises(AttestorKeyError, match="readable by another user"):
            load_attestor_key(LocalSecretStore(tmp_path), REFERENCE)

    def test_a_reference_cannot_climb_out_of_the_store(self, tmp_path: Path) -> None:
        with pytest.raises(AttestorKeyError, match="not a secret reference"):
            load_attestor_key(LocalSecretStore(tmp_path), "../outside")

    def test_a_secret_that_is_not_key_material_is_refused(self, tmp_path: Path) -> None:
        _mounted_secret(tmp_path, value="hunter2")

        with pytest.raises(AttestorKeyError, match="not a 32-byte hex key"):
            load_attestor_key(LocalSecretStore(tmp_path), REFERENCE)


class TestTheKeyNeverLeaks:
    def test_the_material_is_not_printable(self, tmp_path: Path) -> None:
        _mounted_secret(tmp_path)
        key = load_attestor_key(LocalSecretStore(tmp_path), REFERENCE)

        assert KEY not in repr(key)
        assert KEY not in str(key)

    def test_a_key_in_the_environment_is_a_refusal_to_start(self) -> None:
        with pytest.raises(AttestorKeyError, match="MISTHOS_ATTESTOR_PRIVATE_KEY"):
            assert_key_is_not_in_the_environment({"MISTHOS_ATTESTOR_PRIVATE_KEY": KEY})

    @pytest.mark.parametrize("name", FORBIDDEN_KEY_ENV_VARS)
    def test_an_empty_variable_is_not_key_material(self, name: str) -> None:
        assert_key_is_not_in_the_environment({name: ""})

    def test_a_clean_environment_passes(self) -> None:
        assert_key_is_not_in_the_environment({"PATH": "/usr/bin"})

    def test_the_api_refuses_to_build_with_a_key_in_the_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MISTHOS_ATTESTOR_PRIVATE_KEY", KEY)

        with pytest.raises(AttestorKeyError):
            create_app()

    def test_settings_carry_a_reference_and_no_key_at_all(self) -> None:
        assert "attestor_secret_ref" in Settings.model_fields
        assert not [name for name in Settings.model_fields if "private" in name]
        assert not [
            name
            for name in Settings.model_fields
            if is_key_material(str(getattr(Settings(), name)))
        ]


class TestRotationIsARunbookStep:
    def test_the_escrow_keeps_the_attestor_in_storage_not_in_code(self) -> None:
        """An immutable attestor would make a rotation a redeploy, which is exactly
        what the runbook promises it is not."""
        source = ESCROW.read_text(encoding="utf-8")

        assert "function setAttestor(address next) external onlyOwner" in source
        assert "address public attestor;" in source
        assert "address public immutable attestor" not in source

    def test_the_runbook_names_the_rotation_call_and_its_verification(self) -> None:
        """The runbook is the procedure, so it has to name what it depends on."""
        runbook = RUNBOOK.read_text(encoding="utf-8")

        assert "setAttestor" in runbook
        assert "cast call" in runbook
        assert "redeploy" in runbook

    def test_the_api_calls_the_environment_guard(self) -> None:
        """A guard nothing calls is documentation, not a control."""
        main = REPO_ROOT / "backend" / "src" / "misthos" / "main.py"

        assert "assert_key_is_not_in_the_environment()" in main.read_text(encoding="utf-8")


class TestARealDeploymentMustNameItsSecret:
    def test_the_simulation_needs_no_reference(self) -> None:
        assert_secrets_are_configured(simulated=True, reference="")

    def test_a_live_deployment_without_a_reference_is_refused(self) -> None:
        """Discovering the missing reference at the first release is too late."""
        with pytest.raises(AttestorKeyError, match="MISTHOS_ATTESTOR_SECRET_REF"):
            assert_secrets_are_configured(simulated=False, reference="")

    def test_a_live_deployment_with_a_reference_passes(self) -> None:
        assert_secrets_are_configured(simulated=False, reference=REFERENCE)

    def test_the_api_checks_it_on_construction(self) -> None:
        main = REPO_ROOT / "backend" / "src" / "misthos" / "main.py"

        assert "assert_secrets_are_configured(" in main.read_text(encoding="utf-8")
