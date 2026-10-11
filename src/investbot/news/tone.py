"""Тон новостей по бумаге (§8.2 п. 7, §8.3): агрегат LLM-разметки для показа пользователю.

Чистый модуль без I/O. Тон только показывается (метка 🟢/🔴/⚪, разброс, события): **вес тона в скоринге — 0**
до гейта W (§7.1). Флаг «пересмотреть» (`sent_z ≤ −2` и `buzz_z ≥ 2`, §8.3) — Ф3 и требует истории `sent`;
здесь он не считается, и никаких автоматических продаж тон не вызывает.

Агрегат за окно на момент `now`:
1. Отбрасываются реклама (`is_ad`) и элементы позже `now`.
2. Один голос на сюжет: из элементов сюжета берётся элемент с наибольшей |relevance| (при равенстве — самый ранний).
3. Вес голоса `w = relevance · novelty · confidence · q_src · exp(−ln2 · Δt / τ½)`:
   - novelty = 1, если голосует первая новость сюжета (`news_id == story_id`), и 0,5 — если повтор
     (перепечатка или продолжение, первая новость сюжета бумагу не затрагивает или упоминает слабее);
   - q_src — вес достоверности источника из config/sources.yaml, для неизвестного источника `DEFAULT_Q_SRC`;
   - Δt — в календарных сутках от времени новости (published_at, иначе first_seen_at) до `now`.
4. Тон с усадкой к нулю: `sent = Σ w·s / (Σ w + k)`; k = 2 для акций, 1 для облигаций (`ToneParams`).
   Одна слабая новость почти не сдвигает тон, устойчивый фон из нескольких сюжетов — сдвигает.
5. `dispersion = sqrt(Σ w·(s − m)² / Σ w)`, `m = Σ w·s / Σ w` — взвешенное стандартное отклонение тона без усадки.
6. `n_items` — число голосов с w > 0; `top_events` — до трёх типов событий (кроме `other`) по сумме весов.

Метки (пороги — константы ниже, калибровка в Ф4 по золотой выборке):
- `none` — голосов нет (sent = None);
- `mixed` («🟡 спорно») — не меньше `MIXED_MIN_ITEMS` голосов, dispersion ≥ `MIXED_DISPERSION` и у меньшинства
  по знаку тона не меньше `MIXED_MINORITY_SHARE` веса: min(W⁺, W⁻) ≥ 0,25 · (W⁺ + W⁻), где W⁺ и W⁻ — суммы весов
  голосов с s > 0 и s < 0. Разброс без противоположных знаков (нейтральные и позитивные) спорным не считается;
- `pos` — sent ≥ `SENT_THRESHOLD`; `neg` — sent ≤ −`SENT_THRESHOLD`; иначе `neutral`.

Порог 0,1 при k = 2: одна новость с w ≈ 0,5 должна иметь тон ≥ 0,5, чтобы метка стала цветной; три такие
новости — тон ≥ 0,1 · 3,5 / 1,5 ≈ 0,23.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Literal

from pydantic import Field

from investbot.core.models import Strict

ToneLabel = Literal["pos", "neg", "neutral", "mixed", "none"]

LN2 = Decimal(2).ln()
NOVELTY_FIRST = Decimal(1)
NOVELTY_REPEAT = Decimal("0.5")
DEFAULT_Q_SRC = Decimal("0.5")  # источник не из реестра: вес как у самых слабых
SENT_THRESHOLD = Decimal("0.1")
MIXED_DISPERSION = Decimal("0.4")
MIXED_MINORITY_SHARE = Decimal("0.25")
MIXED_MIN_ITEMS = 2
TOP_EVENTS = 3
_STEP = Decimal("0.001")
_DAY_US = Decimal(86_400 * 10**6)


class ToneItem(Strict):
    """Разметка одной новости для одной бумаги (строка news_annotation + поля news_item)."""

    news_id: int
    story_id: int
    at: datetime  # published_at, иначе first_seen_at
    source: str
    tone: Decimal = Field(ge=-1, le=1)
    relevance: Decimal = Field(ge=0, le=1)
    confidence: Decimal = Field(ge=0, le=1)
    event_type: str
    is_ad: bool
    is_rumor: bool = False
    summary: str = ""


class ToneParams(Strict):
    """τ½ и усадка. Время — в календарных сутках (модуль не знает календаря MOEX): τ½ = 2 сут ≈ 1–2 торговых дня."""

    half_life_days: Decimal = Field(gt=0)
    k: Decimal = Field(ge=0)


EQUITY_TONE = ToneParams(half_life_days=Decimal(2), k=Decimal(2))  # ликвидные акции: τ½ 1–2 торговых дня
BOND_TONE = ToneParams(half_life_days=Decimal(10), k=Decimal(1))  # облигации и эмитенты: τ½ 5–20 дней


class ToneSummary(Strict):
    ticker: str
    sent: Decimal | None  # тон с усадкой −1…+1; None — нет размеченных новостей
    n_items: int  # голосов (сюжетов) с ненулевым весом
    dispersion: Decimal | None  # взвешенное стандартное отклонение тона; None — голосов нет
    top_events: list[str]  # коды таксономии по убыванию суммарного веса, без `other`
    label: ToneLabel


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _age_days(now: datetime, at: datetime) -> Decimal:
    d = now - at
    return Decimal((d.days * 86_400 + d.seconds) * 10**6 + d.microseconds) / _DAY_US


def _q(d: Decimal) -> Decimal:
    return d.quantize(_STEP, rounding=ROUND_HALF_EVEN)


def story_votes(items: Sequence[ToneItem], now: datetime) -> list[ToneItem]:
    """По одному элементу на сюжет: без рекламы и будущего; наибольшая |relevance|, при равенстве — самый ранний."""
    now = _aware(now)
    by_story: dict[int, ToneItem] = {}
    for it in items:
        if it.is_ad or _aware(it.at) > now:
            continue
        cur = by_story.get(it.story_id)
        key = (-abs(it.relevance), _aware(it.at), it.news_id)
        if cur is None or key < (-abs(cur.relevance), _aware(cur.at), cur.news_id):
            by_story[it.story_id] = it
    return list(by_story.values())


def item_weight(it: ToneItem, now: datetime, q_src: Mapping[str, Decimal], half_life_days: Decimal,
                default_q: Decimal = DEFAULT_Q_SRC) -> Decimal:
    novelty = NOVELTY_FIRST if it.news_id == it.story_id else NOVELTY_REPEAT
    age = max(_age_days(_aware(now), _aware(it.at)), Decimal(0))
    decay = (-LN2 * age / half_life_days).exp()
    return abs(it.relevance) * novelty * it.confidence * q_src.get(it.source, default_q) * decay


def _label(sent: Decimal, n: int, dispersion: Decimal, w_pos: Decimal, w_neg: Decimal) -> ToneLabel:
    signed = w_pos + w_neg
    if (n >= MIXED_MIN_ITEMS and dispersion >= MIXED_DISPERSION and signed > 0
            and min(w_pos, w_neg) >= MIXED_MINORITY_SHARE * signed):
        return "mixed"
    if sent >= SENT_THRESHOLD:
        return "pos"
    if sent <= -SENT_THRESHOLD:
        return "neg"
    return "neutral"


def summarize_tone(
    items: Sequence[ToneItem],
    now: datetime,
    q_src: Mapping[str, Decimal],
    half_life_days: Decimal = EQUITY_TONE.half_life_days,
    k: Decimal = EQUITY_TONE.k,
    *,
    ticker: str,
    default_q: Decimal = DEFAULT_Q_SRC,
) -> ToneSummary:
    """Тон бумаги `ticker` по её разметке `items` на момент `now` (формулы и пороги — в docstring модуля)."""
    if half_life_days <= 0:
        raise ValueError("half_life_days должен быть положительным")
    if k < 0:
        raise ValueError("k не может быть отрицательным")
    weighted = [(it, w) for it in story_votes(items, now)
                if (w := item_weight(it, now, q_src, half_life_days, default_q)) > 0]
    if not weighted:
        return ToneSummary(ticker=ticker, sent=None, n_items=0, dispersion=None, top_events=[], label="none")
    w_sum = sum((w for _, w in weighted), Decimal(0))
    ws_sum = sum((w * it.tone for it, w in weighted), Decimal(0))
    mean = ws_sum / w_sum
    var = sum((w * (it.tone - mean) ** 2 for it, w in weighted), Decimal(0)) / w_sum
    sent = _q(ws_sum / (w_sum + k))
    dispersion = _q(var.sqrt())
    w_pos = sum((w for it, w in weighted if it.tone > 0), Decimal(0))
    w_neg = sum((w for it, w in weighted if it.tone < 0), Decimal(0))
    by_event: dict[str, Decimal] = defaultdict(Decimal)
    for it, w in weighted:
        if it.event_type != "other":
            by_event[it.event_type] += w
    top = sorted(by_event, key=lambda e: (-by_event[e], e))[:TOP_EVENTS]
    return ToneSummary(ticker=ticker, sent=sent, n_items=len(weighted), dispersion=dispersion, top_events=top,
                       label=_label(sent, len(weighted), dispersion, w_pos, w_neg))


_BADGES: dict[ToneLabel, str] = {"pos": "🟢", "neg": "🔴", "neutral": "⚪", "mixed": "🟡 спорно", "none": ""}


def tone_badge(summary: ToneSummary) -> str:
    """Метка тона: «🟢», «🔴», «⚪», «🟡 спорно»; без размеченных новостей — пустая строка."""
    return _BADGES[summary.label]


def _num(d: Decimal, signed: bool = False) -> str:
    return format(d.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN), "+.2f" if signed else ".2f").replace(".", ",")


def tone_line(summary: ToneSummary, titles: Mapping[str, str] | None = None) -> str:
    """Строка для карточки позиции и блока «Новостной фон» (простой текст, не экранирован для HTML):
    «🟢 тон +0,23 · 4 сюж. · разброс 0,31 · дивиденды, отчётность»; `titles` — подписи событий из таксономии."""
    if summary.sent is None:
        return "тон: нет размеченных новостей"
    parts = [f"{tone_badge(summary)} тон {_num(summary.sent, signed=True)}", f"{summary.n_items} сюж."]
    if summary.dispersion is not None and summary.n_items > 1:
        parts.append(f"разброс {_num(summary.dispersion)}")
    if summary.top_events:
        parts.append(", ".join((titles or {}).get(e, e) for e in summary.top_events))
    return " · ".join(parts)
