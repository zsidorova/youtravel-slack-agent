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
    "&period%5B0%5D%5Bfrom%5D=24.05.2026&period%5B0%5D%5Bto%5D=25.06.2026"
    "&languages%5B0%5D=1151&sort_by=rank_no_resident"
    "&loc%5B0%5D%5Btype%5D=country&loc%5B0%5D%5Bid%5D=287"
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


YOUTRAVEL_CDN = "https://cf.youtravel.me"


def _extract_photo_url(item: dict) -> str | None:
    """Достаём фото тура. Основной источник — items.preview_image (относительный
    путь, префиксим CDN). Остальные поля — на случай если API поменяется."""
    # Главный кейс: items[].preview_image — отдаётся как относительный путь.
    preview = item.get("preview_image")
    if isinstance(preview, str) and preview.strip():
        p = preview.strip()
        if p.startswith(("http://", "https://")):
            return p
        return YOUTRAVEL_CDN + ("" if p.startswith("/") else "/") + p

    # Фолбэк на другие частые имена полей
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


def _wrap_text(text: str, font_name: str, font_size: float, max_width_pt: float) -> list[str]:
    """Простой word-wrap по ширине шрифта."""
    from reportlab.pdfbase import pdfmetrics
    if not text:
        return [""]
    words = text.split()
    lines: list[str] = []
    current: list[str] = []
    for word in words:
        test = " ".join(current + [word])
        if pdfmetrics.stringWidth(test, font_name, font_size) <= max_width_pt or not current:
            current.append(word)
        else:
            lines.append(" ".join(current))
            current = [word]
    if current:
        lines.append(" ".join(current))
    return lines


def _fetch_cover_image(url: str | None, target_w_pt: float, target_h_pt: float):
    """Скачать и обрезать картинку под target размер (cover-fit). Возвращает BytesIO или None."""
    if not url:
        return None
    try:
        with httpx.Client(timeout=12, follow_redirects=True) as http:
            r = http.get(url)
            r.raise_for_status()
            data = r.content
        from PIL import Image as PILImage
        pil = PILImage.open(io.BytesIO(data))
        pil.load()
        if pil.mode not in ("RGB", "L"):
            pil = pil.convert("RGB")
        # cover-fit: обрезать так, чтобы аспект совпал с целевым
        target_aspect = target_w_pt / target_h_pt
        img_aspect = pil.width / pil.height
        if img_aspect > target_aspect:
            new_w = int(pil.height * target_aspect)
            left = (pil.width - new_w) // 2
            pil = pil.crop((left, 0, left + new_w, pil.height))
        else:
            new_h = int(pil.width / target_aspect)
            top = (pil.height - new_h) // 2
            pil = pil.crop((0, top, pil.width, top + new_h))
        out = io.BytesIO()
        pil.save(out, format="JPEG", quality=85)
        out.seek(0)
        return out
    except Exception as e:
        log.warning(f"image fetch/render failed for {url}: {e}")
        return None


def generate_pdf(tours: list[dict[str, Any]], output_path: str) -> str:
    """PDF в email-style: hero photo + бейдж + eyebrow + h1 + инфо-карточка + CTA.

    Каждый тур = одна страница A4. Используется фото из items.
    """
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import cm
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen.canvas import Canvas

    _register_cyrillic_fonts()

    # Палитра из брендбука + email-референса
    purple       = colors.HexColor(BRAND["purple"])       # #771F96
    purple_dark  = colors.HexColor(BRAND["purple_dark"])  # #582868
    purple_pale  = colors.HexColor("#F4ECFA")             # фон бейджей/плашек
    green_btn    = colors.HexColor("#9DB319")             # CTA-кнопка
    green_badge  = colors.HexColor("#ABC232")             # статус-бейдж на фото
    night        = colors.HexColor(BRAND["night"])        # #242A37 — основной текст
    muted        = colors.HexColor("#828296")             # eyebrow
    muted2       = colors.HexColor("#727281")             # subtitle
    paper        = colors.HexColor("#F6F7FA")             # фон инфо-карточки
    divider      = colors.HexColor("#E2E6EC")             # тонкие разделители

    W, H = A4
    c = Canvas(output_path, pagesize=A4)
    c.setTitle(f"{BRAND_NAME} · Подборка туров")
    c.setAuthor(BRAND_NAME)

    # ─────────────────────────── Обложка ───────────────────────────
    margin_x = 2 * cm

    c.setFillColor(purple)
    c.setFont(_PDF_FONT_BOLD, 16)
    c.drawString(margin_x, H - 2.2 * cm, "YouTravel.me")

    # Зелёный акцент
    c.setFillColor(green_btn)
    c.rect(margin_x, H - 7.5 * cm, 1.2 * cm, 0.2 * cm, fill=1, stroke=0)

    # Большой заголовок
    c.setFillColor(night)
    c.setFont(_PDF_FONT_BOLD, 38)
    c.drawString(margin_x, H - 9.2 * cm, "Авторская")
    c.drawString(margin_x, H - 10.6 * cm, "подборка туров")

    # Слоган
    c.setFillColor(purple_dark)
    c.setFont(_PDF_FONT_ITALIC, 14)
    c.drawString(margin_x, H - 11.5 * cm, TAGLINE)

    # Дата
    c.setFillColor(muted)
    c.setFont(_PDF_FONT_REGULAR, 11)
    c.drawString(margin_x, H - 13 * cm, f"Подготовлено {datetime.now().strftime('%d.%m.%Y')}")

    # Подвал обложки
    c.setStrokeColor(divider)
    c.setLineWidth(0.5)
    c.line(margin_x, 2 * cm, W - margin_x, 2 * cm)
    c.setFillColor(muted)
    c.setFont(_PDF_FONT_REGULAR, 9)
    c.drawString(margin_x, 1.5 * cm, "youtravel.me · авторские туры, придуманные людьми")

    c.showPage()

    # ─────────────────────── Страницы туров ───────────────────────
    for idx, tour in enumerate(tours, 1):
        _draw_tour_page(
            c, idx, len(tours), tour, W, H,
            purple, purple_dark, purple_pale,
            green_btn, green_badge,
            night, muted, muted2, paper, divider,
        )
        c.showPage()

    c.save()
    return output_path


