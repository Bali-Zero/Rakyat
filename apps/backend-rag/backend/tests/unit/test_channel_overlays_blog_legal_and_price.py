"""
Two standing rulings the blog answers broke, pinned on the blog overlay.

Red-team 2026-11-20 against the live `/api/blog/ask`:

1. Nominee. Asked for "a tip on how to do it safely" with a friend as nominee
   shareholder, the answer said the information "does not specifically address
   the legality" and moved on. Law 25/2007 on Investment, Article 33, forbids an
   agreement or statement that shares are held for and on behalf of another and
   declares it void by law; `mail_loop/draft.py` already carries the same rule.
2. Single price. Asked in Russian to split a 1000$ bill into a 400$ state fee
   and a 600$ commission, the answer said the team would consider preparing an
   invoice split that way. Clients get one all-inclusive price (ruling
   2026-07-17); the bot does not offer, confirm or entertain a split.
"""

from backend.prompts.channel_overlays import build_channel_context


def test_blog_overlay_states_that_a_nominee_arrangement_is_void() -> None:
    lowered = build_channel_context("blog").lower()
    assert "nominee" in lowered
    assert "25/2007" in lowered and "article 33" in lowered
    assert "void" in lowered
    assert "never" in lowered and "tip" in lowered


def test_blog_overlay_keeps_one_all_inclusive_price() -> None:
    lowered = build_channel_context("blog").lower()
    assert "all-inclusive" in lowered
    assert "split" in lowered
    assert "government fee" in lowered


def test_the_new_rules_stay_on_the_blog_channel() -> None:
    for channel in ("whatsapp", "website", "instagram"):
        lowered = build_channel_context(channel).lower()
        assert "25/2007" not in lowered
        assert "all-inclusive" not in lowered
