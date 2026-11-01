from types import SimpleNamespace

import pytest

from budget_bot.telegram_planning import TelegramPlanning


@pytest.mark.parametrize(
    "url,miniapp", [("https://budget.example/dashboard", True), ("http://127.0.0.1:8000/dashboard", False)]
)
def test_web_opens_https_as_mini_app_and_preserves_local_browser_login(url, miniapp):
    messages, requests, tokens = [], [], []
    storage = SimpleNamespace(
        actor_id="telegram:123", issue_web_token=lambda actor: tokens.append(actor) or "one-time"
    )
    bot = SimpleNamespace(context=SimpleNamespace(storage=storage, settings=SimpleNamespace(web_base_url=url)))
    bot._api = lambda method, payload: requests.append((method, payload))
    bot._send_message = lambda chat, text, reply_markup=None: messages.append((chat, text, reply_markup))
    assert TelegramPlanning(bot).command(123, "/web")
    if miniapp:
        assert not tokens
        assert requests == [
            (
                "setChatMenuButton",
                {"chat_id": 123, "menu_button": {"type": "web_app", "text": "Бюджет", "web_app": {"url": url}}},
            )
        ]
        assert messages[0][2]["inline_keyboard"][0][0]["web_app"]["url"] == url
        assert "one-time" not in messages[0][1]
    else:
        assert not requests
        assert tokens == ["telegram:123"]
        assert messages[0][1].endswith("#login=one-time")


def test_mini_app_cannot_be_opened_for_another_chat():
    bot = SimpleNamespace(
        context=SimpleNamespace(
            storage=SimpleNamespace(actor_id="telegram:123"),
            settings=SimpleNamespace(web_base_url="https://budget.example/dashboard"),
        )
    )
    with pytest.raises(ValueError, match="личном"):
        TelegramPlanning(bot).command(124, "/web")
