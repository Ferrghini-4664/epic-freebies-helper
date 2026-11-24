"""Anonymous catalog check for short giveaways and previously pending weekly offers."""
import json
import os
import time
from datetime import timedelta
from types import SimpleNamespace
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import urlopen

from claim_state import ClaimState, account_key
from epic_promotions import PROMOTION_PARAMS, URL_PROMOTIONS, active_free_offers


def should_claim(payload, state, emails, now=None):
    active = list(active_free_offers(payload, now))
    promotions = [SimpleNamespace(**game) for game, _ in active]
    owned_accounts = {
        account_key(email) for email in emails[:3]
        if state.all_confirmed(email, promotions, now)
    }
    if os.getenv("EPIC_STOP_IF_TWO_OF_THREE_OWNED") == "true" and len(owned_accounts) >= 2:
        return False
    short = [SimpleNamespace(**game) for game, duration in active if duration <= timedelta(hours=48)]
    retry_failed = os.getenv("EPIC_RETRY_FAILED_ACCOUNTS", "true") == "true"
    return state.needs_short_claim(emails, short, now, retry_failed=retry_failed) or (
        retry_failed and state.has_due_retry(promotions, now, emails=emails)
    )


def account_emails():
    raw = os.getenv("EPIC_ACCOUNTS", "").strip()
    if raw:
        lines = [line.strip() for line in raw.splitlines() if line.strip()]
        if any(":" not in line or not line.split(":", 1)[0].strip() for line in lines):
            raise ValueError("Invalid multi-account configuration")
        return [line.split(":", 1)[0].strip() for line in lines]
    email = os.getenv("EPIC_EMAIL", "").strip()
    return [email] if email else []


def main():
    state = ClaimState(os.environ["EPIC_CLAIM_STATE_PATH"])
    url = URL_PROMOTIONS + "?" + urlencode(PROMOTION_PARAMS)
    for attempt in range(3):
        try:
            with urlopen(url, timeout=15) as response:
                found = should_claim(json.load(response), state, account_emails())
            break
        except (URLError, TimeoutError, KeyError, TypeError, ValueError):
            if attempt == 2:
                raise SystemExit("Promotion/retry detection failed after three attempts") from None
            time.sleep(5 * (attempt + 1))
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        print("found=" + str(found).lower(), file=output)


if __name__ == "__main__":
    main()
