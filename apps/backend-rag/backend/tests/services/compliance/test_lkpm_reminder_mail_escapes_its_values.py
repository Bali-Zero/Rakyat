"""The LKPM reminder mail must not let database values enter its HTML as markup."""

from __future__ import annotations

from unittest.mock import MagicMock

from backend.services.compliance.lkpm_deadline_notifier import (
    _ROW_COLORS,
    LKPM_DASHBOARD_URL,
    LKPMDeadlineNotifier,
)


def _notifier() -> LKPMDeadlineNotifier:
    return LKPMDeadlineNotifier(MagicMock())


def _row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "company_name": "PT Contoh Klien",
        "oss_username": "contoh.oss",
        "status": "draft",
        "days_until_deadline": 5,
    }
    row.update(overrides)
    return row


def test_row_values_from_the_database_are_escaped() -> None:
    body = _notifier()._build_email_body(
        "Tim Tax",
        [
            _row(
                company_name='<a href="https://example.test/login">PT Contoh</a>',
                oss_username="<img src=x>",
                status="<b>draft</b>",
            )
        ],
    )
    assert "&lt;a href=&quot;https://example.test/login&quot;&gt;PT Contoh&lt;/a&gt;" in body
    assert "&lt;img src=x&gt;" in body
    assert "&lt;b&gt;draft&lt;/b&gt;" in body
    assert '<a href="https://example.test' not in body
    assert "<img" not in body
    assert "<b>draft" not in body


def test_greeting_name_is_escaped() -> None:
    body = _notifier()._build_email_body("<script>x</script>", [_row()])
    assert "&lt;script&gt;x&lt;/script&gt;" in body
    assert "<script>" not in body


def test_plain_row_renders_as_before() -> None:
    body = _notifier()._build_email_body("Tim Tax", [_row()])
    assert f'<tr style="background:{_ROW_COLORS["warning"]};">' in body
    assert "<td style='padding:8px;'>PT Contoh Klien</td>" in body
    assert "<td style='padding:8px;'>contoh.oss</td>" in body
    assert "<td style='padding:8px;'>draft</td>" in body
    assert "<td style='padding:8px; font-weight:bold;'>5 hari</td>" in body


def test_dashboard_link_is_still_a_real_link() -> None:
    body = _notifier()._build_email_body("Tim Tax", [_row()])
    assert f'<a href="{LKPM_DASHBOARD_URL}">{LKPM_DASHBOARD_URL}</a>' in body
