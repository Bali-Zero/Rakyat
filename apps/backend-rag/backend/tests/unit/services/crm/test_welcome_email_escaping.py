from backend.services.crm.welcome import welcome_email_service


def test_build_html_escapes_client_and_advisor_names():
    html = welcome_email_service._build_html(
        "en",
        "<b>Alex",
        '"<Mira',
        "advisor@example.test",
    )

    assert "&lt;b&gt;Alex." in html
    assert "&quot;&lt;Mira will reach out" in html
    assert "<b>Alex." not in html
    assert '"<Mira will reach out' not in html


def test_build_html_keeps_ordinary_names_unchanged():
    html = welcome_email_service._build_html(
        "en",
        "Ari",
        "Mira",
        "advisor@example.test",
    )

    assert "Ari." in html
    assert "Mira will reach out" in html
    assert "Chat with Mira on WhatsApp" in html
