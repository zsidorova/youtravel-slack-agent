"""
YouTravel.me — Slack-агент (прямая интеграция со Slack через slack_bolt).

Архитектура:
    Slack App (бот в воркспейсе)
        |
        ↓  Events API
    Render: FastAPI + slack_bolt  ── HTTP ──>  YouTravel API
        |
        ↓  chat.postMessage
    Тот же канал в Slack

Что делает сейчас:
    - Слушает событие `app_mention` (когда в канале пишут @имя_бота)
    - Идёт за фиксированным JSON в YouTravel API
    - Достаёт все `title` из `items`
    - Постит нумерованный список туров в тот же канал

Куда расти (комментарии в коде помечены `# TODO step N`):
    step 2 — LLM-парсинг свободного запроса в фильтры
    step 3 — обращение к разным URL (другой агент на собственном сервере)
    step 4 — саммари по каждому туру через Claude
    step 5 — PDF в брендстиле + загрузка в Slack

Environment variables (задаются в Render → Environment):
    SLACK_BOT_TOKEN          (обязательно)  xoxb-...   — из Slack App: OAuth & Permissions
    SLACK_SIGNING_SECRET     (обязательно)  ...       — из Slack App: Basic Information
    YOUTRAVEL_URL            (опционально)            — переопределить URL к API
    HTTP_TIMEOUT             (опционально, по умолч. 20)
    PORT                     (опционально, по умолч. 8000) — Render проставляет автоматически

Запуск локально:
    pip install -r requirements.txt
    export SLACK_BOT_TOKEN=xoxb-...
    export SLACK_SIGNING_SECRET=...
    uvicorn slack_agent:api --port 8000

Production (Render):
    Build  : pip install -r requirements.txt
    Start  : uvicorn slack_agent:api --host 0.0.0.0 --port $PORT
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx
from fastapi import FastAPI, Request
from slack_bolt import App as SlackApp
from slack_bolt.adapter.fastapi import SlackRequestHandler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("slack_agent")


# --- Конфигурация ----------------------------------------------------------

DEFAULT_URL = (
    "http://168.119.244.229/api/v2/serp/tours"
    "?available_spaces=1"
    "&currency=rub"
    "&group_size=10"
    "&lang=ru"
    "&sort_dir=desc"
    "&take=20"
    "&is_period_strict=1"
    "&period%5B0%5D%5Bfrom%5D=24.05.2026"
    "&period%5B0%5D%5Bto%5D=25.05.2026"
    "&languages%5B0%5D=1151"
    "&sort_by=rank_no_resident"
)
YOUTRAVEL_URL = os.environ.get("YOUTRAVEL_URL", DEFAULT_URL)
HTTP_TIMEOUT = float(os.environ.get("HTTP_TIMEOUT", "20"))

SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
SLACK_SIGNING_SECRET = os.environ.get("SLACK_SIGNING_SECRET")


# --- Парсер JSON-ответа -----------------------------------------------------

def extract_items(payload: Any) -> list[dict[str, Any]]:
    """Достаёт массив туров. Покрывает несколько частых структур JSON."""
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if not isinstance(payload, dict):
        return []

    direct_keys = ("items", "tours", "results", "list")
    for key in direct_keys:
        v = payload.get(key)
        if isinstance(v, list) and v:
            return [x for x in v if isinstance(x, dict)]

    for wrapper in ("data", "response", "result", "payload"):
        inner = payload.get(wrapper)
        if isinstance(inner, list) and inner:
            return [x for x in inner if isinstance(x, dict)]
        if isinstance(inner, dict):
            for key in direct_keys:
                v = inner.get(key)
                if isinstance(v, list) and v:
                    return [x for x in v if isinstance(x, dict)]
    return []


def extract_title(item: dict[str, Any]) -> str | None:
    """title может быть строкой или мультиязычным объектом {ru: ..., en: ...}."""
    for key in ("title", "name", "tour_title"):
        v = item.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, dict):
            for lang in ("ru", "en", "default"):
                if isinstance(v.get(lang), str) and v[lang].strip():
                    return v[lang].strip()
    return None


# --- HTTP-вызов YouTravel ---------------------------------------------------

def fetch_tours(url: str = YOUTRAVEL_URL) -> tuple[list[dict[str, Any]], str | None]:
    """Возвращает (items, error). При успехе error=None."""
    # TODO step 3: вместо одного DEFAULT_URL — роутинг по типу запроса
    # (если запрос про X → URL_X, если про Y → URL_Y).
    try:
        with httpx.Client(timeout=HTTP_TIMEOUT, follow_redirects=True) as http:
            r = http.get(url, headers={"Accept": "application/json"})
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPStatusError as e:
        return [], f"API ответил {e.response.status_code}"
    except httpx.HTTPError as e:
        return [], f"Не смог достучаться до API: {e!s}"
    except ValueError:
        return [], "API вернул не-JSON ответ"

    items = extract_items(data)
    if not items:
        return [], "В ответе API не нашлось списка туров"
    return items, None


def format_text(items: list[dict[str, Any]]) -> str:
    """Текст под Slack: нумерованный список title'ов."""
    # TODO step 4: добавить саммари по каждому туру (Claude API).
    titles = [t for t in (extract_title(i) for i in items) if t]
    if not titles:
        return "Туры найдены, но без названий — проверь структуру JSON."
    head = f"Найдено туров: {len(titles)}"
    lines = [f"{i+1}. {t}" for i, t in enumerate(titles)]
    return head + "\n" + "\n".join(lines)


