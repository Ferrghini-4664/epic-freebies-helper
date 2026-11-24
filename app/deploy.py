# -*- coding: utf-8 -*-
"""
Epic Games Free Game Collection Deployment Module

This module orchestrates the automated collection of free games from Epic Games Store
using browser automation and scheduling capabilities.

@Time    : 2025/7/16 21:28
@Author  : QIN2DIM
@GitHub  : https://github.com/QIN2DIM
"""

import asyncio
import json
import os
import signal
from contextlib import suppress
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from loguru import logger
from pytz import timezone

from claim_state import ClaimState, account_key
from accounts import get_epic_accounts_raw, mask_email, parse_multi_accounts, swap_account
from services.epic_authorization_service import EpicAuthorization
from services.browser_context import open_browser_context, resolve_headless_mode
from services.epic_collection_summary_service import collect_epic_games_with_summary
from services.epic_games_service import EpicAgent, EpicFreeGameRateLimitError, get_promotions
from services.telegram_notification_service import (
    failure_summary_from_exception,
    send_collection_summary_to_telegram,
    telegram_notifications_enabled,
)
from services.wxpush_notification_service import (
    send_collection_summary_to_wxpush,
    send_batch_summary_to_wxpush,
    wxpush_notifications_enabled,
)
from settings import LOG_DIR
from settings import settings
from utils import init_log

# Initialize logging configuration for runtime, error, and serialization logs
init_log(
    runtime=LOG_DIR.joinpath("runtime.log"),
    error=LOG_DIR.joinpath("error.log"),
    serialize=LOG_DIR.joinpath("serialize.log"),
)

# Default timezone for scheduling operations
TIMEZONE = timezone("Asia/Shanghai")
RATE_LIMITED_OUTCOME = "rate_limited"
UNCONFIRMED_OUTCOME = "unconfirmed"
CLAIM_STATE = ClaimState(os.environ["EPIC_CLAIM_STATE_PATH"]) if os.getenv("EPIC_CLAIM_STATE_PATH") else None
CLAIM_TARGETS = None


def _prepare_claim_state(emails):
    global CLAIM_TARGETS
    if CLAIM_STATE is not None and CLAIM_TARGETS is None:
        CLAIM_TARGETS = get_promotions()
        CLAIM_STATE.register(emails, CLAIM_TARGETS)



def _is_free_game_rate_limit_error(err: Exception) -> bool:
    current: BaseException | None = err
    seen: set[int] = set()

    while current is not None and id(current) not in seen:
        if isinstance(current, EpicFreeGameRateLimitError):
            return True
        seen.add(id(current))
        current = current.__cause__ or current.__context__

    return False


@logger.catch(reraise=True)
async def execute_browser_tasks(
    headless: bool | str = True, *, collect_summary: bool = False, pending_promotions=None
):
    """
    Execute Epic Games free game collection tasks using browser automation.

    This function handles the complete workflow of authenticating with Epic Games
    and collecting available free games through browser automation.

    Args:
        headless: Whether to run browser in headless mode
    """
    logger.debug("Starting Epic Games collection task")

    # Configure browser with anti-detection features and video recording
    async with open_browser_context(headless=headless) as browser:
        # Initialize or reuse existing browser page
        page = browser.pages[0] if browser.pages else await browser.new_page()
        logger.debug("Browser initialized successfully")

        # Handle Epic Games authentication
        logger.debug("Initiating Epic Games authentication")
        agent = EpicAuthorization(page)
        is_authenticated = await agent.invoke()
        if not is_authenticated:
            raise RuntimeError("Authentication failed, aborting this run")
        logger.debug("Authentication completed")

        # Execute a free games collection on new page
        logger.debug("Starting free games collection process")
        game_page = await browser.new_page()
        agent = EpicAgent(game_page)
        try:
            if collect_summary:
                summary = await collect_epic_games_with_summary(agent)
            else:
                await agent.collect_epic_games()
                summary = None
        finally:
            if CLAIM_STATE is not None and pending_promotions:
                # An exit code or a namespace match alone cannot confirm an offer.
                try:
                    await agent.refresh_order_namespaces()
                    order_keys = {(order.namespace, order.offerId) for order in agent._orders}
                    CLAIM_STATE.confirm(settings.EPIC_EMAIL, pending_promotions, order_keys)
                except Exception as error:
                    logger.warning("Claim verification unavailable | error_type={}", type(error).__name__)
        logger.debug("Free games collection completed")

        # Cleanup browser resources
        logger.debug("Cleaning up browser resources")
        with suppress(Exception):
            for p in browser.pages:
                await p.close()

        logger.debug("Browser tasks execution finished successfully")
        return summary


