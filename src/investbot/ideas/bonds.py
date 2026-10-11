"""Идеи для входа v1: облигации, рука `bond_carry_roll` (ARCHITECTURE §12.1–12.3, метрики §7.2).

Чистая функция: на входе снимок рынка ISS, справочник брокера, КБД и IPS, на выходе — идеи и топ-кандидаты.
Детерминированна: одинаковый вход в любом порядке строк → одинаковый выход.

Конвейер:
1. **Вселенная.** Рублёвые облигации с фиксированным купоном: ОФЗ (режим TQOB или `is_government`) и корпоративные
   с рейтингом не ниже `ips.rules.ig_min_rating` (без рейтинга и ВДО в v1 не берём). Флоатеры и линкеры
   (`coupon_kind` или нет EFFECTIVEYIELD), номинал не в рублях, бумаги без справочника брокера — мимо.
   Оферта/выкуп/call/погашение ближе `min_days_to_event` дней — мимо. Дюрация Маколея в `[min, max]`.
   Ликвидность: NUMTRADES ≥ `min_trades` и VALTODAY ≥ `min_val_today` — **прокси ADV20 по одной сессии**,
   пока нет 20-дневной истории оборотов. Доступ инвестора: `for_qual_only` без статуса квала,
   `buy_available = False`, исключённые эмитенты IPS и эмитенты на лимите IPS (кроме ОФЗ) — мимо.
2. **Метрики** (`analytics.bonds`, доли за горизонт h): горизонт `h = min(horizon_weeks, до ближайшего события)`;
   `alpha_net = carry + roll − EL − c`, где `c = 2 × costs.rate(ticker)` — круговые издержки (комиссия + полуспред
   на входе и на выходе) **один раз за горизонт, без амортизации**: остальные слагаемые тоже за горизонт.
   Выход по оферте или погашению тоже считается продажей — консервативно. Барьер: `alpha_net > min_alpha_net`.
   `score` идеи = `alpha_net`. Короткий горизонт (оферта раньше 26 недель) честно даёт меньший `score`.
3. **Группы «рейтинг × дюрация»** (ОФЗ / AAA / AA / A × корзины дюрации): медиана, MAD и p90 G-спреда по всем
   бумагам, прошедшим рыночные фильтры (п. 1 без фильтров доступа — ограничения инвестора не меняют рынок).
   Группа меньше `min_group_size` — группового сигнала нет: цель доходности и отмена по спреду не задаются.
   Бумага со спредом уже выше p90 группы исключается: её собственное условие отмены выполнено с рождения.
4. **Ранжирование.** Ключ `(бакет не в недовесе, −score, SECID)`: идеи бакетов с недовесом IPS (ofz_fixed / corp_ig)
   идут первыми, внутри — по score. Топ-`max_candidates` — кандидаты в журнал (§12.2, п. 8).
5. **Разнообразие** внутри топа: ≤ `max_per_issuer` на эмитента (все ОФЗ — один эмитент, Минфин), ≤ `max_per_class`
   на бакет, ≤ `max_ideas` всего. Отобранные — `shown=True`.
6. **EntryPlan.** Цены — % номинала. `y_target = G(D) + медиана спреда группы + m·MAD`; если текущая доходность
   уже ≥ цели (или группы нет) — зона `[P − буфер, P]`, иначе верх зоны — цена при целевой доходности по
   `ΔP ≈ −D_mod·Δy·P` (первый порядок, без выпуклости и НКД; на сдвигах до ~100 б.п. ошибка — доли п. цены).
   Отмена: G-спред выше p90 группы; доходность выше `max(y, y_target) + breakeven`; понижение рейтинга;
   техдефолт. Бенчмарк — по бакету и дюрации из конфига.
"""

from __future__ import annotations

from bisect import bisect_right
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_FLOOR, ROUND_HALF_EVEN, Decimal

from investbot.adapters.moex_iss.models import BondMarketRow
from investbot.analytics.bonds import (
    BondMetrics,
    Curve,
    bond_metrics,
    from_bp,
    mad,
    median,
    pd_for_rating,
    price_for_yield,
    quantile,
    to_bp,
    years_from_days,
)
from investbot.core.models import AssetClass, CouponKind, InstrumentMeta
from investbot.core.money import ZERO, pct
from investbot.ideas.config import IdeasConfig
from investbot.ideas.models import EntryIdea, IdeaCandidate
from investbot.portfolio.buckets import RATING_ORDINAL, is_investment_grade, rating_ordinal
from investbot.rebalance.ips import IPS
from investbot.rebalance.models import CostModel

