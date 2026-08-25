from __future__ import annotations

import base64
import json
import re
from abc import ABC, abstractmethod
from datetime import date
from typing import Any, Dict, List, Tuple

import requests

from .categories import CategoryBook
from .models import ParsedScreenshot


class VisionClient(ABC):
    @abstractmethod
    def parse_screenshot(
        self,
        image_content: bytes,
        mime_type: str,
        category_book: CategoryBook,
        screenshot_date: date,
    ) -> ParsedScreenshot:
        raise NotImplementedError

    def parse_screenshots(
        self,
        images: List[Tuple[bytes, str]],
        category_book: CategoryBook,
        screenshot_date: date,
    ) -> ParsedScreenshot:
        if not images:
            raise ValueError("At least one screenshot is required")
        if len(images) == 1:
            image_content, mime_type = images[0]
            return self.parse_screenshot(image_content, mime_type, category_book, screenshot_date)
        raise NotImplementedError("This vision client does not support multiple screenshots")


class MockVisionClient(VisionClient):
    def parse_screenshot(
        self,
        image_content: bytes,
        mime_type: str,
        category_book: CategoryBook,
        screenshot_date: date,
    ) -> ParsedScreenshot:
        del image_content, mime_type, category_book
        return ParsedScreenshot.from_json(
            {
                "bank": "mock",
                "period": {
                    "month": screenshot_date.month,
                    "year": screenshot_date.year,
                    "screenshot_date": screenshot_date.isoformat(),
                },
                "operations": [],
            }
        )

    def parse_screenshots(
        self,
        images: List[Tuple[bytes, str]],
        category_book: CategoryBook,
        screenshot_date: date,
    ) -> ParsedScreenshot:
        del images, category_book
        return ParsedScreenshot.from_json(
            {
                "bank": "mock",
                "period": {
                    "month": screenshot_date.month,
                    "year": screenshot_date.year,
                    "screenshot_date": screenshot_date.isoformat(),
                },
                "operations": [],
            }
        )


class GeminiVisionClient(VisionClient):
    def __init__(self, api_key: str, model: str) -> None:
        if not api_key:
            raise ValueError("GEMINI_API_KEY is required when LLM_PROVIDER=gemini")
        self.api_key = api_key
        self.model = model

    def parse_screenshot(
        self,
        image_content: bytes,
        mime_type: str,
        category_book: CategoryBook,
        screenshot_date: date,
    ) -> ParsedScreenshot:
        return self.parse_screenshots([(image_content, mime_type)], category_book, screenshot_date)

    def parse_screenshots(
        self,
        images: List[Tuple[bytes, str]],
        category_book: CategoryBook,
        screenshot_date: date,
    ) -> ParsedScreenshot:
        prompt = build_prompt(category_book, screenshot_date, screenshot_count=len(images))
        parts: List[Dict[str, Any]] = [{"text": prompt}]
        for image_content, mime_type in images:
            parts.append(
                {
                    "inline_data": {
                        "mime_type": mime_type,
                        "data": base64.b64encode(image_content).decode("ascii"),
                    }
                }
            )
        response = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent",
            params={"key": self.api_key},
            json={
                "contents": [
                    {
                        "role": "user",
                        "parts": parts,
                    }
                ],
                "generationConfig": {
                    "temperature": 0.0,
                    "response_mime_type": "application/json",
                },
            },
            timeout=90,
        )
        _raise_for_provider_status(response, provider="Gemini", auth_env="GEMINI_API_KEY")
        text = _extract_gemini_text(response.json())
        payload = parse_json_text(text)
        return ParsedScreenshot.from_json(payload)


class OpenAIVisionClient(VisionClient):
    def __init__(self, api_key: str, model: str, proxy_url: str = "", timeout_seconds: int = 90) -> None:
        if not api_key:
            raise ValueError("OPENAI_API_KEY is required when LLM_PROVIDER=openai")
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.session = requests.Session()
        if proxy_url:
            self.session.trust_env = False
            self.session.proxies.update({"http": proxy_url, "https": proxy_url})

    def check_model_access(self) -> Dict[str, Any]:
        response = self.session.get(
            f"https://api.openai.com/v1/models/{self.model}",
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=min(self.timeout_seconds, 30),
        )
        _raise_for_provider_status(response, provider="OpenAI", auth_env="OPENAI_API_KEY")
        return response.json()

    def parse_screenshot(
        self,
        image_content: bytes,
        mime_type: str,
        category_book: CategoryBook,
        screenshot_date: date,
    ) -> ParsedScreenshot:
        return self.parse_screenshots([(image_content, mime_type)], category_book, screenshot_date)

    def parse_screenshots(
        self,
        images: List[Tuple[bytes, str]],
        category_book: CategoryBook,
        screenshot_date: date,
    ) -> ParsedScreenshot:
        prompt = build_prompt(category_book, screenshot_date, screenshot_count=len(images))
        content: List[Dict[str, Any]] = [{"type": "input_text", "text": prompt}]
        for image_content, mime_type in images:
            data_url = (
                f"data:{mime_type};base64,"
                f"{base64.b64encode(image_content).decode('ascii')}"
            )
            content.append({"type": "input_image", "image_url": data_url, "detail": "high"})
        response = self.session.post(
            "https://api.openai.com/v1/responses",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.model,
                "input": [
                    {
                        "role": "user",
                        "content": content,
                    }
                ],
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": "bank_operations",
                        "strict": True,
                        "schema": response_schema(category_book),
                    }
                },
            },
            timeout=self.timeout_seconds,
        )
        _raise_for_provider_status(response, provider="OpenAI", auth_env="OPENAI_API_KEY")
        payload = parse_json_text(_extract_openai_text(response.json()))
        return ParsedScreenshot.from_json(payload)


