"""
YouTravel.me — Slack-агент (прямая интеграция через slack_bolt).

Что делает на этой итерации:
    - Слушает app_mention (упоминание @бота в каналах)
    - Слушает message.im (личные сообщения боту)
    - Идёт за JSON в YouTravel API
    - Берёт первые 5 туров из items
    - Постит структурированное саммари в Slack
    - Генерирует PDF в брендстиле YouTravel и прикладывает к ответу

Куда расти:
    - Claude парсит свободный запрос → подставляет фильтры в URL
    - Несколько источников JSON (агент на сервере YouTravel)
    - Текстовое описание по каждому туру через Claude

Environment variables (задаются в Render → Environment):
    SLACK_BOT_TOKEN          (обязательно)  xoxb-...   из Slack App: OAuth & Permissions
    SLACK_SIGNING_SECRET     (обязательно)             из Slack App: Basic Information
    YOUTRAVEL_URL            (опц.)                    переопределить URL к API
    HTTP_TIMEOUT             (опц., по умолч. 20)
    PORT                     (опц., по умолч. 8000) — Render проставляет автоматически

Production (Render):
    Build  : pip install -r requirements.txt
    Start  : uvicorn slack_agent:api --host 0.0.0.0 --port $PORT
"""

from __future__ import annotations

import io
import logging
import os
import tempfile
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import FastAPI, Request
from slack_bolt import App as SlackApp
from slack_bolt.adapter.fastapi import SlackRequestHandler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("slack_agent")


# ============================================================================
# Конфигурация
# ============================================================================

DEFAULT_URL = (
    "http://168.119.244.229/api/v2/serp/tours"
    "?available_spaces=1&currency=rub&group_size=10&lang=ru&sort_dir=desc&take=20"
    "&is_period_strict=1"
    "&period%5B0%5D%5Bfrom%5D=24.05.2026&period%5B0%5D%5Bto%5D=25.05.2026"
    "&languages%5B0%5D=1151&sort_by=rank_no_resident"
)
YOUTRAVEL_URL = os.environ.get("YOUTRAVEL_URL", DEFAULT_URL)
HTTP_TIMEOUT = float(os.environ.get("HTTP_TIMEOUT", "20"))

SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
SLACK_SIGNING_SECRET = os.environ.get("SLACK_SIGNING_SECRET")

TOP_N = 5  # сколько туров возвращаем

# Брендстиль YouTravel (из брендбука)
BRAND = {
    "purple": "#771F96",
    "purple_dark": "#582868",
    "green": "#C9E33A",
    "green_pale": "#F0F6DD",
    "night": "#242A37",
    "white": "#FFFFFF",
    "red": "#F84565",
    "muted": "#727281",
}
TAGLINE = "Создавай впечатления, проживай истории"
BRAND_NAME = "YouTravel.me"


# ============================================================================
# Парсер JSON
# ============================================================================

def extract_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if not isinstance(payload, dict):
        return []

    for key in ("items", "tours", "results", "list"):
        v = payload.get(key)
        if isinstance(v, list) and v:
            return [x for x in v if isinstance(x, dict)]
    for wrapper in ("data", "response", "result", "payload"):
        inner = payload.get(wrapper)
        if isinstance(inner, list) and inner:
            return [x for x in inner if isinstance(x, dict)]
        if isinstance(inner, dict):
            for key in ("items", "tours", "results", "list"):
                v = inner.get(key)
                if isinstance(v, list) and v:
                    return [x for x in v if isinstance(x, dict)]
    return []


def _maybe_str(v: Any) -> str | None:
    if isinstance(v, str) and v.strip():
        return v.strip()
    if isinstance(v, dict):
        for lang in ("ru", "en", "default"):
            val = v.get(lang)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return None


def _extract_first_date_group(item: dict) -> dict:
    """dates.group[0] — но защитимся от разных форм."""
    dates = item.get("dates")
    if not isinstance(dates, dict):
        return {}
    group = dates.get("group")
    if isinstance(group, list) and group:
        first = group[0]
        return first if isinstance(first, dict) else {}
    if isinstance(group, dict):
        return group
    return {}


def _fmt_unix(ts: Any) -> str | None:
    if ts in (None, "", 0):
        return None
    try:
        ts = float(ts)
    except (TypeError, ValueError):
        return None
    if ts > 10**12:
        ts = ts / 1000.0   # миллисекунды → секунды
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%d.%m.%Y")
    except (OSError, ValueError):
        return None