ARM = "bond_carry_roll"
IDEA_BUCKETS = ("ofz_fixed", "corp_ig")
OFZ_BOARDS = frozenset({"TQOB"})
SOVEREIGN_ISSUER = "Минфин России"
OFZ_GROUP = "ОФЗ"
PRICE_STEP = Decimal("0.01")  # шаг округления границ зоны входа, п. % номинала
BUCKET_NAMES = {"ofz_fixed": "ОФЗ с фиксированным купоном", "corp_ig": "корпоративные облигации IG"}
_RATING_LABEL = {v: k for k, v in RATING_ORDINAL.items()}

# Коды причин исключения (screen_bond). Рыночные влияют и на статистику групп, остальные — только на идеи.
NO_META = "no_meta"
FX_FACE = "fx_face"
FLOATING = "floating_or_linker"
NO_YIELD = "no_yield"
NO_DURATION = "no_duration"
NO_PRICE = "no_price"
NOT_IG = "not_ig"
BUCKET = "bucket_not_for_ideas"
BUCKET_CLOSED = "bucket_closed_in_ips"
NO_MATURITY = "no_maturity"
NEAR_EVENT = "near_event"
DURATION_BOUNDS = "duration_bounds"
ILLIQUID = "illiquid"
QUAL_ONLY = "qual_only"
NOT_BUYABLE = "not_buyable"
EXCLUDED_ISSUER = "excluded_issuer"
ISSUER_LIMIT = "issuer_limit"
NO_PD = "no_pd"


@dataclass(frozen=True)
class _Bond:
    """Бумага, прошедшая рыночные фильтры."""

    row: BondMarketRow
    meta: InstrumentMeta
    bucket: str
    is_ofz: bool
    rating: str | None  # канонический ("AA-"); None у ОФЗ
    issuer_key: str
    event_date: date
    event_kind: str  # maturity | offer | buyback | call
    h_days: int
    y: Decimal
    d_mac: Decimal
    price: Decimal
    group: tuple[str, int]  # (рейтинговая группа, номер корзины дюрации)


@dataclass(frozen=True)
class _Group:
    n: int
    median: Decimal
    mad: Decimal
    cancel: Decimal  # квантиль spread_cancel_quantile (p90)


@dataclass(frozen=True)
class _Scored:
    bond: _Bond
    m: BondMetrics
    pd: Decimal
    spread_group: _Group | None
    underweight: bool
    held_share: Decimal | None


# --- вселенная ------------------------------------------------------------------------------------------------


def _is_ofz(row: BondMarketRow, meta: InstrumentMeta) -> bool:
    return row.board in OFZ_BOARDS or meta.is_government


def _next_event(row: BondMarketRow, meta: InstrumentMeta, as_of: date) -> tuple[date, str] | None:
    maturity = row.mat_date or meta.maturity
    if maturity is None or maturity < as_of:
        return None
    dates = [
        (row.offer_date, "offer"),
        (row.put_option_date, "offer"),
        (row.buyback_date, "buyback"),
        (row.call_option_date, "call"),
        (maturity, "maturity"),
    ]
    return min((d, kind) for d, kind in dates if d is not None and d >= as_of)


def _duration_bucket(d_mac: Decimal, cfg: IdeasConfig) -> int:
    return bisect_right(cfg.duration_bucket_edges, d_mac)


def _group_label(group: tuple[str, int], cfg: IdeasConfig) -> str:
    rating, i = group
    edges = cfg.duration_bucket_edges
    if i == 0:
        dur = f"до {_num(edges[0], 0)} г."
    elif i == len(edges):
        dur = f"{_num(edges[-1], 0)}+ г."
    else:
        dur = f"{_num(edges[i - 1], 0)}–{_num(edges[i], 0)} г."
    return f"{rating} × {dur}"