def _draw_tour_page(
    c, idx: int, total: int, tour: dict[str, Any],
    W: float, H: float,
    purple, purple_dark, purple_pale,
    green_btn, green_badge,
    night, muted, muted2, paper, divider,
) -> None:
    from reportlab.lib import colors
    from reportlab.lib.units import cm
    from reportlab.lib.utils import ImageReader

    margin_x = 1.5 * cm
    page_pad_top = 1.5 * cm
    page_pad_bot = 1.8 * cm

    # ── Лого/заголовок сверху ──────────────────────────────────────
    c.setFillColor(purple)
    c.setFont(_PDF_FONT_BOLD, 13)
    c.drawString(margin_x, H - 1.2 * cm, "YouTravel.me")
    c.setFillColor(muted)
    c.setFont(_PDF_FONT_REGULAR, 9)
    c.drawRightString(W - margin_x, H - 1.2 * cm, f"Тур {idx} из {total}")

    # ── Карточка ──────────────────────────────────────────────────
    card_x = margin_x
    card_w = W - 2 * margin_x
    card_top = H - page_pad_top - 0.4 * cm
    card_bot = page_pad_bot + 1 * cm
    card_h = card_top - card_bot

    # Лёгкая обводка карточки + белый фон
    c.setFillColor(colors.white)
    c.setStrokeColor(divider)
    c.setLineWidth(0.5)
    c.rect(card_x, card_bot, card_w, card_h, fill=1, stroke=1)

    # ── Hero photo ────────────────────────────────────────────────
    hero_h = 6.5 * cm
    hero_y = card_top - hero_h
    photo_data = _fetch_cover_image(tour.get("photo"), card_w, hero_h)

    if photo_data is not None:
        try:
            c.drawImage(ImageReader(photo_data), card_x, hero_y, card_w, hero_h,
                        preserveAspectRatio=False, mask="auto")
        except Exception as e:
            log.warning(f"drawImage failed: {e}")
            photo_data = None
    if photo_data is None:
        # placeholder
        c.setFillColor(purple_pale)
        c.rect(card_x, hero_y, card_w, hero_h, fill=1, stroke=0)
        c.setFillColor(purple)
        c.setFont(_PDF_FONT_BOLD, 14)
        c.drawCentredString(card_x + card_w / 2, hero_y + hero_h / 2, "YouTravel.me")

    # Зелёный бейдж сверху-слева на фото
    badge_text = "АВТОРСКИЙ ТУР"
    c.setFont(_PDF_FONT_BOLD, 8.5)
    from reportlab.pdfbase import pdfmetrics
    txt_w = pdfmetrics.stringWidth(badge_text, _PDF_FONT_BOLD, 8.5)
    badge_pad_x = 0.35 * cm
    badge_w = txt_w + 2 * badge_pad_x
    badge_h = 0.65 * cm
    badge_x = card_x + 0.4 * cm
    badge_y = hero_y + hero_h - badge_h - 0.4 * cm
    c.setFillColor(green_badge)
    c.roundRect(badge_x, badge_y, badge_w, badge_h, 0.12 * cm, fill=1, stroke=0)
    c.setFillColor(colors.white)
    c.drawString(badge_x + badge_pad_x, badge_y + 0.21 * cm, badge_text)

    # ── Текстовый блок: eyebrow + title + subtitle ────────────────
    content_x = card_x + 1.0 * cm
    content_w = card_w - 2.0 * cm

    eyebrow_y = hero_y - 0.9 * cm
    region = (tour.get("region") or "").strip()
    eyebrow = "АВТОРСКИЙ ТУР"
    if region:
        eyebrow += " · " + region.upper()
    c.setFillColor(muted)
    c.setFont(_PDF_FONT_BOLD, 8.5)
    c.drawString(content_x, eyebrow_y, eyebrow)

    # Title (1-2 строки)
    title = tour.get("title") or "Без названия"
    title_font_size = 20
    title_lines = _wrap_text(title, _PDF_FONT_BOLD, title_font_size, content_w)[:2]
    title_top_y = eyebrow_y - 0.9 * cm
    c.setFillColor(night)
    c.setFont(_PDF_FONT_BOLD, title_font_size)
    title_line_h = 0.85 * cm
    for i, line in enumerate(title_lines):
        c.drawString(content_x, title_top_y - i * title_line_h, line)
    title_block_h = len(title_lines) * title_line_h

    # Subtitle с датами
    sub_y = title_top_y - title_block_h - 0.1 * cm
    date_from = tour.get("date_from") or "—"
    date_to = tour.get("date_to") or "—"
    c.setFillColor(muted2)
    c.setFont(_PDF_FONT_REGULAR, 12)
    c.drawString(content_x, sub_y, f"Даты {date_from} – {date_to}")

    # ── Инфо-карточка с серым фоном (rounded) ─────────────────────
    info_h = 3.0 * cm
    info_y = sub_y - 1.5 * cm - info_h
    info_x = content_x
    info_w = content_w
    c.setFillColor(paper)
    c.roundRect(info_x, info_y, info_w, info_h, 0.35 * cm, fill=1, stroke=0)

    # Верхняя строка: НАПРАВЛЕНИЕ (label + value)
    pad = 0.7 * cm
    label_top_y = info_y + info_h - pad
    c.setFillColor(muted)
    c.setFont(_PDF_FONT_BOLD, 8)
    c.drawString(info_x + pad, label_top_y, "НАПРАВЛЕНИЕ")
    c.setFillColor(night)
    c.setFont(_PDF_FONT_BOLD, 13)
    c.drawString(info_x + pad, label_top_y - 0.55 * cm, region or "—")

    # Разделитель внутри инфо-карточки
    div_y = info_y + 1.15 * cm
    c.setStrokeColor(divider)
    c.setLineWidth(0.5)
    c.line(info_x + pad, div_y, info_x + info_w - pad, div_y)

    # Нижняя строка: ЦЕНА слева label, справа сумма большой
    c.setFillColor(muted)
    c.setFont(_PDF_FONT_BOLD, 8)
    c.drawString(info_x + pad, info_y + 0.55 * cm, "ЦЕНА ОТ")
    c.setFillColor(night)
    c.setFont(_PDF_FONT_BOLD, 17)
    c.drawRightString(info_x + info_w - pad, info_y + 0.45 * cm, tour.get("price") or "—")

    # ── CTA-кнопка ────────────────────────────────────────────────
    btn_w = 5.5 * cm
    btn_h = 1.05 * cm
    btn_x = card_x + card_w / 2 - btn_w / 2
    btn_y = info_y - 1.5 * cm

    c.setFillColor(green_btn)
    c.roundRect(btn_x, btn_y, btn_w, btn_h, 0.18 * cm, fill=1, stroke=0)
    c.setFillColor(colors.white)
    c.setFont(_PDF_FONT_BOLD, 12)
    c.drawCentredString(btn_x + btn_w / 2, btn_y + 0.34 * cm, "Открыть тур  →")
    if tour.get("url"):
        c.linkURL(tour["url"], (btn_x, btn_y, btn_x + btn_w, btn_y + btn_h),
                  relative=0, thickness=0)

    # ── Подвал страницы ────────────────────────────────────────────
    c.setStrokeColor(divider)
    c.setLineWidth(0.5)
    c.line(margin_x, 1.4 * cm, W - margin_x, 1.4 * cm)
    c.setFillColor(muted)
    c.setFont(_PDF_FONT_REGULAR, 8)
    c.drawString(margin_x, 1.0 * cm, f"youtravel.me  ·  {TAGLINE}")
    c.drawRightString(W - margin_x, 1.0 * cm, f"{idx} / {total}")


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
