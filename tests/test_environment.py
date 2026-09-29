import stat

import pytest
from django.core.exceptions import ImproperlyConfigured

from pandect.config.environment import (
    boolean,
    comma_separated,
    create_data_directory,
    load_secret_key,
)


def test_data_directory_defaults_under_project_base(tmp_path, monkeypatch):
    monkeypatch.delenv("PANDECT_DATA_DIR", raising=False)

    data_dir = create_data_directory(tmp_path)

    assert data_dir == tmp_path / "data"
    assert data_dir.is_dir()


def test_relative_nondefault_data_directory_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("PANDECT_DATA_DIR", "state")

    with pytest.raises(ImproperlyConfigured, match="Only ./data"):
        create_data_directory(tmp_path)
    assert not (tmp_path / "state").exists()


def test_absolute_external_data_directory_is_supported(tmp_path, monkeypatch):
    base = tmp_path / "checkout"
    base.mkdir()
    external = tmp_path / "private-state"
    monkeypatch.setenv("PANDECT_DATA_DIR", str(external))

    assert create_data_directory(base) == external
    assert (external / ".pandect-data").is_file()


def test_existing_unrelated_directory_is_unchanged(tmp_path, monkeypatch):
    base = tmp_path / "checkout"
    base.mkdir()
    unrelated = tmp_path / "personal"
    unrelated.mkdir()
    (unrelated / "keep.txt").write_text("private")
    before = unrelated.stat().st_mode
    monkeypatch.setenv("PANDECT_DATA_DIR", str(unrelated))

    with pytest.raises(ImproperlyConfigured, match="unrelated nonempty"):
        create_data_directory(base)
    assert unrelated.stat().st_mode == before
    assert list(unrelated.iterdir()) == [unrelated / "keep.txt"]


def test_generated_secret_is_persistent_and_restrictive(tmp_path, monkeypatch):
    monkeypatch.delenv("PANDECT_SECRET_KEY", raising=False)

    first = load_secret_key(tmp_path)
    second = load_secret_key(tmp_path)
    secret_path = tmp_path / "secret.key"

    assert first == second
    assert secret_path.read_text(encoding="utf-8").strip() == first
    assert stat.S_IMODE(secret_path.stat().st_mode) == 0o600


def test_secret_environment_override_does_not_create_a_file(tmp_path, monkeypatch):
    monkeypatch.setenv("PANDECT_SECRET_KEY", "operator-provided-secret")

    assert load_secret_key(tmp_path) == "operator-provided-secret"
    assert not (tmp_path / "secret.key").exists()


def test_empty_secret_environment_override_uses_the_local_file(tmp_path, monkeypatch):
    monkeypatch.setenv("PANDECT_SECRET_KEY", "")

    generated = load_secret_key(tmp_path)

    assert (tmp_path / "secret.key").read_text(encoding="utf-8").strip() == generated


@pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
def test_boolean_accepts_explicit_true_values(monkeypatch, value):
    monkeypatch.setenv("PANDECT_FLAG", value)
    assert boolean("PANDECT_FLAG", default=False) is True


def test_boolean_rejects_ambiguous_values(monkeypatch):
    monkeypatch.setenv("PANDECT_FLAG", "sometimes")

    with pytest.raises(ImproperlyConfigured, match="PANDECT_FLAG must be one of"):
        boolean("PANDECT_FLAG", default=False)


def test_comma_separated_rejects_an_empty_override(monkeypatch):
    monkeypatch.setenv("PANDECT_VALUES", " , ")

    with pytest.raises(ImproperlyConfigured, match="at least one value"):
        comma_separated("PANDECT_VALUES", default=("default",))