async def _deliver_collection_summary(
    summary, *, account_label: str | None = None, wxpush_results: list | None = None
) -> None:
    """Fan the claim summary out to every configured notification channel."""
    if telegram_notifications_enabled():
        await send_collection_summary_to_telegram(summary, account_label=account_label)
    if wxpush_results is not None:
        wxpush_results.append({"account": account_label, "summary": summary})
    elif wxpush_notifications_enabled():
        await send_collection_summary_to_wxpush(summary, account_label=account_label)


async def execute_browser_tasks_with_notification(
    headless: bool | str = True, *, account_label: str | None = None,
    wxpush_results: list | None = None
) -> str | None:
    pending_promotions = None
    if CLAIM_STATE is not None:
        _prepare_claim_state([settings.EPIC_EMAIL])
        pending_promotions = CLAIM_STATE.pending(
            settings.EPIC_EMAIL, CLAIM_TARGETS,
            automatic=os.getenv("EPIC_RETRY_ONLY") == "true",
        )
        if not pending_promotions:
            logger.info("Skipping account: no unconfirmed active offers due for retry")
            if wxpush_results is not None:
                wxpush_results.append({
                    "account": account_label, "summary": None, "status": "跳过",
                    "all_previously_owned": CLAIM_STATE.all_confirmed(settings.EPIC_EMAIL, CLAIM_TARGETS),
                })
            return None
        CLAIM_STATE.attempted(settings.EPIC_EMAIL, pending_promotions)

    if configuration_error := settings.llm_configuration_error:
        logger.error(configuration_error)
        raise RuntimeError(configuration_error)

    notifications_enabled = telegram_notifications_enabled() or wxpush_notifications_enabled()
    if not notifications_enabled:
        logger.debug("No notification channel is configured; using standard collection flow")
    else:
        enabled_channels = []
        if telegram_notifications_enabled():
            enabled_channels.append("Telegram")
        if wxpush_notifications_enabled():
            enabled_channels.append("WXPush")
        logger.debug("Notification channel(s) enabled: {}", ", ".join(enabled_channels))

    browser_options = {
        "headless": headless,
        "collect_summary": notifications_enabled or CLAIM_STATE is not None,
    }
    if CLAIM_STATE is not None:
        browser_options["pending_promotions"] = pending_promotions
    try:
        summary = await execute_browser_tasks(**browser_options)
    except Exception as err:
        # Notify first, then decide whether this failure is a rate limit — the
        # rate-limited outcome must not swallow the failure notification.
        if notifications_enabled:
            await _deliver_collection_summary(
                failure_summary_from_exception(err), account_label=account_label,
                wxpush_results=wxpush_results
            )
        if _is_free_game_rate_limit_error(err):
            logger.warning(
                "Epic 24-hour free-game limit detected; ending this run without retry. "
                "No successful claim was confirmed for the affected account."
            )
            if CLAIM_STATE is not None:
                CLAIM_STATE.attempted(settings.EPIC_EMAIL, pending_promotions, hours=24)
            return RATE_LIMITED_OUTCOME
        raise

    if CLAIM_STATE is not None:
        remaining = CLAIM_STATE.pending(settings.EPIC_EMAIL, pending_promotions)
        if remaining and summary is not None:
            summary.unconfirmed_promotions = remaining
            summary.error_message = summary.error_message or "Order history did not confirm all active offers"
    if notifications_enabled:
        await _deliver_collection_summary(
            summary, account_label=account_label, wxpush_results=wxpush_results
        )
    if CLAIM_STATE is not None and CLAIM_STATE.pending(
        settings.EPIC_EMAIL, pending_promotions
    ):
        logger.warning("Account still has unconfirmed offers; retry remains pending")
        return UNCONFIRMED_OUTCOME
    return None


