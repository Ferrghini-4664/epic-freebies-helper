import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path


def utcnow():
    return datetime.now(timezone.utc)


def account_key(email):
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()


class ClaimState:
    """Store confirmed offer windows and pending retries without credentials."""

    def __init__(self, path):
        self.path = Path(path)
        self.data = {}
        if self.path.exists():
            try:
                saved = json.loads(self.path.read_text(encoding="utf-8"))
                if saved.get("version") == 1 and isinstance(saved.get("accounts"), dict):
                    for account, records in saved["accounts"].items():
                        if not isinstance(records, dict):
                            continue
                        valid = {}
                        for key, record in records.items():
                            try:
                                end = datetime.fromisoformat(record["end"])
                                after = record.get("retry_after", "")
                                if end.tzinfo is None or type(record["confirmed"]) is not bool:
                                    continue
                                if after and datetime.fromisoformat(after).tzinfo is None:
                                    continue
                                valid[key] = record
                            except (KeyError, TypeError, ValueError, AttributeError):
                                continue
                        self.data[account] = valid
            except (ValueError, AttributeError):
                pass

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"version": 1, "accounts": self.data}), encoding="utf-8")
        temporary.replace(self.path)

    def register(self, emails, promotions, now=None):
        now = now or utcnow()
        for account in list(self.data):
            records = self.data[account]
            for key in list(records):
                if datetime.fromisoformat(records[key]["end"]) <= now:
                    del records[key]
            if not records:
                del self.data[account]
        for email in emails:
            records = self.data.setdefault(account_key(email), {})
            for game in promotions:
                if game.claim_key and game.claim_end:
                    records.setdefault(game.claim_key, {
                        "end": game.claim_end, "confirmed": False, "retry_after": "",
                    })
        self.save()

    def pending(self, email, promotions, *, automatic=False, now=None):
        now = now or utcnow()
        records = self.data.get(account_key(email), {})
        result = []
        for game in promotions:
            record = records.get(game.claim_key)
            if not record or record["confirmed"]:
                continue
            if datetime.fromisoformat(record["end"]) <= now:
                continue
            retry_after = record.get("retry_after")
            if automatic and retry_after and datetime.fromisoformat(retry_after) > now:
                continue
            result.append(game)
        return result

    def attempted(self, email, promotions, *, hours=4, now=None):
        retry_after = ((now or utcnow()) + timedelta(hours=hours)).isoformat()
        records = self.data[account_key(email)]
        for game in promotions:
            if not records[game.claim_key]["confirmed"]:
                records[game.claim_key]["retry_after"] = retry_after
        self.save()

    def confirm(self, email, promotions, order_keys):
        records = self.data[account_key(email)]
        for game in promotions:
            if (game.namespace, game.id) in order_keys:
                records[game.claim_key]["confirmed"] = True
                records[game.claim_key]["retry_after"] = ""
        self.save()

    def all_confirmed(self, email, promotions, now=None):
        now = now or utcnow()
        records = self.data.get(account_key(email), {})
        return bool(promotions) and all(
            game.claim_key in records
            and records[game.claim_key]["confirmed"]
            and datetime.fromisoformat(records[game.claim_key]["end"]) > now
            for game in promotions
        )

    def needs_short_claim(self, emails, promotions, now=None, *, retry_failed=True):
        now = now or utcnow()
        for email in emails:
            records = self.data.get(account_key(email), {})
            for game in promotions:
                record = records.get(game.claim_key)
                if record is None:
                    return True
                if retry_failed and not record["confirmed"]:
                    after = record.get("retry_after")
                    if not after or datetime.fromisoformat(after) <= now:
                        return True
        return False

    def has_due_retry(self, promotions, now=None, emails=None):
        # Only windows already attempted/registered by a full run qualify.
        now = now or utcnow()
        active_keys = {game.claim_key for game in promotions if game.claim_key}
        allowed = {account_key(email) for email in emails} if emails is not None else set(self.data)
        for account, records in self.data.items():
            if account not in allowed:
                continue
            for key, record in records.items():
                if key not in active_keys or record["confirmed"]:
                    continue
                after = record.get("retry_after")
                if datetime.fromisoformat(record["end"]) > now and (
                    not after or datetime.fromisoformat(after) <= now
                ):
                    return True
        return False
