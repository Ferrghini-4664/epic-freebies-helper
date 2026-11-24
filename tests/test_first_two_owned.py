import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import deploy
from claim_state import ClaimState
from detect_claim_retries import should_claim
from epic_promotions import active_free_offers
from models import PromotionGame
from services import epic_collection_summary_service as summaries

GAME = PromotionGame(title="fixture", id="offer", namespace="namespace",
                     description="", offerType="BASE_GAME", url="https://example.invalid",
                     claim_key="window", claim_end=(datetime.now(timezone.utc)+timedelta(days=3)).isoformat())


def owned():
    return summaries.CollectionSummary(
        all_promotions=[GAME], previously_claimed_promotions=[GAME],
        all_previously_owned=True,
    )


class FirstTwoOwnedTest(unittest.IsolatedAsyncioTestCase):
    async def batch(self, first, second, enabled=True):
        calls = []
        rows = []

        async def execute(**kwargs):
            index = len(calls)
            calls.append(index)
            result = [first, second][index] if index < 2 else owned()
            if isinstance(result, Exception):
                raise result
            return result

        with patch.dict(os.environ, {"EPIC_STOP_AFTER_TWO_OWNED": str(enabled).lower()}), patch.object(
            deploy, "CLAIM_STATE", None
        ), patch.object(
            deploy, "settings", SimpleNamespace(llm_configuration_error=None)
        ), patch.object(deploy, "swap_account"), patch.object(
            deploy, "telegram_notifications_enabled", return_value=False
        ), patch.object(deploy, "wxpush_notifications_enabled", return_value=True), patch.object(
            deploy, "send_batch_summary_to_wxpush",
            new=AsyncMock(side_effect=lambda result: rows.extend(result))
        ), patch.object(
            deploy, "failure_summary_from_exception",
            side_effect=lambda error: summaries.CollectionSummary(error_message=str(error))
        ), patch.object(deploy, "execute_browser_tasks", side_effect=execute):
            accounts = [(f"fixture{index}@example.invalid", "fixture-password") for index in range(8)]
            try:
                await deploy.execute_multiple_accounts(accounts)
            except RuntimeError:
                if not isinstance(first, Exception) and not isinstance(second, Exception):
                    raise
        return calls, rows

    async def test_two_preowned_stop_at_two_and_report_other_six_as_rule_skips(self):
        calls, rows = await self.batch(owned(), owned())
        self.assertEqual(2, len(calls))
        self.assertEqual(8, len(rows))
        self.assertEqual(["按规则跳过"] * 6, [row["status"] for row in rows[2:]])

    async def test_new_claim_does_not_trigger_shortcut(self):
        newly = summaries.CollectionSummary(all_promotions=[GAME], newly_claimed_promotions=[GAME])
        calls, _ = await self.batch(owned(), newly)
        self.assertEqual(8, len(calls))

    async def test_failure_or_unconfirmed_does_not_trigger_shortcut(self):
        for second in (RuntimeError("fixture failure"), summaries.CollectionSummary(
            all_promotions=[GAME], unconfirmed_promotions=[GAME]
        )):
            calls, _ = await self.batch(owned(), second)
            self.assertEqual(8, len(calls))

    async def test_empty_snapshot_or_disabled_setting_keeps_all_accounts(self):
        calls, _ = await self.batch(owned(), summaries.CollectionSummary())
        self.assertEqual(8, len(calls))
        calls, _ = await self.batch(owned(), owned(), enabled=False)
        self.assertEqual(8, len(calls))

    async def test_exact_pre_run_orders_are_required(self):
        for offer_id, expected in (("offer", True), ("other-offer", False)):
            agent = SimpleNamespace(
                _orders=[SimpleNamespace(namespace=GAME.namespace, offerId=offer_id)],
                refresh_order_namespaces=AsyncMock(return_value={GAME.namespace}),
                collect_epic_games=AsyncMock(),
            )
            with patch.object(summaries, "get_promotions", return_value=[GAME]):
                summary = await summaries.collect_epic_games_with_summary(agent)
            self.assertEqual(expected, summary.all_previously_owned)

    async def test_detector_honors_cached_first_two_but_new_game_resets_rule(self):
        now = datetime.now(timezone.utc)
        def payload(identity):
            return {"data": {"Catalog": {"searchStore": {"elements": [{
                "id": identity, "namespace": "fixture",
                "promotions": {"promotionalOffers": [{"promotionalOffers": [{
                    "startDate": (now-timedelta(hours=1)).isoformat(),
                    "endDate": (now+timedelta(hours=23)).isoformat(),
                    "discountSetting": {"discountPercentage": 0},
                }]}]},
            }]}}}}
        current = payload("one")
        games = [SimpleNamespace(**game) for game, _ in active_free_offers(current, now)]
        emails = [f"fixture{index}@example.invalid" for index in range(8)]
        with tempfile.TemporaryDirectory() as folder, patch.dict(
            os.environ, {"EPIC_STOP_AFTER_TWO_OWNED": "true"}
        ):
            state = ClaimState(Path(folder)/"ledger.json")
            state.register(emails, games, now)
            for email in emails[:2]:
                state.confirm(email, games, {("fixture", "one")})
            self.assertFalse(should_claim(current, state, emails, now))
            self.assertTrue(should_claim(payload("two"), state, emails, now))
            self.assertTrue(state.pending(emails[2], games, now=now))


if __name__ == "__main__":
    unittest.main()
