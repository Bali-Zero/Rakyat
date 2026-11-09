"""Partner-facing sanitizers shared by the router and the commission emails."""


def sterilize_service_type_for_partner(raw: str | None) -> str:
    """Map a raw service_type to a generic category for partner visibility.

    CRIT-6 / UU PDP data minimization: partner should know a referral converted,
    not which specific visa class / investment threshold / tax service was
    purchased. Revealing "KITAS E33G" tells a partner the client's residency
    plan; "PT PMA Rp 10M" reveals investment scale.

    Mapping is additive — add new keywords when new service types land.
    """
    if not raw:
        return "Service"
    rl = raw.lower()
    if any(k in rl for k in ("kitas", "visa", "c1", "c2", "c7", "e33", "e23", "e28")):
        return "Visa / KITAS service"
    if "kitap" in rl:
        return "KITAP service"
    if any(k in rl for k in ("pma", "company", "pt ", "perusahaan")):
        return "Company setup"
    if any(k in rl for k in ("tax", "pajak", "pph", "npwp")):
        return "Tax service"
    if any(k in rl for k in ("property", "sertifikat")):
        return "Property service"
    return "Other service"
