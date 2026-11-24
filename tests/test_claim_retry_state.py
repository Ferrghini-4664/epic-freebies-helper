import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from claim_state import ClaimState
from detect_claim_retries import should_claim
from epic_promotions import active_free_offers

NOW = datetime(2026, 12, 25, 0, tzinfo=timezone.utc)


def catalog(hours=24, identity="daily", start=None):
    start = start or NOW - timedelta(hours=1)
    return {"data": {"Catalog": {"searchStore": {"elements": [{
        "id": identity, "namespace": "fixture", "title": identity,
        "promotions": {"promotionalOffers": [{"promotionalOffers": [{
            "startDate": start.isoformat(),
            "endDate": (start + timedelta(hours=hours)).isoformat(),
            "discountSetting": {"discountPercentage": 0},
        }]}]},
    }]}}}}


def games(payload, now=NOW):
    return [SimpleNamespace(**game) for game, _ in active_free_offers(payload, now)]


class ClaimRetryTest(unittest.TestCase):
    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(self.directory) / "ledger.json"
        self.state = ClaimState(self.path)
        self.emails = ["success@example.invalid", "failed@example.invalid"]
        self.payload = catalog()
        self.games = games(self.payload)

    def test_daily_success_survives_reload_only_failed_retries(self):
        self.state.register(self.emails, self.games, NOW)
        for email in self.emails:
            self.state.attempted(email, self.games, now=NOW)
        self.state.confirm(self.emails[0], self.games, {("fixture", "daily")})
        noon = NOW + timedelta(hours=12)
        restored = ClaimState(self.path)
        self.assertEqual([], restored.pending(self.emails[0], self.games, automatic=True, now=noon))
        self.assertEqual(1, len(restored.pending(self.emails[1], self.games, automatic=True, now=noon)))
        self.assertTrue(should_claim(self.payload, restored, self.emails, noon))
        restored.confirm(self.emails[1], self.games, {("fixture", "daily")})
        self.assertFalse(should_claim(self.payload, restored, self.emails, noon))

    def test_weekly_retry_remains_due_until_confirmed_or_expired(self):
        payload = catalog(168, "weekly")
        targets = games(payload)
        self.assertFalse(should_claim(payload, self.state, self.emails, NOW))
        self.state.register(self.emails, targets, NOW)
        self.state.confirm(self.emails[0], targets, {("fixture", "weekly")})
        self.state.attempted(self.emails[1], targets, now=NOW)
        self.assertFalse(should_claim(payload, self.state, self.emails, NOW + timedelta(hours=2)))
        self.assertTrue(should_claim(payload, self.state, self.emails, NOW + timedelta(hours=12)))
        self.state.attempted(self.emails[1], targets, now=NOW + timedelta(hours=12))
        self.assertTrue(should_claim(payload, self.state, self.emails, NOW + timedelta(days=2)))
        self.assertFalse(should_claim(payload, self.state, self.emails, NOW + timedelta(days=8)))

    def test_partial_orders_and_same_namespace_do_not_confirm_other_game(self):
        targets = self.games + games(catalog(24, "other"))
        self.state.register(self.emails, targets, NOW)
        self.state.confirm(self.emails[0], targets, {("fixture", "daily")})
        remaining = self.state.pending(self.emails[0], targets, now=NOW)
        self.assertEqual(["other"], [game.id for game in remaining])

    def test_new_promotion_window_and_added_account_need_claim(self):
        self.state.register(self.emails, self.games, NOW)
        for email in self.emails:
            self.state.confirm(email, self.games, {("fixture", "daily")})
        self.assertTrue(should_claim(self.payload, self.state, ["new@example.invalid"], NOW))
        later = NOW + timedelta(days=3)
        next_payload = catalog(24, start=later - timedelta(hours=1))
        self.assertTrue(should_claim(next_payload, self.state, self.emails, later))

    def test_rate_limit_waits_24_hours_and_removed_accounts_do_not_trigger(self):
        payload = catalog(168, "weekly")
        targets = games(payload)
        self.state.register(self.emails, targets, NOW)
        self.state.confirm(self.emails[0], targets, {("fixture", "weekly")})
        self.state.attempted(self.emails[1], targets, hours=24, now=NOW)
        self.assertFalse(should_claim(payload, self.state, self.emails, NOW + timedelta(hours=12)))
        self.assertTrue(should_claim(payload, self.state, self.emails, NOW + timedelta(hours=25)))
        self.assertFalse(should_claim(payload, self.state, [self.emails[0]], NOW + timedelta(hours=25)))

    def test_missing_or_damaged_state_does_not_skip_short_offer(self):
        self.path.write_text("broken", encoding="utf-8")
        self.assertTrue(should_claim(self.payload, ClaimState(self.path), self.emails, NOW))

    def test_invalid_confirmation_value_is_not_trusted(self):
        self.state.register(self.emails, self.games, NOW)
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        records = next(iter(raw["accounts"].values()))
        next(iter(records.values()))["confirmed"] = "true"
        self.path.write_text(json.dumps(raw), encoding="utf-8")
        self.assertTrue(should_claim(self.payload, ClaimState(self.path), self.emails, NOW))

    def test_state_contains_no_raw_account_or_password(self):
        self.state.register(self.emails, self.games, NOW)
        serialized = self.path.read_text(encoding="utf-8")
        for email in self.emails:
            self.assertNotIn(email, serialized)
        self.assertNotIn("password", serialized)

    def test_interrupt_preserves_prior_success_and_pending_accounts(self):
        self.state.register(self.emails, self.games, NOW)
        self.state.confirm(self.emails[0], self.games, {("fixture", "daily")})
        restored = ClaimState(self.path)
        self.assertEqual([], restored.pending(self.emails[0], self.games, now=NOW))
        self.assertTrue(restored.pending(self.emails[1], self.games, now=NOW))