def _market(row: BondMarketRow, meta: InstrumentMeta | None, as_of: date, cfg: IdeasConfig, ips: IPS) -> _Bond | str:
    """Рыночные фильтры: тип бумаги, данные, рейтинг, события, дюрация, ликвидность."""
    if meta is None:
        return NO_META  # без справочника брокера не проверить доступ и рейтинг
    if row.face_unit not in (None, "rub") or meta.currency != "rub":
        return FX_FACE
    if meta.coupon_kind in (CouponKind.FLOATING, CouponKind.LINKER):
        return FLOATING
    if row.effective_yield is None:
        return NO_YIELD  # у флоатеров и ночью ISS отдаёт 0 → None
    if row.duration_days is None:
        return NO_DURATION
    if row.price_pct is None:
        return NO_PRICE
    is_ofz = _is_ofz(row, meta)
    rating: str | None = None
    if not is_ofz:
        if not is_investment_grade(meta.rating, ips.rules.ig_min_rating):
            return NOT_IG  # ВДО и бумаги без рейтинга в v1 не предлагаются
        ordinal = rating_ordinal(meta.rating)
        rating = None if ordinal is None else _RATING_LABEL[ordinal]
    default_bucket = "ofz_fixed" if is_ofz else "corp_ig"
    bucket = ips.instrument_buckets.get(meta.ticker, ips.instrument_buckets.get(row.secid, default_bucket))
    if bucket not in IDEA_BUCKETS:
        return BUCKET
    target = ips.buckets.get(bucket)
    if target is None or target.bounds()[1] <= 0:
        return BUCKET_CLOSED
    event = _next_event(row, meta, as_of)
    if event is None:
        return NO_MATURITY
    days = (event[0] - as_of).days
    if days <= cfg.min_days_to_event:
        return NEAR_EVENT
    d_mac = years_from_days(row.duration_days)
    if not cfg.min_duration_years <= d_mac <= cfg.max_duration_years:
        return DURATION_BOUNDS
    if (row.num_trades or 0) < cfg.min_trades or (row.val_today or ZERO) < cfg.min_val_today:
        return ILLIQUID
    group_rating = OFZ_GROUP if is_ofz else (rating or "").rstrip("+-")
    return _Bond(
        row=row,
        meta=meta,
        bucket=bucket,
        is_ofz=is_ofz,
        rating=rating,
        issuer_key=meta.issuer or (SOVEREIGN_ISSUER if is_ofz else row.secid),
        event_date=event[0],
        event_kind=event[1],
        h_days=min(cfg.horizon_weeks * 7, days),
        y=row.effective_yield / 100,
        d_mac=d_mac,
        price=row.price_pct,
        group=(group_rating, _duration_bucket(d_mac, cfg)),
    )


def _investor(
    b: _Bond, cfg: IdeasConfig, ips: IPS, *, qualified: bool, held_issuers: Mapping[str, Decimal] | None
) -> str | None:
    """Фильтры инвестора: квал, доступность покупки, эмитенты IPS, PD."""
    if b.meta.for_qual_only and not qualified:
        return QUAL_ONLY
    if not b.meta.buy_available:
        return NOT_BUYABLE
    issuer = b.meta.issuer
    if issuer is not None and issuer in ips.limits.exclude_issuers:
        return EXCLUDED_ISSUER
    if not b.is_ofz:
        share = (held_issuers or {}).get(b.issuer_key)
        if share is not None and share >= ips.limits.max_issuer_total:
            return ISSUER_LIMIT  # к ОФЗ лимит эмитента не применяется: суверенный риск
        if pd_for_rating(b.rating, cfg.pd_annual) is None:
            return NO_PD
    return None


def screen_bond(
    row: BondMarketRow,
    meta: InstrumentMeta | None,
    as_of: date,
    cfg: IdeasConfig,
    ips: IPS,
    *,
    qualified: bool,
    held_issuers: Mapping[str, Decimal] | None = None,
) -> str | None:
    """Причина исключения бумаги из вселенной идей (код из констант модуля) или None, если бумага допустима."""
    b = _market(row, meta, as_of, cfg, ips)
    if isinstance(b, str):
        return b
    return _investor(b, cfg, ips, qualified=qualified, held_issuers=held_issuers)


# --- группы и скоринг -----------------------------------------------------------------------------------------


