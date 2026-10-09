"""Детерминированный новостной фон по бумаге (§8.3, Ф2a): счётчики сюжетов, buzz_z и заголовки — без LLM и тона.

Чистый модуль без I/O и без Telegram: рендер — в каналах.

Определения:
- Время новости — `published_at`, если источник его дал, иначе `first_seen_at`. Новости «из будущего»
  (позже `now`) не учитываются.
- Сюжет «активен» в окне, если в окне есть хотя бы одна его новость, связанная с бумагой. Сюжет, который
  продолжился на следующий день, учитывается в обоих днях — это мера внимания (buzz), а не число новых событий.
- День — календарный день по Москве (UTC+3 без перехода на летнее время с 2014 года, как в календаре каналов).
- История для buzz_z — календарные дни до сегодняшнего (не торговые: модуль не знает календаря MOEX),
  не раньше начала архива. Выходные с малым числом новостей занижают среднее и завышают разброс.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import ROUND_HALF_EVEN, Decimal

from investbot.news.models import Headline, StoredNews, TickerNewsBackground

MSK = timezone(timedelta(hours=3), "MSK")
MIN_OBS = 20
STD_FLOOR = Decimal(1)  # минимальный знаменатель z, в сюжетах
_Z_STEP = Decimal("0.01")


def news_time(n: StoredNews) -> datetime:
    return n.published_at or n.first_seen_at


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def msk_date(at: datetime) -> date:
    """Календарный день по Москве; наивное время считается UTC."""
    return _aware(at).astimezone(MSK).date()


def _linked(n: StoredNews, ticker: str) -> bool:
    return any(link.ticker == ticker for link in n.links)


def daily_story_counts(items: Sequence[StoredNews], ticker: str, end: date, days: int) -> list[int]:
    """Число уникальных сюжетов по бумаге за каждый московский день `[end − days + 1, end]`, от старых к новым."""
    if days <= 0:
        return []
    start = end - timedelta(days=days - 1)
    per_day: dict[date, set[int]] = defaultdict(set)
    for n in items:
        if _linked(n, ticker):
            d = msk_date(news_time(n))
            if start <= d <= end:
                per_day[d].add(n.story_id)
    return [len(per_day.get(start + timedelta(days=i), ())) for i in range(days)]


def buzz_z(
    history: Sequence[int], current: int, min_obs: int = MIN_OBS, *, std_floor: Decimal = STD_FLOOR
) -> Decimal | None:
    """z-оценка `current` к среднему и выборочному стандартному отклонению `history`, с точностью 0,01.

    None — в истории меньше `min_obs` наблюдений. Знаменатель не меньше `std_floor` (1 сюжет): у редко
    упоминаемой бумаги история почти из нулей, и без нижней границы одна новость дала бы z ≈ 10, а при
    нулевом разбросе z не определена. С границей нулевой разброс не особый случай: та же активность → 0,
    всплеск на k сюжетов над средним → k.
    """
    n = len(history)
    if n < max(min_obs, 1):
        return None
    mean = Decimal(sum(history)) / n
    var = sum(((Decimal(x) - mean) ** 2 for x in history), Decimal(0)) / (n - 1) if n > 1 else Decimal(0)
    std = max(var.sqrt(), std_floor)
    return ((Decimal(current) - mean) / std).quantize(_Z_STEP, rounding=ROUND_HALF_EVEN)


def build_background(
    ticker: str,
    items: Sequence[StoredNews],
    now: datetime,
    history_days: int = 120,
    max_headlines: int = 5,
    *,
    history_start: date | None = None,
) -> TickerNewsBackground:
    """Фон по бумаге на момент `now`.

    `items` — архив за `history_days` дней (можно по всем бумагам: фильтр по связи внутри). Дубликаты сюжета без
    связи с бумагой тоже учитываются в `story_size`. `history_start` — первый день архива (по Москве); по умолчанию
    — день самого раннего `first_seen_at` в `items`, чтобы дни до запуска сбора не считались нулевыми наблюдениями.

    - `stories_24h`, `stories_7d` — уникальные сюжеты в скользящих окнах (now − 24 ч, now], (now − 7 сут, now];
    - `buzz_z` — `stories_24h` к дневным счётчикам с `history_start` по вчера (не больше `history_days` дней);
    - `headlines` — по одному на сюжет из окна 7 суток: самая ранняя новость сюжета, `story_size` — число разных
      источников сюжета; новые сверху.
    """
    now = _aware(now)
    visible = [n for n in items if _aware(news_time(n)) <= now]
    linked = [n for n in visible if _linked(n, ticker)]

    def stories_since(delta: timedelta) -> set[int]:
        return {n.story_id for n in linked if _aware(news_time(n)) > now - delta}

    s24 = stories_since(timedelta(hours=24))
    s7 = stories_since(timedelta(days=7))

    yesterday = msk_date(now) - timedelta(days=1)
    start = history_start or min((msk_date(n.first_seen_at) for n in items), default=yesterday + timedelta(days=1))
    days = min(history_days, (yesterday - start).days + 1)
    history = daily_story_counts(linked, ticker, yesterday, days)

    members: dict[int, list[StoredNews]] = defaultdict(list)
    for n in visible:
        if n.story_id in s7:
            members[n.story_id].append(n)
    heads: list[tuple[datetime, int, Headline]] = []
    for sid, ms in members.items():
        rep = min(ms, key=lambda m: (_aware(news_time(m)), m.id))
        at = news_time(rep)
        heads.append((_aware(at), sid, Headline(
            title=rep.title, url=rep.url, source=rep.source, at=at, story_size=len({m.source for m in ms}),
        )))
    heads.sort(key=lambda h: (h[0], h[1]), reverse=True)

    return TickerNewsBackground(
        ticker=ticker,
        stories_7d=len(s7),
        stories_24h=len(s24),
        buzz_z=buzz_z(history, len(s24)),
        headlines=tuple(h for _, _, h in heads[: max(max_headlines, 0)]),
    )
