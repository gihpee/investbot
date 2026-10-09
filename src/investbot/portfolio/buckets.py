"""Отнесение позиций к бакетам IPS.

Правила по умолчанию детерминированы и прозрачны; всё, что не удалось классифицировать,
попадает в `unclassified` и показывается пользователю — молча не угадываем.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from investbot.core.models import AssetClass, CouponKind, FundFocus, InstrumentMeta

UNCLASSIFIED = "unclassified"

# Единая шкала рейтингов: AAA = 1 … D = 22 (национальные шкалы АКРА, Эксперт РА, НКР, НРА совпадают по буквам).
_SCALE = [
    "AAA", "AA+", "AA", "AA-", "A+", "A", "A-", "BBB+", "BBB", "BBB-", "BB+", "BB", "BB-",
    "B+", "B", "B-", "CCC+", "CCC", "CCC-", "CC", "C", "D",
]
RATING_ORDINAL = {r: i + 1 for i, r in enumerate(_SCALE)}
_RATING_RE = re.compile(r"(AAA|AA|A|BBB|BB|B|CCC|CC|C|D)([+-]?)")


def rating_ordinal(raw: str | None) -> int | None:
    """«ruAA-», «AA-(RU)», «A+.ru», «BBB|ru|» → порядковый номер; None — нет рейтинга или не распознан."""
    if not raw:
        return None
    s = raw.upper().replace("RU", "").replace("(", "").replace(")", "").replace("|", "").replace(".", "")
    m = _RATING_RE.search(s)
    if not m:
        return None
    return RATING_ORDINAL.get(m.group(1) + m.group(2))


def is_investment_grade(rating: str | None, ig_min: str) -> bool:
    """IG — не ниже порога IPS (по умолчанию A-). Без рейтинга — не IG (ВДО)."""
    ordinal = rating_ordinal(rating)
    threshold = RATING_ORDINAL[ig_min]
    return ordinal is not None and ordinal <= threshold


def classify(meta: InstrumentMeta, ig_min: str, overrides: Mapping[str, str] | None = None) -> str:
    if overrides and meta.ticker in overrides:
        return overrides[meta.ticker]
    match meta.asset_class:
        case AssetClass.EQUITY:
            return "equity_ru"
        case AssetClass.DFA:
            return "dfa"
        case AssetClass.CURRENCY:
            return "money_market" if meta.currency == "rub" else "fx_cash"
        case AssetClass.BOND:
            if meta.currency != "rub":
                return "fx_cash"  # замещающие и юаневые облигации — валютная экспозиция (лимит max_fx_exposure)
            if meta.is_government:
                if meta.coupon_kind in (CouponKind.FLOATING, CouponKind.LINKER):
                    return "ofz_float_linker"
                return "ofz_fixed"
            return "corp_ig" if is_investment_grade(meta.rating, ig_min) else "corp_hy"
        case AssetClass.FUND:
            match meta.fund_focus:
                case FundFocus.MONEY_MARKET:
                    return "money_market" if meta.currency == "rub" else "fx_cash"
                case FundFocus.EQUITY:
                    return "equity_ru"
                case FundFocus.GOLD:
                    return "gold"
                case _:
                    return UNCLASSIFIED  # облигационные и смешанные фонды — через instrument_buckets
        case _:
            return UNCLASSIFIED