def _fmt_price(p: Any) -> str | None:
    if p in (None, "", 0):
        return None
    try:
        p = int(float(p))
    except (TypeError, ValueError):
        return None
    return f"{p:,}".replace(",", " ") + " ₽"


def _extract_region(item: dict) -> str | None:
    """regions, иначе countries. Допускаются строка, список, dict с name."""
    for key in ("regions", "countries"):
        val = item.get(key)
        if not val:
            continue
        if isinstance(val, str) and val.strip():
            return val.strip()
        if isinstance(val, list) and val:
            names = []
            for x in val:
                if isinstance(x, str) and x.strip():
                    names.append(x.strip())
                elif isinstance(x, dict):
                    n = _maybe_str(x.get("name") or x.get("title"))
                    if n:
                        names.append(n)
            if names:
                return ", ".join(names)
        if isinstance(val, dict):
            n = _maybe_str(val.get("name") or val.get("title"))
            if n:
                return n
    return None


def _extract_photo_url(item: dict) -> str | None:
    """Самые частые поля с фото."""
    for key in ("photo", "cover", "image", "preview", "thumbnail"):
        v = item.get(key)
        if isinstance(v, str) and v.startswith(("http://", "https://")):
            return v
        if isinstance(v, dict):
            u = v.get("url") or v.get("src") or v.get("link")
            if isinstance(u, str) and u.startswith(("http://", "https://")):
                return u
    for key in ("photos", "images", "gallery"):
        v = item.get(key)
        if isinstance(v, list) and v:
            first = v[0]
            if isinstance(first, str) and first.startswith(("http://", "https://")):
                return first
            if isinstance(first, dict):
                u = first.get("url") or first.get("src") or first.get("link")
                if isinstance(u, str) and u.startswith(("http://", "https://")):
                    return u
    return None


def summarize_tour(item: dict[str, Any]) -> dict[str, Any]:
    """Из сырого элемента items[] вытащить нормализованные поля."""
    g = _extract_first_date_group(item)
    title = _maybe_str(item.get("title") or item.get("name"))
    tour_id = item.get("id") or item.get("tour_id") or item.get("uuid")
    return {
        "title": title or "Без названия",
        "id": tour_id,
        "url": f"https://youtravel.me/tours/{tour_id}" if tour_id else None,
        "region": _extract_region(item),
        "date_from": _fmt_unix(g.get("date_from")),
        "date_to": _fmt_unix(g.get("date_to")),
        "price": _fmt_price(g.get("price")),
        "photo": _extract_photo_url(item),
    }


# ============================================================================
# Загрузка туров из YouTravel API
# ============================================================================

def fetch_tours(url: str = YOUTRAVEL_URL) -> tuple[list[dict[str, Any]], str | None]:
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


# ============================================================================
# Форматирование текстового ответа в Slack (Block Kit)
# ============================================================================

def format_text(tours: list[dict[str, Any]]) -> str:
    """Plain-text версия (для fallback и initial_comment файла)."""
    if not tours:
        return "Подходящих туров не нашлось."
    lines = [f"*Топ-{len(tours)} туров под ваш запрос*", ""]
    for i, t in enumerate(tours, 1):
        dates = (
            f"{t['date_from']} – {t['date_to']}"
            if t.get("date_from") and t.get("date_to") else "—"
        )
        lines += [
            f"*{i}. {t['title']}*",
            f"   Ссылка: {t['url'] or '—'}",
            f"   Направление: {t.get('region') or '—'}",
            f"   Даты: {dates}",
            f"   Цена: {t.get('price') or '—'}",
            "",
        ]
    return "\n".join(lines).rstrip()