async def execute_multiple_accounts(
    accounts: list[tuple[str, str]], headless: bool | str = True
) -> None:
    """Run collection for an explicitly enabled, fully valid multi-account list."""
    _prepare_claim_state([email for email, _ in accounts])
    total = len(accounts)
    succeeded = 0
    rate_limited_accounts: list[str] = []
    failed_accounts: list[str] = []
    wxpush_results: list[dict] = []
    stop_on_owned_quorum = os.getenv("EPIC_STOP_IF_TWO_OF_THREE_OWNED") == "true"

    for index, (email, password) in enumerate(accounts, 1):
        masked_email = mask_email(email)
        logger.info("=" * 60)
        logger.info("Processing account {}/{}: {}", index, total, masked_email)
        logger.info("=" * 60)

        result_start = len(wxpush_results)
        status = "成功"
        try:
            # Swap active credentials so user_data_dir and login use this account.
            swap_account(email, password)
            outcome = await execute_browser_tasks_with_notification(
                headless=headless, account_label=masked_email,
                wxpush_results=wxpush_results
            )
            if outcome == RATE_LIMITED_OUTCOME:
                status = "限流待重试"
                rate_limited_accounts.append(masked_email)
                logger.warning(
                    "Account {}/{} stopped by Epic's 24-hour free-game limit: {}",
                    index,
                    total,
                    masked_email,
                )
            elif outcome == UNCONFIRMED_OUTCOME:
                status = "未确认"
                failed_accounts.append(masked_email)
            else:
                succeeded += 1
                logger.success("Account {}/{} completed: {}", index, total, masked_email)
        except Exception as err:
            status = "失败"
            failed_accounts.append(masked_email)
            logger.error("Account {}/{} failed: {} | error: {}", index, total, masked_email, err)
            # Continue to next account — don't abort the entire run

        if len(wxpush_results) == result_start:
            wxpush_results.append({"account": masked_email, "summary": None})
        row = wxpush_results[-1]
        row.setdefault("status", status)
        summary = row.get("summary")
        if summary is not None:
            row["all_previously_owned"] = (
                summary.all_previously_owned
                and bool(summary.all_promotions)
                and not summary.newly_claimed_promotions
                and not summary.unconfirmed_promotions
                and not summary.failed_promotions
                and not summary.error_message
            )
        owned_accounts = {
            account_key(accounts[position][0])
            for position, result in enumerate(wxpush_results[:3])
            if result.get("all_previously_owned")
            and result["status"] in {"成功", "跳过"}
        }
        if stop_on_owned_quorum and index <= 3 and index < total and len(owned_accounts) >= 2:
            logger.info("Two of the first three accounts already own all active offers; skipping remaining accounts by configured rule")
            wxpush_results.extend(
                {"account": mask_email(other_email), "summary": None, "status": "按规则跳过"}
                for other_email, _ in accounts[index:]
            )
            break

    if wxpush_notifications_enabled():
        await send_batch_summary_to_wxpush(wxpush_results)

    logger.info("=" * 60)
    logger.info(
        "Multi-account run summary: {}/{} succeeded, {} rate-limited",
        succeeded,
        total,
        len(rate_limited_accounts),
    )
    if failed_accounts:
        logger.warning("Failed accounts: {}", ", ".join(failed_accounts))
        raise RuntimeError(
            f"{len(failed_accounts)} of {total} account(s) failed: " + ", ".join(failed_accounts)
        )
    if rate_limited_accounts:
        logger.warning(
            "Run ended without retry for rate-limited account(s): {}",
            ", ".join(rate_limited_accounts),
        )
    elif any(result["status"] == "按规则跳过" for result in wxpush_results):
        logger.info("Remaining accounts were skipped under the two-of-three-owned rule")
    else:
        logger.success("All {} account(s) completed successfully", total)


