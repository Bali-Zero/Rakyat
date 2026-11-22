import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parents[6] / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))

import check_llm_cost_tracking as cost_gate  # noqa: E402


def test_scan_does_not_whitelist_parent_directory_name(tmp_path, monkeypatch):
    repository = tmp_path / "tests" / "fake-repository"
    paid_file = repository / "apps" / "paid_client.py"
    paid_file.parent.mkdir(parents=True)
    paid_file.write_text("from openai import AsyncOpenAI\nAsyncOpenAI()\n")
    monkeypatch.setattr(
        cost_gate,
        "__file__",
        str(repository / "scripts" / "check_llm_cost_tracking.py"),
    )

    assert str(paid_file) in cost_gate.scan_files([repository])


def test_scan_keeps_repository_exemptions_and_cost_tracking(tmp_path, monkeypatch):
    repository = tmp_path / "fake-repository"
    exempt_file = repository / "apps" / "backend" / "tests" / "fixture.py"
    tracked_file = repository / "apps" / "backend" / "paid_client.py"
    exempt_file.parent.mkdir(parents=True)
    tracked_file.parent.mkdir(parents=True, exist_ok=True)
    exempt_file.write_text("from openai import AsyncOpenAI\nAsyncOpenAI()\n")
    tracked_file.write_text("from openai import AsyncOpenAI\nrecord_llm_call()\n")
    monkeypatch.setattr(
        cost_gate,
        "__file__",
        str(repository / "scripts" / "check_llm_cost_tracking.py"),
    )

    violations = cost_gate.scan_files([repository])

    assert str(exempt_file) not in violations
    assert str(tracked_file) not in violations


def test_scan_keeps_a_top_level_exempt_directory(tmp_path, monkeypatch):
    repository = tmp_path / "fake-repository"
    exempt_file = repository / "tests" / "fixture.py"
    exempt_file.parent.mkdir(parents=True)
    exempt_file.write_text("from openai import AsyncOpenAI\nAsyncOpenAI()\n")
    monkeypatch.setattr(
        cost_gate,
        "__file__",
        str(repository / "scripts" / "check_llm_cost_tracking.py"),
    )

    assert cost_gate.scan_files([repository]) == []