def format_blocks(tours: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Slack Block Kit — нативное форматирование с гиперссылками."""
    if not tours:
        return [{"type": "section",
                 "text": {"type": "mrkdwn", "text": "Подходящих туров не нашлось."}}]

    blocks: list[dict[str, Any]] = [
        {"type": "header",
         "text": {"type": "plain_text", "text": f"Топ-{len(tours)} туров под ваш запрос"}}
    ]
    for i, t in enumerate(tours, 1):
        dates = (
            f"{t['date_from']} – {t['date_to']}"
            if t.get("date_from") and t.get("date_to") else "—"
        )
        text = (
            f"*{i}. <{t['url']}|{t['title']}>*"
            if t.get("url") else f"*{i}. {t['title']}*"
        )
        text += (
            f"\n• *Направление:* {t.get('region') or '—'}"
            f"\n• *Даты:* {dates}"
            f"\n• *Цена:* {t.get('price') or '—'}"
        )
        blocks.append({"type": "section",
                       "text": {"type": "mrkdwn", "text": text}})
        blocks.append({"type": "divider"})
    return blocks


# ============================================================================
# Генерация PDF в брендстиле YouTravel
# ============================================================================

_PDF_FONT_REGULAR = "Helvetica"
_PDF_FONT_BOLD = "Helvetica-Bold"
_PDF_FONT_ITALIC = "Helvetica-Oblique"


def _register_cyrillic_fonts() -> None:
    """Ищем DejaVuSans (есть на Render/Linux). Если нашли — переключаем шрифты."""
    global _PDF_FONT_REGULAR, _PDF_FONT_BOLD, _PDF_FONT_ITALIC
    if _PDF_FONT_REGULAR != "Helvetica":
        return  # уже зарегистрированы

    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    candidate_dirs = [
        "/usr/share/fonts/truetype/dejavu",        # Debian/Ubuntu (Render)
        "/usr/share/fonts/dejavu",
        "/Library/Fonts",                          # macOS
        "/System/Library/Fonts/Supplemental",
        "/usr/share/fonts/TTF",
    ]
    regular = bold = italic = None
    for d in candidate_dirs:
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            p = os.path.join(d, name)
            low = name.lower()
            if low == "dejavusans.ttf" and regular is None:
                regular = p
            elif low == "dejavusans-bold.ttf" and bold is None:
                bold = p
            elif low == "dejavusans-oblique.ttf" and italic is None:
                italic = p
        if regular:
            break

    if not regular:
        log.warning("DejaVuSans не найден — PDF останется на Helvetica (без кириллицы).")
        return

    try:
        pdfmetrics.registerFont(TTFont("YT-Sans", regular))
        if bold:
            pdfmetrics.registerFont(TTFont("YT-Sans-Bold", bold))
        if italic:
            pdfmetrics.registerFont(TTFont("YT-Sans-Italic", italic))
        _PDF_FONT_REGULAR = "YT-Sans"
        _PDF_FONT_BOLD = "YT-Sans-Bold" if bold else "YT-Sans"
        _PDF_FONT_ITALIC = "YT-Sans-Italic" if italic else "YT-Sans"
        log.info(f"PDF fonts registered: {regular}")
    except Exception as e:
        log.warning(f"font registration failed: {e}")


def generate_pdf(tours: list[dict[str, Any]], output_path: str) -> str:
    """Брендированный PDF с обложкой и страницей на тур.
    Возвращает путь к собранному файлу."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.platypus import (
        BaseDocTemplate, Frame, PageTemplate, PageBreak, Paragraph,
        Spacer, Table, TableStyle, Image as RLImage,
    )

    _register_cyrillic_fonts()

    purple = colors.HexColor(BRAND["purple"])
    purple_dark = colors.HexColor(BRAND["purple_dark"])
    green = colors.HexColor(BRAND["green"])
    green_pale = colors.HexColor(BRAND["green_pale"])
    night = colors.HexColor(BRAND["night"])
    muted = colors.HexColor(BRAND["muted"])

    base = getSampleStyleSheet()
    style_h1 = ParagraphStyle("h1", parent=base["Heading1"], fontName=_PDF_FONT_BOLD,
                              fontSize=28, leading=32, textColor=purple, spaceAfter=10)
    style_h2 = ParagraphStyle("h2", parent=base["Heading2"], fontName=_PDF_FONT_BOLD,
                              fontSize=18, leading=22, textColor=purple, spaceAfter=8)
    style_lead = ParagraphStyle("lead", parent=base["Italic"], fontName=_PDF_FONT_ITALIC,
                                fontSize=14, leading=18, textColor=purple_dark, spaceAfter=6)
    style_body = ParagraphStyle("body", parent=base["BodyText"], fontName=_PDF_FONT_REGULAR,
                                fontSize=11, leading=15, textColor=night)
    style_meta = ParagraphStyle("meta", parent=style_body, textColor=muted, fontSize=9)
    style_link = ParagraphStyle("link", parent=style_body, fontName=_PDF_FONT_BOLD,
                                fontSize=12, textColor=purple)

    def draw_brand_header(canvas, doc):
        """Фиолетовая полоска сверху + 'YouTravel.me' + слоган внизу."""
        canvas.saveState()
        w, h = A4
        # Верхняя плашка
        canvas.setFillColor(purple)
        canvas.rect(0, h - 1.2 * cm, w, 1.2 * cm, fill=1, stroke=0)
        canvas.setFillColor(colors.white)
        canvas.setFont(_PDF_FONT_BOLD, 12)
        canvas.drawString(2 * cm, h - 0.8 * cm, BRAND_NAME)
        # Зелёная стрелка-акцент
        canvas.setFillColor(green)
        canvas.rect(2 * cm + 3.2 * cm, h - 0.95 * cm, 0.6 * cm, 0.25 * cm, fill=1, stroke=0)

        # Низ
        canvas.setStrokeColor(purple)
        canvas.setLineWidth(0.5)
        canvas.line(2 * cm, 1.2 * cm, w - 2 * cm, 1.2 * cm)
        canvas.setFillColor(muted)
        canvas.setFont(_PDF_FONT_REGULAR, 8)
        canvas.drawString(2 * cm, 0.8 * cm, f"{BRAND_NAME}  ·  {TAGLINE}")
        canvas.drawRightString(w - 2 * cm, 0.8 * cm, f"стр. {doc.page}")
        canvas.restoreState()

    doc = BaseDocTemplate(
        output_path,
        pagesize=A4,
        leftMargin=2 * cm, rightMargin=2 * cm,
        topMargin=2 * cm, bottomMargin=2 * cm,
        title="Подборка туров · YouTravel.me",
        author="YouTravel.me",
    )
    frame = Frame(doc.leftMargin, doc.bottomMargin,
                  doc.width, doc.height, id="main")
    doc.addPageTemplates([PageTemplate(id="brand", frames=[frame],
                                       onPage=draw_brand_header)])

    story: list[Any] = []

    # ---- Обложка ----
    story.append(Spacer(1, 4 * cm))
    story.append(Paragraph("Авторская<br/>подборка туров", style_h1))
    story.append(Paragraph(TAGLINE, style_lead))
    story.append(Spacer(1, 0.6 * cm))
    story.append(Paragraph(
        f"Подготовлено {datetime.now().strftime('%d.%m.%Y')}", style_meta))
    story.append(PageBreak())

    # ---- По одной странице на тур ----
    for i, t in enumerate(tours, 1):
        story.append(Paragraph(f"{i}. {t['title']}", style_h2))
        story.append(Spacer(1, 0.2 * cm))

        # Изображение
        photo_flowable = _fetch_image_flowable(t.get("photo"), max_w_cm=16)
        if photo_flowable is not None:
            story.append(photo_flowable)
            story.append(Spacer(1, 0.4 * cm))

        # Таблица с данными тура
        dates = (
            f"{t['date_from']} – {t['date_to']}"
            if t.get("date_from") and t.get("date_to") else "—"
        )
        data = [
            ["Направление", t.get("region") or "—"],
            ["Даты", dates],
            ["Цена", t.get("price") or "—"],
        ]
        info = Table(data, colWidths=[4 * cm, 12 * cm])
        info.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (0, -1), green_pale),
            ("TEXTCOLOR", (0, 0), (0, -1), purple_dark),
            ("FONTNAME", (0, 0), (0, -1), _PDF_FONT_BOLD),
            ("FONTNAME", (1, 0), (1, -1), _PDF_FONT_REGULAR),
            ("FONTSIZE", (0, 0), (-1, -1), 11),
            ("TOPPADDING", (0, 0), (-1, -1), 8),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ("LEFTPADDING", (0, 0), (-1, -1), 12),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LINEBELOW", (0, 0), (-1, -2), 0.3, colors.white),
        ]))
        story.append(info)

        # CTA-ссылка
        if t.get("url"):
            story.append(Spacer(1, 0.6 * cm))
            story.append(Paragraph(
                f'<a href="{t["url"]}" color="{BRAND["purple"]}">'
                f'Открыть тур на youtravel.me →</a>',
                style_link,
            ))

        if i < len(tours):
            story.append(PageBreak())

    doc.build(story)
    return output_path