def _groups(bonds: Iterable[_Bond], curve: Curve, cfg: IdeasConfig) -> dict[tuple[str, int], _Group]:
    spreads: dict[tuple[str, int], list[Decimal]] = defaultdict(list)
    for b in bonds:
        spreads[b.group].append(b.y - curve(b.d_mac))
    out: dict[tuple[str, int], _Group] = {}
    for key, xs in spreads.items():
        if len(xs) >= cfg.min_group_size:
            out[key] = _Group(len(xs), median(xs), mad(xs), quantile(xs, cfg.spread_cancel_quantile))
    return out


def _rank_key(s: _Scored) -> tuple[int, Decimal, str]:
    return (0 if s.underweight else 1, -s.m.alpha_net, s.bond.row.secid)


def generate_bond_ideas(
    rows: Sequence[BondMarketRow],
    meta_by_isin: Mapping[str, InstrumentMeta],
    curve: Curve,
    r_f: Decimal,
    ips: IPS,
    costs: CostModel,
    as_of: date,
    cfg: IdeasConfig,
    *,
    qualified: bool,
    underweight_buckets: set[str],
    held_issuers: Mapping[str, Decimal] | None = None,
) -> tuple[list[EntryIdea], list[IdeaCandidate]]:
    """Идеи v1 по облигациям: (показанные идеи в порядке ранга, топ-кандидаты с флагом shown).

    `r_f` — эффективная годовая ставка денежного рынка (доля; см. analytics.bonds.effective_from_nominal).
    `held_issuers` — эмитент → текущая сквозная доля в NAV (доля); эмитент на лимите IPS исключается.
    """
    market: list[_Bond] = []
    seen: set[str] = set()
    for row in sorted(rows, key=lambda r: (r.secid, r.board)):
        if row.secid in seen:
            continue  # одна бумага в двух режимах — берём первый по алфавиту режима
        seen.add(row.secid)
        b = _market(row, meta_by_isin.get(row.isin) if row.isin else None, as_of, cfg, ips)
        if isinstance(b, _Bond):
            market.append(b)
    groups = _groups(market, curve, cfg)

    scored: list[_Scored] = []
    for b in market:
        if _investor(b, cfg, ips, qualified=qualified, held_issuers=held_issuers) is not None:
            continue
        pd = ZERO if b.is_ofz else (pd_for_rating(b.rating, cfg.pd_annual) or ZERO)
        m = bond_metrics(
            y=b.y,
            d_mac=b.d_mac,
            r_f=r_f,
            curve=curve,
            h=years_from_days(Decimal(b.h_days)),
            pd_annual=pd,
            lgd=cfg.lgd,
            cost=2 * costs.rate(b.row.secid),
        )
        group = groups.get(b.group)
        if group is not None and m.g_spread > group.cancel:
            continue  # условие отмены «спред выше p90 группы» уже выполнено
        if m.alpha_net <= cfg.min_alpha_net:
            continue  # барьер издержек
        held = None if b.is_ofz else (held_issuers or {}).get(b.issuer_key)
        scored.append(_Scored(b, m, pd, group, b.bucket in underweight_buckets, held))

    top = sorted(scored, key=_rank_key)[: cfg.max_candidates]
    per_issuer: Counter[str] = Counter()
    per_bucket: Counter[str] = Counter()
    ideas: list[EntryIdea] = []
    candidates: list[IdeaCandidate] = []
    for rank, s in enumerate(top, start=1):
        shown = (
            len(ideas) < cfg.max_ideas
            and per_issuer[s.bond.issuer_key] < cfg.max_per_issuer
            and per_bucket[s.bond.bucket] < cfg.max_per_class
        )
        idea = _idea(s, r_f, ips, as_of, cfg)
        if shown:
            per_issuer[s.bond.issuer_key] += 1
            per_bucket[s.bond.bucket] += 1
            ideas.append(idea)
        candidates.append(IdeaCandidate(idea=idea, rank=rank, shown=shown))
    return ideas, candidates


# --- EntryPlan и тексты ---------------------------------------------------------------------------------------


def _q(x: Decimal, places: int) -> Decimal:
    return x.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_EVEN)


def _floor(x: Decimal, step: Decimal = PRICE_STEP) -> Decimal:
    return x.quantize(step, rounding=ROUND_FLOOR)


def _num(x: Decimal, places: int) -> str:
    return str(_q(x, places)).replace(".", ",").replace("-", "−")


def _bp(x: Decimal) -> str:
    """Доля → «262 б.п.»."""
    return f"{_num(to_bp(x), 0)} б.п."


