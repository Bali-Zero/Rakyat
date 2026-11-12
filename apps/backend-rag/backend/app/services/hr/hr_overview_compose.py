"""The one composition of a member's HR picture, shared by `/me/overview` and `/people/{id}/overview`.

Both routes call `compose_member_overview` with the team_members id they resolved on their own
(the token's sub, or a People path id that passed its scope check), so what a member sees about
themselves and what an authorised viewer sees about that member can never drift apart.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from backend.app.services.hr.hr_card_models import member_card
from backend.app.services.hr.hr_overview_logic import bonus_totals
from backend.app.services.hr.hr_overview_models import MyHrOverview

logger = logging.getLogger(__name__)


class OverviewReadError(Exception):
    """The rows behind the overview could not be read. Carries no driver text."""


async def compose_member_overview(service: Any, member_id: str) -> MyHrOverview | None:
    """The member's overview, or None when they have no hr_employees row.

    `HRCardError` from the card read propagates (the caller maps it to its fixed message);
    any failure of the rows read becomes `OverviewReadError` after logging the class only.
    """
    card = await service.get_card_for_member(member_id)
    if not card:
        return None
    try:
        rows = await service.get_my_overview_rows(card["id"], member_id)
    except Exception as exc:  # a fixed message: a driver error can quote a value
        logger.error("hr overview read failed (%s)", type(exc).__name__)
        raise OverviewReadError from None
    return MyHrOverview(
        team_member_id=str(member_id),
        card=member_card(card),
        payslips=rows["payslips"],
        bonuses=rows["bonuses"],
        bonus_totals=bonus_totals(rows["bonuses"], datetime.now(timezone.utc)),
        leave={"balances": rows["balances"], "requests": rows["requests"]},
        attendance=rows["attendance"],
    )