def _fetch_image_flowable(url: str | None, max_w_cm: float = 16):
    """Скачать изображение и обернуть в reportlab Image. None при ошибке."""
    if not url:
        return None
    try:
        with httpx.Client(timeout=10, follow_redirects=True) as http:
            r = http.get(url)
            r.raise_for_status()
            data = r.content
    except Exception as e:
        log.warning(f"image fetch failed: {url}: {e}")
        return None

    try:
        from PIL import Image as PILImage
        from reportlab.lib.units import cm
        from reportlab.platypus import Image as RLImage

        bio = io.BytesIO(data)
        # Открываем для определения размера и конвертации, если нужно
        pil = PILImage.open(bio)
        pil.load()
        if pil.mode not in ("RGB", "L"):
            pil = pil.convert("RGB")
        out = io.BytesIO()
        pil.save(out, format="JPEG", quality=85)
        out.seek(0)

        max_w_pt = max_w_cm * cm
        ratio = pil.height / pil.width
        return RLImage(out, width=max_w_pt, height=max_w_pt * ratio)
    except Exception as e:
        log.warning(f"image render failed: {e}")
        return None


# ============================================================================
# Slack обработчики
# ============================================================================

if not SLACK_BOT_TOKEN or not SLACK_SIGNING_SECRET:
    log.warning("SLACK_BOT_TOKEN / SLACK_SIGNING_SECRET не заданы — Slack-эндпоинт работать не будет.")

