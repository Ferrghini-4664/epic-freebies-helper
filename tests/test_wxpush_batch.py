import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import deploy
from services.epic_collection_summary_service import CollectionSummary
from services import wxpush_notification_service as wxpush


class BatchNotificationTest(unittest.IsolatedAsyncioTestCase):
    async def run_batch(self, outcomes):
        accounts = [(f"fixture{index}@example.invalid", "fixture-password")
                    for index in range(len(outcomes))]
        executed = []
        result = []

        async def collect(**kwargs):
            index = len(executed)
            executed.append(index)
            value = outcomes[index]
            if isinstance(value, Exception):
                raise value
            return value

        async def send(rows):
            self.assertEqual(len(accounts), len(executed))
            result.extend(rows)

        settings = SimpleNamespace(llm_configuration_error=None)
        with patch.object(deploy, "CLAIM_STATE", None), patch.object(
            deploy, "settings", settings
        ), patch.object(deploy, "swap_account"), patch.object(
            deploy, "execute_browser_tasks", side_effect=collect
        ), patch.object(deploy, "telegram_notifications_enabled", return_value=False), patch.object(
            deploy, "wxpush_notifications_enabled", return_value=True
        ), patch.object(
            deploy, "failure_summary_from_exception",
            side_effect=lambda error: CollectionSummary(error_message=str(error))
        ), patch.object(
            deploy, "send_collection_summary_to_wxpush", new_callable=AsyncMock
        ) as per_account, patch.object(
            deploy, "send_batch_summary_to_wxpush", side_effect=send
        ) as batch:
            if any(isinstance(value, Exception) for value in outcomes):
                with self.assertRaises(RuntimeError):
                    await deploy.execute_multiple_accounts(accounts)
            else:
                await deploy.execute_multiple_accounts(accounts)
            per_account.assert_not_awaited()
            batch.assert_awaited_once()
        return result

    async def test_all_success_send_only_after_last_account(self):
        rows = await self.run_batch([CollectionSummary(), CollectionSummary()])
        self.assertEqual(["成功", "成功"], [row["status"] for row in rows])

    async def test_failure_does_not_send_early_or_stop_later_accounts(self):
        rows = await self.run_batch([
            CollectionSummary(), RuntimeError("fixture login failed"), CollectionSummary()
        ])
        self.assertEqual(["成功", "失败", "成功"], [row["status"] for row in rows])

    async def test_aggregation_keeps_pending_and_skipped_counts(self):
        rows = [
            {"account": "fi***@example.invalid", "status": status, "summary": None}
            for status in ["成功", "失败", "未确认", "跳过", "限流待重试"]
        ]
        with patch.object(wxpush, "_send_wxpush_message", new_callable=AsyncMock) as send:
            await wxpush.send_batch_summary_to_wxpush(rows)
            send.assert_awaited_once()
            title, content = send.await_args.args
        self.assertIn("待重试3", title)
        for status in ["成功", "失败", "未确认", "跳过", "限流待重试"]:
            self.assertIn(status, content)

    async def test_large_batch_fits_wechat_fields(self):
        rows = [{"account": f"f{index}***@example.invalid", "status": "成功", "summary": None}
                for index in range(100)]
        with patch.object(wxpush, "_send_wxpush_message", new_callable=AsyncMock) as send:
            await wxpush.send_batch_summary_to_wxpush(rows)
            title, content = send.await_args.args
        self.assertLessEqual(len(title), wxpush.TITLE_FIELD_MAX_CHARS)
        self.assertLessEqual(len(content), wxpush.CONTENT_MAX_CHARS)
        self.assertIn("100个账号", content)


if __name__ == "__main__":
    unittest.main()
