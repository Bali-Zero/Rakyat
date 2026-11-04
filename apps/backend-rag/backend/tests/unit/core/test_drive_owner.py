"""BACKUP_DRIVE_OWNER_EMAIL: the Drive-account guard of the backup jobs fails closed (synthetic identities only)."""

import pytest

from backend.core import drive_owner
from backend.services.backup import backup_roots_bootstrap as bootstrap
from backend.services.backup import nlm_drive_backup as nlm

OWNER = "drive-owner@example.test"
OTHER = "someone-else@example.test"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    monkeypatch.delenv(drive_owner.ENV_NAME, raising=False)
    monkeypatch.setenv(drive_owner.ENV_FILE, str(tmp_path / "absent.env"))


def test_unset_and_blank_mean_not_configured(monkeypatch):
    assert drive_owner.expected_drive_owner_email() is None
    monkeypatch.setenv(drive_owner.ENV_NAME, "   ")
    assert drive_owner.expected_drive_owner_email() is None


@pytest.mark.parametrize("email", [OWNER, OTHER, "", None, 42])
def test_nothing_matches_when_not_configured(email):
    assert drive_owner.drive_owner_matches(email) is False


def test_match_is_exact_modulo_case_and_whitespace(monkeypatch):
    monkeypatch.setenv(drive_owner.ENV_NAME, f" {OWNER.upper()} ")
    assert drive_owner.drive_owner_matches(OWNER) is True
    assert drive_owner.drive_owner_matches(f"{OWNER} ") is True
    assert drive_owner.drive_owner_matches(OTHER) is False
    assert drive_owner.drive_owner_matches("") is False
    assert drive_owner.drive_owner_matches(None) is False


def _about(email):
    return {"user": {"emailAddress": email}}


@pytest.fixture
def stub_about(monkeypatch):
    def install(email):
        monkeypatch.setattr(nlm, "_drive_get", lambda *a, **k: _about(email))
        monkeypatch.setattr(bootstrap, "_drive_api_call", lambda *a, **k: _about(email))

    return install


@pytest.mark.parametrize(
    ("verify", "error"),
    [
        (nlm._verify_account, nlm.BackupError),
        (bootstrap._verify_account, bootstrap.DriveSetupError),
    ],
)
class TestVerifyAccount:
    def test_matching_account_passes(self, monkeypatch, stub_about, verify, error):
        monkeypatch.setenv(drive_owner.ENV_NAME, OWNER)
        stub_about(OWNER)
        assert verify("token")["user"]["emailAddress"] == OWNER

    def test_other_account_refused_without_echoing_addresses(
        self, monkeypatch, stub_about, verify, error
    ):
        monkeypatch.setenv(drive_owner.ENV_NAME, OWNER)
        stub_about(OTHER)
        with pytest.raises(error) as exc:
            verify("token")
        assert OTHER not in str(exc.value)
        assert OWNER not in str(exc.value)

    @pytest.mark.parametrize("returned", [OWNER, "", None])
    def test_unconfigured_refuses_everything(self, stub_about, verify, error, returned):
        stub_about(returned)
        with pytest.raises(error):
            verify("token")


class TestOwnerFile:
    def _write(self, tmp_path, monkeypatch, text, mode=0o600):
        path = tmp_path / "owner.env"
        path.write_text(text, encoding="utf-8")
        path.chmod(mode)
        monkeypatch.setenv(drive_owner.ENV_FILE, str(path))
        return path

    def test_file_only_works(self, tmp_path, monkeypatch):
        self._write(tmp_path, monkeypatch, f"  {OWNER}  \n\n")
        assert drive_owner.expected_drive_owner_email() == OWNER
        assert drive_owner.drive_owner_matches(OWNER) is True

    def test_env_wins_over_file(self, tmp_path, monkeypatch):
        self._write(tmp_path, monkeypatch, OTHER)
        monkeypatch.setenv(drive_owner.ENV_NAME, OWNER)
        assert drive_owner.expected_drive_owner_email() == OWNER

    def test_missing_file_refuses(self):
        assert drive_owner.expected_drive_owner_email() is None

    @pytest.mark.parametrize("text", ["", "\n  \n", f"{OWNER}\n{OTHER}\n"])
    def test_empty_or_multi_line_refuses(self, tmp_path, monkeypatch, text):
        self._write(tmp_path, monkeypatch, text)
        assert drive_owner.expected_drive_owner_email() is None

    @pytest.mark.parametrize("mode", [0o644, 0o640, 0o400, 0o660])
    def test_wrong_mode_refuses(self, tmp_path, monkeypatch, mode):
        self._write(tmp_path, monkeypatch, OWNER, mode=mode)
        assert drive_owner.expected_drive_owner_email() is None

    def test_symlink_refuses(self, tmp_path, monkeypatch):
        real = self._write(tmp_path, monkeypatch, OWNER)
        link = tmp_path / "link.env"
        link.symlink_to(real)
        monkeypatch.setenv(drive_owner.ENV_FILE, str(link))
        assert drive_owner.expected_drive_owner_email() is None


class TestHardening:
    def test_file_is_opened_nofollow_and_validated_on_the_descriptor(self, tmp_path, monkeypatch):
        path = tmp_path / "owner.env"
        path.write_text(OWNER, encoding="utf-8")
        path.chmod(0o600)
        monkeypatch.setenv(drive_owner.ENV_FILE, str(path))

        def boom(*args, **kwargs):
            raise AssertionError("path-based stat/read re-opens the check-then-use window")

        monkeypatch.setattr("pathlib.Path.lstat", boom)
        monkeypatch.setattr("pathlib.Path.read_text", boom)
        assert drive_owner.expected_drive_owner_email() == OWNER

    def test_bom_prefixed_file_is_read(self, tmp_path, monkeypatch):
        path = tmp_path / "owner.env"
        path.write_bytes(b"\xef\xbb\xbf" + OWNER.encode() + b"\n")
        path.chmod(0o600)
        monkeypatch.setenv(drive_owner.ENV_FILE, str(path))
        assert drive_owner.expected_drive_owner_email() == OWNER
        assert drive_owner.drive_owner_matches(OWNER) is True

    def test_bom_only_file_is_not_configured(self, tmp_path, monkeypatch):
        path = tmp_path / "owner.env"
        path.write_bytes(b"\xef\xbb\xbf\n")
        path.chmod(0o600)
        monkeypatch.setenv(drive_owner.ENV_FILE, str(path))
        assert drive_owner.expected_drive_owner_email() is None

    @pytest.mark.parametrize(
        "value", ["not-an-email", "a@b", "@example.test", "a b@example.test", "a@@example.test"]
    )
    def test_non_email_values_are_not_configured(self, tmp_path, monkeypatch, value):
        path = tmp_path / "owner.env"
        path.write_text(value, encoding="utf-8")
        path.chmod(0o600)
        monkeypatch.setenv(drive_owner.ENV_FILE, str(path))
        assert drive_owner.expected_drive_owner_email() is None
        monkeypatch.setenv(drive_owner.ENV_NAME, value)
        assert drive_owner.expected_drive_owner_email() is None