slack_app = SlackApp(
    token=SLACK_BOT_TOKEN or "xoxb-placeholder",
    signing_secret=SLACK_SIGNING_SECRET or "placeholder",
    token_verification_enabled=bool(SLACK_BOT_TOKEN),
    request_verification_enabled=bool(SLACK_SIGNING_SECRET),
)


def _respond_with_tours(channel: str, client, logger) -> None:
    """Общая логика: дёрнуть API, отформатировать, отправить текст + PDF."""
    items, err = fetch_tours()
    if err:
        client.chat_postMessage(channel=channel, text=f":warning: {err}")
        return

    tours = [summarize_tour(it) for it in items[:TOP_N]]
    text = format_text(tours)
    blocks = format_blocks(tours)

    # Текст с богатой разметкой
    try:
        client.chat_postMessage(channel=channel, text=text, blocks=blocks,
                                unfurl_links=False, unfurl_media=False)
    except Exception as e:
        logger.error(f"chat_postMessage failed: {e}")
        return

    # PDF
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            pdf_path = tmp.name
        generate_pdf(tours, pdf_path)
        client.files_upload_v2(
            channel=channel,
            file=pdf_path,
            filename="youtravel-podborka.pdf",
            title="Подборка туров · YouTravel.me",
            initial_comment="Подборка в PDF — можно переслать клиенту.",
        )
    except Exception as e:
        logger.warning(f"PDF upload failed: {e}")
        client.chat_postMessage(channel=channel,
                                text=":information_source: PDF не удалось приложить (см. логи).")
    finally:
        try:
            os.unlink(pdf_path)
        except Exception:
            pass


@slack_app.event("app_mention")
def handle_mention(event: dict, client, logger) -> None:
    """Бот упомянут в канале → подбор."""
    channel = event.get("channel")
    user = event.get("user")
    text = event.get("text", "")
    logger.info(f"mention from {user} in {channel}: {text!r}")
    _respond_with_tours(channel, client, logger)


@slack_app.event("message")
def handle_message(event: dict, client, logger) -> None:
    """Личные сообщения (DM) — реагируем только на сообщения людей в IM."""
    # игнорируем эхо собственного бота
    if event.get("bot_id") or event.get("subtype") == "bot_message":
        return
    # реагируем только на DM (channel_type == "im")
    if event.get("channel_type") != "im":
        return
    channel = event.get("channel")
    user = event.get("user")
    text = event.get("text", "")
    logger.info(f"DM from {user}: {text!r}")
    _respond_with_tours(channel, client, logger)


# ============================================================================
# FastAPI
# ============================================================================

api = FastAPI(title="YouTravel Slack Agent")
slack_handler = SlackRequestHandler(slack_app)


@api.post("/slack/events")
async def slack_events(req: Request):
    return await slack_handler.handle(req)


@api.get("/")
def root() -> dict[str, str]:
    return {
        "status": "ok",
        "slack_endpoint": "/slack/events",
        "test_endpoint": "/ask",
        "pdf_preview": "/preview.pdf",
    }


@api.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@api.get("/ask")
def ask_test() -> dict[str, Any]:
    """Тест без Slack — открой в браузере, увидишь JSON с топ-5 туров."""
    items, err = fetch_tours()
    if err:
        return {"error": err, "count": 0}
    tours = [summarize_tour(it) for it in items[:TOP_N]]
    return {"count": len(tours), "tours": tours, "preview_text": format_text(tours)}


@api.get("/preview.pdf")
def preview_pdf():
    """Сгенерировать PDF и отдать в браузер — для проверки вёрстки без Slack."""
    from fastapi.responses import FileResponse
    items, err = fetch_tours()
    if err:
        return {"error": err}
    tours = [summarize_tour(it) for it in items[:TOP_N]]
    out = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False).name
    generate_pdf(tours, out)
    return FileResponse(out, media_type="application/pdf", filename="youtravel-podborka.pdf")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(api, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