def build_vision_client(
    provider: str,
    openai_api_key: str,
    openai_model: str,
    gemini_api_key: str,
    gemini_model: str,
    openai_proxy_url: str = "",
    openai_timeout_seconds: int = 90,
) -> VisionClient:
    if provider == "mock":
        return MockVisionClient()
    if provider == "openai":
        return OpenAIVisionClient(
            api_key=openai_api_key,
            model=openai_model,
            proxy_url=openai_proxy_url,
            timeout_seconds=openai_timeout_seconds,
        )
    if provider == "gemini":
        return GeminiVisionClient(api_key=gemini_api_key, model=gemini_model)
    raise ValueError(f"Unsupported LLM_PROVIDER: {provider!r}")


def build_prompt(category_book: CategoryBook, screenshot_date: date, screenshot_count: int = 1) -> str:
    batch_rules = ""
    if screenshot_count > 1:
        batch_rules = f"""

Это пачка из {screenshot_count} скриншотов, отправленных одним Telegram-альбомом.
Считай их частями одной непрерывной истории операций в порядке отправки.
Если на следующем скриншоте нет нового заголовка дня, это продолжение предыдущего видимого дня,
а не следующий календарный день.
Если дата дня видна на одном из скриншотов пачки, применяй эту дату ко всем последующим операциям пачки,
пока не появится новый видимый заголовок дня. В таком случае ставь "date_status": "relative".
Не придумывай новую дату вроде следующего дня только потому, что начался новый скриншот.
Не дублируй одну и ту же операцию, если она попала в перекрытие двух скриншотов.
Если одинаковые операции действительно видны несколько раз подряд с тем же названием и суммой, верни их отдельными элементами.
"""
    return f"""
Ты извлекаешь операции из скриншота истории банковских операций.
Верни только JSON без Markdown.

Дата отправки скриншота: {screenshot_date.isoformat()}.
Если на скриншоте написано "Вчера", вычисли дату относительно даты отправки.
Если указан месяц без года, используй год даты отправки.
Если виден заголовок дня вроде "1 августа", эта дата относится ко всем операциям ниже до следующего заголовка дня,
даже если нижние строки далеко от заголовка.
Возвращай операции в том же порядке, как они идут на экране сверху вниз.
Если операция находится под видимым заголовком дня, используй дату этого заголовка и "date_status": "relative", а не "missing".
Если дата операции на скриншоте не видна и нет "Сегодня"/"Вчера"/другого явного указателя даты,
не угадывай дату: верни "date": null и "date_status": "missing".
{batch_rules}

Допустимые категории расходов и подкатегории:
{category_book.expense_prompt_text()}

Допустимые категории доходов:
{category_book.income_prompt_text()}

Правила:
- Извлекай только операции, которые действительно видны на изображении.
- Не восстанавливай обрезанные суммы, названия или даты по догадке.
- Если текст не удается надежно прочитать, не придумывай значение; поставь null для категории/подкатегории или needs_review=true.
- Не придумывай категории и подкатегории вне списков выше.
- Расходы возвращай с отрицательной суммой, доходы с положительной.
- Переводы людям или между счетами помечай type="transfer".
- Для transfer сохраняй направление: исходящий перевод - отрицательная сумма, входящий перевод - положительная сумма.
- Серые суммы справа от заголовка дня ("7 августа", "8 августа") — это итог дня, а не операция: никогда не используй их как amount.
- Для операции бери сумму только из той же строки, что и название операции.
- Кэшбэк, бонусы и желтые бонусные бейджи рядом с операцией не являются отдельными операциями: не добавляй их в operations.
- Если строка явно называется "Кэшбэк", "Бонусы" или похожим образом, верни type="ignore".
- Возвраты денег по покупкам возвращай как income, только если это отдельная зеленая операция возврата, а не бонусный бейдж.
- Непонятные операции помечай type="ignore" или верни category/subcategory null.
- Если банк явно показывает категорию операции под названием магазина, используй ее как сильный сигнал при выборе категории и подкатегории из разрешенного списка.
- Не копируй банковскую категорию напрямую, если такого значения нет в разрешенном справочнике; сопоставь ее с ближайшей допустимой категорией.
- Фастфуд вроде Бургер Кинг, KFC, Вкусно и точка относить к Еда / Фастфуд, если такая подкатегория есть.
- Супермаркеты и продуктовые магазины вроде Пятерочка, Магнит, SPAR, Перекресток относить к Еда / Супермаркеты.
- Такси относить к Транспорт / Такси.
- Метро, автобусы, городской транспорт, электрички и РЖД в городском контексте относить к Транспорт / Местный транспорт.
- Ж/д билеты для поездок относить к Путешествия / Ж/д билеты.
- name должен максимально сохранять название, видимое на скриншоте.
- Разрешается убрать технические префиксы, организационно-правовую форму и очевидный служебный мусор только если смысл названия не меняется.
- Не придумывай полное название организации по сокращению.
- needs_review=true, если сумма читается неоднозначно, дата неоднозначна, категория не определяется или название операции существенно обрезано.

Формат:
{{
  "bank": "tbank|sber|alfa|unknown",
  "period": {{"month": 8, "year": 2026, "screenshot_date": "{screenshot_date.isoformat()}"}},
  "operations": [
    {{
      "date": "YYYY-MM-DD или null если дата не видна",
      "date_status": "visible|relative|missing",
      "name": "Пятёрочка",
      "amount": -79.99,
      "type": "expense|income|transfer|ignore",
      "category": "Еда",
      "subcategory": "Супермаркеты",
      "needs_review": false
    }}
  ]
}}
""".strip()