# --- Slack ------------------------------------------------------------------

if not SLACK_BOT_TOKEN or not SLACK_SIGNING_SECRET:
    log.warning(
        "SLACK_BOT_TOKEN или SLACK_SIGNING_SECRET не заданы — Slack-эндпоинт работать не будет. "
        "Это норм для локального теста /ask, но в Render обязательно проставь обе переменные."
    )

slack_app = SlackApp(
    token=SLACK_BOT_TOKEN or "xoxb-placeholder-for-local-dev",
    signing_secret=SLACK_SIGNING_SECRET or "placeholder",
    # Не дёргать Slack auth.test при старте — иначе деплой падает,
    # если токен ещё не пробросили в Environment Variables.
    token_verification_enabled=bool(SLACK_BOT_TOKEN),
    request_verification_enabled=bool(SLACK_SIGNING_SECRET),
)


@slack_app.event("app_mention")
def handle_mention(event: dict, say, logger) -> None:
    """Реакция на @упоминание бота в канале."""
    user_text = event.get("text", "")
    channel = event.get("channel")
    logger.info(f"mention from {event.get('user')} in {channel}: {user_text!r}")

    # TODO step 2: распарсить user_text через Claude → передать фильтры в fetch_tours.
    items, err = fetch_tours()
    if err:
        say(text=f":warning: {err}", channel=channel)
        return
    say(text=format_text(items), channel=channel)
    # TODO step 5: вместо say(text=...) — files_upload PDF с подборкой.


# Игнорируем сообщения бота (на случай если позже подпишемся на message events)
@slack_app.event("message")
def ignore_bot_messages(event, logger) -> None:
    if event.get("bot_id"):
        return
    # Можно расширить: например, отвечать на DM боту.
    logger.debug(f"message event ignored: {event.get('text', '')[:60]}")


# --- FastAPI приложение -----------------------------------------------------

api = FastAPI(title="YouTravel Slack Agent")
slack_handler = SlackRequestHandler(slack_app)


@api.post("/slack/events")
async def slack_events(req: Request):
    """Сюда Slack шлёт события (упоминания и т.д.). Этот URL прописывается
    в Slack App: Event Subscriptions → Request URL."""
    return await slack_handler.handle(req)


@api.get("/")
def root() -> dict[str, str]:
    return {"status": "ok", "slack_endpoint": "/slack/events", "test_endpoint": "/ask"}


@api.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@api.get("/ask")
def ask_test() -> dict[str, Any]:
    """Тестовый эндпоинт: позволяет проверить, что fetch_tours работает,
    не настраивая Slack. Открой в браузере: https://<your>.onrender.com/ask"""
    items, err = fetch_tours()
    if err:
        return {"error": err, "count": 0}
    titles = [t for t in (extract_title(i) for i in items) if t]
    return {"count": len(titles), "titles": titles, "preview_text": format_text(items)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(api, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
