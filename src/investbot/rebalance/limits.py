"""HardLimits после оптимизатора (§10.5), подмножество MVP-1a.

Проверяются: минимальная сделка, недельный оборот (размещение свободного кэша и пополнения не в счёт), кэш
(по счёту ≥ 0 и общий резерв при любой покупке), потолки и полосы покупаемых бакетов, сквозной лимит эмитента
и исключённые эмитенты. Нарушение снимает сделку: planner запрещает её и решает задачу заново. Сектор, выпуск
облигаций, ВДО на выпуск, ЦФА по оператору, ликвидность (ADV20) и валютная экспозиция — следующие этапы: нужных
данных во входе движка нет.
Тексты нарушений уходят в Telegram, поэтому в них нет рублёвых сумм (режим приватности `weights`).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from investbot.allocation.models import bucket_title
from investbot.core.money import ZERO, pct
from investbot.rebalance.models import Side


@dataclass(frozen=True)
class Trade:
    key: int  # индекс ноги в задаче округления
    account_id: str
    ticker: str
    bucket: str
    issuer: str | None
    side: Side
    amount: Decimal
    fee: Decimal


@dataclass(frozen=True)
class LimitContext:
    nav: Decimal
    cash: Mapping[str, Decimal]  # рублёвый кэш торгуемых счетов до сделок (с пополнением)
    reserve: Decimal
    min_trade: Decimal
    turnover_cap: Decimal
    bucket_values: Mapping[str, Decimal]
    bucket_caps: Mapping[str, Decimal]  # бакет → максимальная доля после покупок (верх полосы или cap)
    issuer_values: Mapping[str, Decimal]
    max_issuer: Decimal
    exclude_issuers: frozenset[str]
    cash_bucket: str = "money_market"
    cash_flow: Decimal = ZERO  # размещение свободного кэша и пополнения оборотом не считается


@dataclass(frozen=True)
class Breach:
    keys: tuple[int, ...]  # сделки, которые нужно снять
    scope: Literal["leg", "ticker"]  # ticker — запретить бумагу на всех счетах
    message: str


def cash_after(trades: Sequence[Trade], cash: Mapping[str, Decimal]) -> dict[str, Decimal]:
    out = defaultdict(lambda: ZERO, cash)
    for t in trades:
        out[t.account_id] += (t.amount if t.side == "sell" else -t.amount) - t.fee
    return dict(out)


def values_after(trades: Sequence[Trade], values: Mapping[str, Decimal], cash_bucket: str) -> dict[str, Decimal]:
    out = defaultdict(lambda: ZERO, values)
    for t in trades:
        signed = t.amount if t.side == "buy" else -t.amount
        out[t.bucket] += signed
        out[cash_bucket] -= signed + t.fee
    return dict(out)


def _smallest(trades: Sequence[Trade]) -> Trade:
    return min(trades, key=lambda t: (t.amount, t.ticker, t.account_id, t.key))


def check_hard_limits(trades: Sequence[Trade], ctx: LimitContext) -> list[Breach]:
    out: list[Breach] = []
    buys = [t for t in trades if t.side == "buy"]
    for t in trades:
        if t.amount < ctx.min_trade:
            out.append(Breach((t.key,), "leg", f"снята сделка {t.ticker}: меньше минимальной суммы сделки"))
    for t in buys:
        if t.issuer is not None and t.issuer in ctx.exclude_issuers:
            out.append(Breach((t.key,), "ticker", f"снята покупка {t.ticker}: эмитент исключён в IPS"))

    nav_after = ctx.nav - sum((t.fee for t in trades), ZERO)
    values = values_after(trades, ctx.bucket_values, ctx.cash_bucket)
    by_bucket: dict[str, list[Trade]] = defaultdict(list)
    for t in buys:
        by_bucket[t.bucket].append(t)
    for bucket in sorted(by_bucket):
        cap = ctx.bucket_caps.get(bucket)
        grew = values[bucket] > ctx.bucket_values.get(bucket, ZERO)
        if cap is not None and grew and values[bucket] > cap * nav_after:
            keys = tuple(t.key for t in by_bucket[bucket])
            out.append(Breach(keys, "ticker", f"сняты покупки в «{bucket_title(bucket)}»: доля превысила бы "
                                              f"верхнюю границу {pct(cap)}"))

    issuer_delta: dict[str, Decimal] = defaultdict(lambda: ZERO)
    by_issuer: dict[str, list[Trade]] = defaultdict(list)
    for t in trades:
        if t.issuer is not None:
            issuer_delta[t.issuer] += t.amount if t.side == "buy" else -t.amount
            if t.side == "buy":
                by_issuer[t.issuer].append(t)
    for issuer in sorted(by_issuer):
        before = ctx.issuer_values.get(issuer, ZERO)
        after = before + issuer_delta[issuer]
        if after > ctx.max_issuer * nav_after and after > before:
            names = ", ".join(sorted({t.ticker for t in by_issuer[issuer]}))
            out.append(Breach(tuple(t.key for t in by_issuer[issuer]), "ticker",
                              f"снята покупка {names}: эмитент превысил бы лимит {pct(ctx.max_issuer)} портфеля"))

    cash = cash_after(trades, ctx.cash)
    for account in sorted(cash):
        own = [t for t in buys if t.account_id == account]
        if cash[account] < 0 and own:
            t = _smallest(own)
            out.append(Breach((t.key,), "leg", f"снята покупка {t.ticker}: не хватает денег на счёте"))
    if buys and sum(cash.values(), ZERO) < ctx.reserve:
        t = _smallest(buys)
        out.append(Breach((t.key,), "leg", f"снята покупка {t.ticker}: кэш опустился бы ниже резерва"))
    sells = [t for t in trades if t.side == "sell"]
    if sum((t.amount for t in sells), ZERO) > ctx.turnover_cap:
        t = _smallest(sells)
        out.append(Breach((t.key,), "leg", f"снята продажа {t.ticker}: превышен недельный лимит оборота"))
    elif sum((t.amount for t in trades), ZERO) > ctx.turnover_cap + ctx.cash_flow and trades:
        t = _smallest(buys or sells)
        out.append(Breach((t.key,), "leg", f"снята сделка {t.ticker}: превышен недельный лимит оборота"))
    return out
