"""Provider composition + per-order routing (spec §3.2/§3.3).

The repository holds a registry: checkout uses the selected
provider, an existing order always talks to ITS OWN provider —
flipping the selector can never strand an in-flight order on the
wrong adapter (signature or status checks against a stranger).
"""

from __future__ import annotations

import pytest

from backend.services.garuda_orders.repository import GarudaOrderRepository


class _FakeProvider:
    def __init__(self, name: str) -> None:
        self.name = name


def _repo(selected: str, providers: dict) -> GarudaOrderRepository:
    return GarudaOrderRepository(
        None,  # pool: unused by the routing unit under test
        eligibility_lookup=None,
        provider=providers[selected],
        environment="TEST",
        provider_name=selected,
        providers=providers,
    )


def test_selected_provider_is_the_default_for_unknown_orders():
    xendit, midtrans = _FakeProvider("xendit"), _FakeProvider("midtrans")
    repo = _repo("midtrans", {"xendit": xendit, "midtrans": midtrans})
    assert repo.provider_for(None) is midtrans
    assert repo.provider_for("xendit") is xendit
    assert repo.provider_for("midtrans") is midtrans


def test_registry_defaults_to_selected_provider_alone():
    only = _FakeProvider("xendit")
    repo = GarudaOrderRepository(
        None,
        eligibility_lookup=None,
        provider=only,
        environment="TEST",
    )
    assert repo.provider_for("xendit") is only
    assert repo.provider_for(None) is only


def test_explicit_provider_not_wired_fails_closed():
    # An order naming a provider this process cannot serve must
    # never be answered by the WRONG vendor (gate finding).
    from backend.services.garuda_orders.errors import PaymentProviderUnavailable

    repo = _repo("midtrans", {"midtrans": _FakeProvider("midtrans")})
    with pytest.raises(PaymentProviderUnavailable, match="not wired"):
        repo.provider_for("xendit")


def test_providers_carry_stable_names_for_inbox_rows():
    from backend.services.payments.midtrans import MidtransPaymentProvider
    from backend.services.payments.xendit import XenditPaymentProvider

    assert MidtransPaymentProvider.name == "midtrans"
    assert XenditPaymentProvider.name == "xendit"