def _signed_pct(x: Decimal) -> str:
    return ("+" if x > 0 else "") + pct(x, 2)


def _date(d: date) -> str:
    return d.strftime("%d.%m.%Y")


def _entry_zone(s: _Scored, target: Decimal | None, cfg: IdeasConfig) -> tuple[Decimal, Decimal]:
    b = s.bond
    at_market = target is None or b.y >= target  # уже на цели или выше — покупка по рынку
    high = b.price if at_market or target is None else _floor(price_for_yield(b.price, s.m.d_mod, target - b.y))
    low = max(_floor(high - cfg.entry_buffer_pct), PRICE_STEP)
    return low, high


def _metrics(s: _Scored, r_f: Decimal) -> dict[str, Decimal]:
    b, m, g = s.bond, s.m, s.spread_group
    out = {
        "ytm": _q(m.y, 6),
        "r_f": _q(r_f, 6),
        "d_mac": _q(m.d_mac, 4),
        "d_mod": _q(m.d_mod, 4),
        "h_years": _q(years_from_days(Decimal(b.h_days)), 4),
        "g_curve": _q(m.g_curve, 6),
        "g_spread_bp": _q(to_bp(m.g_spread), 1),
        "carry": _q(m.carry, 6),
        "roll": _q(m.roll, 6),
        "pd": _q(s.pd, 6),
        "el": _q(m.el, 6),
        "cost": _q(m.cost, 6),
        "alpha_net": _q(m.alpha_net, 6),
        "breakeven_bp": _q(m.breakeven_bp, 1),
        "num_trades": Decimal(b.row.num_trades or 0),
        "val_today": _q(b.row.val_today or ZERO, 0),
    }
    if b.row.gspread_bp is not None:
        out["iss_gspread_bp"] = b.row.gspread_bp
    if g is not None:
        out |= {
            "group_n": Decimal(g.n),
            "group_median_bp": _q(to_bp(g.median), 1),
            "group_mad_bp": _q(to_bp(g.mad), 1),
            "group_p90_bp": _q(to_bp(g.cancel), 1),
        }
        if g.mad > 0:
            out["spread_z"] = _q((m.g_spread - g.median) / g.mad, 2)
    return out