def parse_json_text(text: str) -> Dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?", "", stripped, flags=re.IGNORECASE).strip()
        stripped = re.sub(r"```$", "", stripped).strip()
    return json.loads(stripped)


def _raise_for_provider_status(response: requests.Response, provider: str, auth_env: str) -> None:
    if response.status_code == 401:
        raise RuntimeError(
            f"{provider} rejected the API key with 401 Unauthorized. "
            f"Check {auth_env} in .env, make sure it is not empty, revoked, or copied with extra spaces."
        )
    if response.status_code == 403:
        detail = _provider_error_detail(response)
        suffix = f" Details: {detail}" if detail else ""
        raise RuntimeError(
            f"{provider} returned 403 Forbidden. Check that the API key has access to the selected model.{suffix}"
        )
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        detail = _provider_error_detail(response)
        if detail:
            raise RuntimeError(f"{provider} request failed: {detail}") from exc
        raise


def _provider_error_detail(response: requests.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text[:500]
    return json.dumps(payload, ensure_ascii=False)[:500]


def _extract_gemini_text(payload: Dict[str, Any]) -> str:
    candidates = payload.get("candidates") or []
    if not candidates:
        raise ValueError("Gemini response has no candidates")
    parts = candidates[0].get("content", {}).get("parts", [])
    text = "".join(part.get("text", "") for part in parts)
    if not text.strip():
        raise ValueError("Gemini response has no text")
    return text


def _extract_openai_text(payload: Dict[str, Any]) -> str:
    if payload.get("output_text"):
        return str(payload["output_text"])

    chunks = []
    for item in payload.get("output", []):
        for content in item.get("content", []):
            if content.get("type") in {"output_text", "text"} and content.get("text"):
                chunks.append(content["text"])
    text = "".join(chunks)
    if not text.strip():
        raise ValueError("OpenAI response has no output text")
    return text


def response_schema(category_book: CategoryBook) -> Dict[str, Any]:
    categories = list(category_book.expense_categories.keys())
    subcategories = sorted({item for values in category_book.expense_categories.values() for item in values})
    income_categories = list(category_book.income_categories)
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["bank", "period", "operations"],
        "properties": {
            "bank": {"type": "string", "enum": ["tbank", "sber", "alfa", "unknown"]},
            "period": {
                "type": "object",
                "additionalProperties": False,
                "required": ["month", "year", "screenshot_date"],
                "properties": {
                    "month": {"type": "integer", "minimum": 1, "maximum": 12},
                    "year": {"type": "integer", "minimum": 2020, "maximum": 2100},
                    "screenshot_date": {"type": "string"},
                },
            },
            "operations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "date",
                        "date_status",
                        "name",
                        "amount",
                        "type",
                        "category",
                        "subcategory",
                        "needs_review",
                    ],
                    "properties": {
                        "date": {"type": ["string", "null"]},
                        "date_status": {
                            "type": "string",
                            "enum": ["visible", "relative", "missing"],
                        },
                        "name": {"type": "string"},
                        "amount": {"type": "number"},
                        "type": {
                            "type": "string",
                            "enum": ["expense", "income", "transfer", "ignore"],
                        },
                        "category": {
                            "anyOf": [
                                {"type": "string", "enum": sorted(set(categories + income_categories))},
                                {"type": "null"},
                            ]
                        },
                        "subcategory": {
                            "anyOf": [
                                {"type": "string", "enum": subcategories},
                                {"type": "null"},
                            ]
                        },
                        "needs_review": {"type": "boolean"},
                    },
                },
            },
        },
    }