class DeploymentRetryTest(unittest.IsolatedAsyncioTestCase):
    async def test_verified_order_is_saved_even_after_collection_error(self):
        import deploy
        with tempfile.TemporaryDirectory() as directory:
            target = games(catalog())[0]
            target.claim_end = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
            email = "success@example.invalid"
            state = ClaimState(Path(directory) / "ledger.json")
            state.register([email], [target])
            browser = SimpleNamespace(pages=[], new_page=AsyncMock(return_value=None))
            context = MagicMock()
            context.__aenter__ = AsyncMock(return_value=browser)
            context.__aexit__ = AsyncMock(return_value=False)
            agent = SimpleNamespace(
                refresh_order_namespaces=AsyncMock(return_value={"fixture"}),
                _orders=[SimpleNamespace(namespace="fixture", offerId="daily")],
            )
            auth = SimpleNamespace(invoke=AsyncMock(return_value=True))
            with patch.object(deploy, "CLAIM_STATE", state), patch.object(
                deploy, "settings", SimpleNamespace(EPIC_EMAIL=email)
            ), patch.object(deploy, "open_browser_context", return_value=context), patch.object(
                deploy, "EpicAuthorization", return_value=auth
            ), patch.object(deploy, "EpicAgent", return_value=agent), patch.object(
                deploy, "collect_epic_games_with_summary",
                new=AsyncMock(side_effect=RuntimeError("fixture collection interrupted"))
            ):
                with self.assertRaises(RuntimeError):
                    await deploy.execute_browser_tasks(
                        collect_summary=True, pending_promotions=[target]
                    )
            self.assertEqual([], ClaimState(state.path).pending(email, [target]))

    async def test_normal_entry_only_opens_browser_for_pending_account(self):
        import deploy
        from services.epic_collection_summary_service import CollectionSummary
        from models import PromotionGame

        with tempfile.TemporaryDirectory() as directory:
            target = PromotionGame(**{**games(catalog(168, "weekly"))[0].__dict__,
                                      "description": "", "offerType": "BASE_GAME", "url": "https://example.invalid"})
            # Dynamic time keeps the actual deployment's expiration check deterministic.
            target.claim_end = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
            state = ClaimState(Path(directory) / "ledger.json")
            emails = ["success@example.invalid", "failed@example.invalid"]
            state.register(emails, [target])
            state.confirm(emails[0], [target], {("fixture", "weekly")})
            settings = SimpleNamespace(EPIC_EMAIL="", llm_configuration_error=None)
            called = []

            def swap(email, password):
                settings.EPIC_EMAIL = email

            async def execute(**kwargs):
                called.append(settings.EPIC_EMAIL)
                self.assertEqual([target], kwargs["pending_promotions"])
                return CollectionSummary()  # No verified order update, so still unconfirmed.

            with patch.object(deploy, "CLAIM_STATE", state), patch.object(
                deploy, "CLAIM_TARGETS", None
            ), patch.object(deploy, "get_promotions", return_value=[target]), patch.object(
                deploy, "settings", settings
            ), patch.object(deploy, "swap_account", side_effect=swap), patch.object(
                deploy, "execute_browser_tasks", side_effect=execute
            ), patch.object(deploy, "telegram_notifications_enabled", return_value=False), patch.object(
                deploy, "wxpush_notifications_enabled", return_value=False
            ):
                with self.assertRaises(RuntimeError):
                    await deploy.execute_multiple_accounts([(email, "fixture-password") for email in emails])
            self.assertEqual([emails[1]], called)
            self.assertTrue(state.pending(emails[1], [target]))


if __name__ == "__main__":
    unittest.main()
