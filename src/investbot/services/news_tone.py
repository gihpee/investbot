"""Тон новостей по бумагам (§8.2 п. 6–7, §8.3, Ф3): LLM-разметка очереди и агрегаты для показа.

Тон только показывается пользователю (карточка позиции, блок «Новостной фон»): в скоринг, ребалансировку и идеи
он не входит (вес 0 до гейта W, §7.1) и не вызывает продаж.

В LLM уходят только тексты источников с `tos_ml_allowed: true` (config/sources.yaml): `ml_sources` обязателен,
чтобы забытый фильтр не отправил в модель тексты с неясной лицензией (например, T-Invest News до спайка Ф0).
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterable, Mapping
from datetime import datetime, timedelta
from decimal import Decimal

import structlog

from investbot.data.annotation_repo import AnnotationRepo
from investbot.llm_roles.tagger import Tagger
from investbot.news.sources import SourceConfig
from investbot.news.tone import EQUITY_TONE, ToneParams, ToneSummary, summarize_tone

log = structlog.get_logger(__name__)

MAX_CONSECUTIVE_MISSES = 2  # два None подряд — LLM недоступна или бюджет исчерпан: прекращаем до следующего запуска
UNKNOWN_MODEL = "unknown"


def ml_sources(sources: Iterable[SourceConfig]) -> frozenset[str]:
    """Ключи источников, тексты которых можно отдавать в LLM-разметку."""
    return frozenset(s.key for s in sources if s.tos_ml_allowed)


def q_src_map(sources: Iterable[SourceConfig]) -> dict[str, Decimal]:
    return {s.key: s.q_src for s in sources}


class NewsToneService:
    def __init__(
        self,
        repo: AnnotationRepo,
        tagger: Tagger | None,
        name_for: Callable[[str], str | None],
        *,
        ml_sources: Collection[str],
        q_src: Mapping[str, Decimal] | None = None,
        params_for: Callable[[str], ToneParams] | None = None,
        model_id: Callable[[], str | None] | None = None,
    ) -> None:
        """`name_for` — название бумаги из справочника для списка кандидатов; `params_for` — τ½ и k по тикеру
        (по умолчанию как для акций, облигациям — `BOND_TONE`); `model_id` — точный id модели последнего вызова
        от роутера (G10), без него в разметку пишется "unknown"."""
        self._repo = repo
        self._tagger = tagger
        self._name_for = name_for
        self._ml_sources = frozenset(ml_sources)
        self._q_src = dict(q_src or {})
        self._params_for = params_for or (lambda _t: EQUITY_TONE)
        self._model_id = model_id

    async def annotate_pending(self, tickers: Iterable[str], now: datetime, limit: int = 50,
                               budget_left: Callable[[], bool] | None = None, *, days: int = 7) -> int:
        """Размечает до `limit` неразмеченных новостей за `days` дней по `tickers`, новые первыми.

        Останавливается, когда `budget_left()` вернул False или Tagger вернул None дважды подряд (LLM недоступна
        или бюджет исчерпан); исключение Tagger считается тем же None. Возвращает число размеченных новостей."""
        if self._tagger is None or limit <= 0 or not self._ml_sources:
            return 0
        batch = await self._repo.pending(tickers, now - timedelta(days=days), limit, sources=self._ml_sources)
        done = misses = 0
        for p in batch:
            if budget_left is not None and not budget_left():
                log.info("news_tone.budget_exhausted", done=done, left=len(batch) - done)
                break
            candidates = [(t, name or self._name_for(t)) for t, name in p.candidates]
            try:
                out = await self._tagger.annotate(p.news_id, p.title, p.lead, p.source, candidates)
            except Exception as e:
                log.warning("news_tone.tagger_failed", news_id=p.news_id, error=type(e).__name__)
                out = None
            if out is None:
                misses += 1
                if misses >= MAX_CONSECUTIVE_MISSES:
                    log.warning("news_tone.llm_unavailable", done=done)
                    break
                continue
            misses = 0
            model = (self._model_id() if self._model_id is not None else None) or UNKNOWN_MODEL
            await self._repo.save(p.news_id, out, model, self._tagger.prompt_id, at=now)
            done += 1
        return done

    async def summaries(self, tickers: Iterable[str], now: datetime, days: int = 7) -> dict[str, ToneSummary]:
        """Тон по каждой бумаге за `days` дней на момент `now`; без разметки — метка `none`."""
        wanted = sorted(set(tickers))
        items = await self._repo.tone_items(wanted, now - timedelta(days=days))
        out: dict[str, ToneSummary] = {}
        for t in wanted:
            p = self._params_for(t)
            out[t] = summarize_tone(items.get(t, []), now, self._q_src, p.half_life_days, p.k, ticker=t)
        return out
