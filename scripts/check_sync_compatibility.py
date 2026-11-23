import asyncio
import inspect
import runpy
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from epic_promotions import has_short_offer
from services import epic_games_service

NOW = datetime.now(timezone.utc)


def offer(start, end, price):
    return {
        "startDate": (NOW + timedelta(hours=start)).isoformat(),
        "endDate": (NOW + timedelta(hours=end)).isoformat(),
        "discountSetting": {"discountPercentage": price},
    }


def game(identity, *groups):
    return {
        "id": identity,
        "namespace": "fixture",
        "title": identity,
        "description": "Synthetic promotion",
        "offerType": "BASE_GAME",
        "productSlug": identity,
        "promotions": {
            "promotionalOffers": [{"promotionalOffers": group} for group in groups]
        },
    }


def check(games, expected_ids, expected_short):
    payload = {"data": {"Catalog": {"searchStore": {"elements": games}}}}
    if has_short_offer(payload, NOW) != expected_short:
        raise AssertionError("Detection result differs from fixture expectation")
    with tempfile.TemporaryDirectory() as cache, patch.object(
        epic_games_service, "RUNTIME_DIR", Path(cache)
    ), patch.object(epic_games_service.httpx, "get") as request:
        request.return_value.json.return_value = payload
        actual = epic_games_service.get_promotions()
    if [item.id for item in actual] != expected_ids:
        raise AssertionError("Collection parser result differs from fixture expectation")


weekly = game("weekly", [offer(-167, 1, 0)])
daily_window = offer(-12, 12, 0)
daily = game("daily", [offer(-12, 12, 25)], [daily_window, daily_window])
expired = game("expired", [offer(-24, 0, 0)])
future = game("future", [offer(1, 25, 0)])
check([weekly, expired, future], ["weekly"], False)
check([weekly, daily, expired, future], ["weekly", "daily"], True)

# Load the actual CLI entry point and inspect its coroutine without opening a browser.
with patch.object(asyncio, "run") as run:
    runpy.run_path("app/deploy.py", run_name="__main__")
    run.assert_called_once()
    entry = run.call_args.args[0]
    try:
        if not inspect.iscoroutine(entry):
            raise AssertionError("Deployment entry point did not create a coroutine")
    finally:
        if inspect.iscoroutine(entry):
            entry.close()

print("Weekly, daily, and deployment compatibility checks passed")