async def _run_accounts(headless: bool | str = True) -> None:
    """
    Dispatch single-account and multi-account collection.

    - EPIC_ACCOUNTS unset/empty: exact legacy single-account path
    - EPIC_ACCOUNTS present but no valid rows: fall back to legacy path
    - some valid + some invalid rows: fail with configuration error
    - fully valid rows: multi-account aggregation loop
    """
    raw = get_epic_accounts_raw()

    if not raw:
        # Preserve the exact legacy single-account execution path.
        outcome = await execute_browser_tasks_with_notification(headless=headless)
        if outcome == UNCONFIRMED_OUTCOME:
            raise RuntimeError("Active offers remain unconfirmed")
        return

    accounts, invalid_lines = parse_multi_accounts(raw)

    if not accounts:
        email = (settings.EPIC_EMAIL or "").strip()
        password = settings.EPIC_PASSWORD.get_secret_value().strip()
        if not email or not password:
            raise RuntimeError(
                "EPIC_ACCOUNTS contains no valid entries and "
                "EPIC_EMAIL / EPIC_PASSWORD are not configured."
            )
        logger.warning(
            "No valid EPIC_ACCOUNTS entries; using the legacy single-account configuration"
        )
        outcome = await execute_browser_tasks_with_notification(headless=headless)
        if outcome == UNCONFIRMED_OUTCOME:
            raise RuntimeError("Active offers remain unconfirmed")
        return

    if invalid_lines:
        raise RuntimeError(
            "Invalid EPIC_ACCOUNTS entries on line(s): " + ", ".join(map(str, invalid_lines))
        )

    # Only explicitly enabled, fully valid multi-account configurations
    # should enter the aggregation loop.
    await execute_multiple_accounts(accounts, headless=headless)


async def deploy():
    """
    Main deployment function that executes Epic Games collection tasks.

    This function runs the collection process immediately and optionally
    sets up a scheduled task for automatic recurring execution.
    """
    headless = resolve_headless_mode()

    # Log current configuration for debugging
    sj = settings.model_dump(mode="json")
    sj["headless"] = headless
    logger.debug(
        f"Starting deployment with configuration: {json.dumps(sj, indent=2, ensure_ascii=False)}"
    )
    logger.info(
        "Effective LLM model routing | provider={} | challenge_classifier={} | "
        "image_classifier={} | spatial_point_reasoner={} | spatial_path_reasoner={}",
        settings.LLM_PROVIDER,
        settings.CHALLENGE_CLASSIFIER_MODEL,
        settings.IMAGE_CLASSIFIER_MODEL,
        settings.SPATIAL_POINT_REASONER_MODEL,
        settings.SPATIAL_PATH_REASONER_MODEL,
    )

    # Execute an immediate collection task (single- or multi-account)
    await _run_accounts(headless=headless)

    # Skip scheduler setup if disabled in configuration
    if not settings.ENABLE_APSCHEDULER:
        logger.debug("Scheduler is disabled, deployment completed")
        return

    # Initialize and configure async scheduler
    scheduler = AsyncIOScheduler()

    # Strategy 1: Thursday 23:30 to Friday 03:30, every hour (Beijing Time)
    scheduler.add_job(
        _run_accounts,
        trigger=CronTrigger(
            day_of_week="thu", hour="23,0,1,2,3", minute="30", timezone="Asia/Shanghai"
        ),
        id="weekly_epic_games_task",
        name="weekly_epic_games_task",
        args=[headless],
        replace_existing=False,
        max_instances=1,
    )

    # Strategy 2: Daily at 12:00 PM (Beijing Time)
    scheduler.add_job(
        _run_accounts,
        trigger=CronTrigger(hour="12", minute="0", timezone="Asia/Shanghai"),
        id="daily_epic_games_task",
        name="daily_epic_games_task",
        args=[headless],
        replace_existing=False,
        max_instances=1,
    )

    # Set up graceful shutdown signal handlers
    shutdown_event = asyncio.Event()

    def signal_handler(signum, frame):
        logger.debug(f"Received signal {signal.Signals(signum).name}, initiating graceful shutdown")
        shutdown_event.set()

    # Register signal handlers for graceful shutdown
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Start scheduler and log status information
    scheduler.start()
    logger.debug("Epic Games scheduler started successfully")
    logger.debug(f"Current time: {datetime.now(TIMEZONE).strftime('%Y-%m-%d %H:%M:%S %Z')}")

    # Log next execution times for all scheduled jobs
    for j in scheduler.get_jobs():
        if next_run := j.next_run_time:
            logger.debug(
                f"Next execution scheduled: {next_run.strftime('%Y-%m-%d %H:%M:%S %Z')} (job_id: {j.id})"
            )

    # Keep scheduler running until shutdown signal received
    logger.debug("Scheduler is running, send SIGINT or SIGTERM to stop gracefully")
    try:
        await shutdown_event.wait()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        scheduler.shutdown(wait=True)
        logger.success("Scheduler stopped gracefully")


if __name__ == '__main__':
    asyncio.run(deploy())
