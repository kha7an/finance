"""Telegram-verified identity; budget access is checked on every request."""

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl


def validate_init_data(raw, bot_token, now=None):
    try:
        pairs = parse_qsl(raw, strict_parsing=True)
        data = dict(pairs)
        if not bot_token or len(data) != len(pairs):
            raise ValueError
        supplied = data.pop("hash")
        check = "\n".join(f"{key}={value}" for key, value in sorted(data.items()))
        secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, supplied):
            raise ValueError
        timestamp = int(data["auth_date"])
        age = (time.time() if now is None else now) - timestamp
        if age < -30 or age > 600:
            raise ValueError
        user = json.loads(data["user"])
        user_id = int(user["id"])
        if user_id <= 0:
            raise ValueError
        return user_id
    except (ValueError, KeyError, TypeError):
        raise ValueError("Данные входа Telegram недействительны или устарели.") from None
