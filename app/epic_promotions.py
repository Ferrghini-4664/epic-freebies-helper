import hashlib
import json
from datetime import datetime, timedelta, timezone

URL_PROMOTIONS = "https://store-site-backend-static.ak.epicgames.com/freeGamesPromotions"
PROMOTION_PARAMS = {"local": "zh-CN"}


def active_free_offers(payload, now=None):
    """Yield catalog entries and durations for active, zero-priced promotions."""
    now = now or datetime.now(timezone.utc)
    if payload.get("errors"):
        raise ValueError("Epic returned catalog errors")
    elements = payload["data"]["Catalog"]["searchStore"]["elements"]
    if not isinstance(elements, list):
        raise ValueError("Invalid Epic catalog")
    for game in elements:
        for group in (game.get("promotions") or {}).get("promotionalOffers") or []:
            for offer in group.get("promotionalOffers") or []:
                if (offer.get("discountSetting") or {}).get("discountPercentage") != 0:
                    continue
                try:
                    start = datetime.fromisoformat(offer["startDate"])
                    end = datetime.fromisoformat(offer["endDate"])
                    if start <= now < end:
                        window = [game["namespace"], game["id"],
                                  start.astimezone(timezone.utc).isoformat(),
                                  end.astimezone(timezone.utc).isoformat()]
                        enriched = dict(game)
                        enriched["claim_key"] = hashlib.sha256(json.dumps(window).encode()).hexdigest()
                        enriched["claim_end"] = end.astimezone(timezone.utc).isoformat()
                        yield enriched, end - start
                except (KeyError, TypeError, ValueError):
                    continue


def has_short_offer(payload, now=None):
    return any(duration <= timedelta(hours=48) for _, duration in active_free_offers(payload, now))