def _idea(s: _Scored, r_f: Decimal, ips: IPS, as_of: date, cfg: IdeasConfig) -> EntryIdea:
    b, m, g = s.bond, s.m, s.spread_group
    target = None if g is None else m.g_curve + g.median + cfg.target_mad_k * g.mad
    low, high = _entry_zone(s, target, cfg)
    weeks = max(1, b.h_days // 7)
    label = _group_label(b.group, cfg)

    reasons = [
        f"Доходность {pct(m.y, 2)} {_yield_to(b)}, дюрация {_num(m.d_mac, 2)} г.",
        f"Carry к ставке денег {pct(r_f, 2)}: {_signed_pct(m.carry)} за {weeks} нед.",
    ]
    if m.roll > 0:
        reasons.append(f"Roll-down по КБД: {_signed_pct(m.roll)} за горизонт при неизменной кривой")
    if g is not None:
        reasons.append(f"G-спред {_bp(m.g_spread)} при медиане группы {label} {_bp(g.median)} ({g.n} бумаг)")
    else:
        reasons.append(f"G-спред к КБД {_bp(m.g_spread)}")
    reasons.append(
        f"Запас до безубыточности к денежному рынку: доходность может вырасти на {_num(m.breakeven_bp, 0)} б.п."
    )
    reasons.append(
        f"После круговых издержек {pct(m.cost, 2)} и ожидаемых потерь {pct(m.el, 2)}: "
        f"{_signed_pct(m.alpha_net)} за {weeks} нед."
    )
    if s.underweight:
        reasons.append(f"Бакет «{BUCKET_NAMES[b.bucket]}» ниже цели IPS")

    risks = [_duration_risk(m.d_mod, cfg)]
    if b.is_ofz:
        risks.append("Кредитный риск ОФЗ принят равным нулю (допущение): главный риск — процентный")
    else:
        risks.append(
            f"Кредитный риск {b.rating}: ожидаемые потери ≈ {pct(m.el, 2)} за горизонт "
            f"(PD {pct(s.pd, 2)} в год × LGD {pct(cfg.lgd, 0)} — допущения, не проверены)"
        )
    risks.append(
        f"Ликвидность оценена по одной сессии: {b.row.num_trades or 0} сделок, "
        f"оборот {_num((b.row.val_today or ZERO) / Decimal(1_000_000), 1)} млн ₽ (прокси ADV20)"
    )
    if b.event_kind in ("offer", "buyback"):
        risks.append(
            f"Оферта {_date(b.event_date)}: горизонт и доходность — до оферты, после неё купон могут пересмотреть"
        )
    elif b.event_kind == "call":
        risks.append(f"Call-опцион {_date(b.event_date)}: эмитент может погасить выпуск досрочно")
    elif b.h_days < cfg.horizon_weeks * 7:
        risks.append(f"Погашение {_date(b.event_date)} раньше конца горизонта: деньги придётся реинвестировать")
    if g is None:
        risks.append(f"В группе {label} меньше {cfg.min_group_size} бумаг: сравнение спреда с аналогами недоступно")
    elif g.mad > 0 and (m.g_spread - g.median) / g.mad > 1:
        risks.append("Спред заметно выше медианы группы: «дешевизна» может отражать риск, который не видит модель")
    if target is not None and b.y < target:
        risks.append(
            f"Доходность {pct(m.y, 2)} ниже целевой {pct(target, 2)}: вход только по лимиту, сделки может не быть"
        )
    if b.meta.coupon_kind == CouponKind.UNKNOWN:
        risks.append("Тип купона не подтверждён справочником брокера: проверьте, что купон фиксированный")
    if s.held_share is not None and s.held_share > 0:
        risks.append(
            f"Эмитент уже в портфеле: {pct(s.held_share, 1)} NAV при лимите IPS {pct(ips.limits.max_issuer_total, 0)}"
        )

    y_cancel = (m.y if target is None else max(m.y, target)) + from_bp(m.breakeven_bp)
    invalidation = []
    if g is not None:
        invalidation.append(f"G-спред выше {_bp(g.cancel)} — p90 группы {label}")
    invalidation.append(
        f"Доходность выше {pct(y_cancel, 2)}: запас breakeven {_num(m.breakeven_bp, 0)} б.п. от уровня входа исчерпан"
    )
    if not b.is_ofz:
        invalidation.append(
            f"Понижение рейтинга ниже {b.rating} (порог IPS — {ips.rules.ig_min_rating}) или статус «под наблюдением»"
        )
        invalidation.append(f"Техдефолт или дефолт эмитента либо другой бумаги группы {label}")

    return EntryIdea(
        idea_id=f"{ARM}:{b.row.secid}:{as_of.isoformat()}",
        as_of=as_of,
        asset_class=AssetClass.BOND,
        ticker=b.row.secid,
        isin=b.row.isin,
        name=b.meta.name or b.row.shortname or b.row.secid,
        issuer=b.meta.issuer or (SOVEREIGN_ISSUER if b.is_ofz else None),
        bucket=b.bucket,
        source_arm=ARM,
        score=_q(m.alpha_net, 6),
        metrics=_metrics(s, r_f),
        price=b.price,
        price_unit="pct_nominal",
        entry_low=low,
        entry_high=high,
        target_yield=None if target is None else _q(target, 6),
        valid_until=as_of + timedelta(days=cfg.valid_days),
        horizon_weeks=weeks,
        benchmark=cfg.benchmark(b.bucket, b.d_mac),
        invalidation=tuple(invalidation),
        reasons=tuple(reasons),
        risks=tuple(risks),
        for_underweight_bucket=s.underweight,
    )


def _yield_to(b: _Bond) -> str:
    """К какой дате ISS посчитал EFFECTIVEYIELD (YIELDDATE): к оферте или к погашению."""
    maturity = b.row.mat_date or b.meta.maturity
    yd = b.row.yield_date
    if yd is not None and maturity is not None and yd < maturity:
        return f"к оферте {_date(yd)}"
    return f"к погашению {_date(maturity)}" if maturity is not None else "к погашению"


def _duration_risk(d_mod: Decimal, cfg: IdeasConfig) -> str:
    parts = [f"+{shock} б.п. → цена ≈ −{pct(d_mod * from_bp(Decimal(shock)), 1)}" for shock in cfg.rate_shocks_bp]
    return f"Процентный риск (D_mod {_num(d_mod, 2)} г.): рост доходности " + "; ".join(parts)
