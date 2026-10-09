"""Ручной портфель в Telegram: /edit (§13.2), «✍️ Добавить вручную» (§13.3), мастер ЦФА (§13.4, п. 6).

Ручные счета — бумаги у других брокеров, ЦФА (смарт-счёт Т-Банка и другие операторы) и режим без токена.
Счета Т-Инвестиций с подключённым токеном синхронизируются из API, ручные позиции на них запрещены (ADR-06):
это проверяет сервис `ManualEditor`, канал только показывает его отказ.

Приватность (§13.1): сообщения хранятся у Telegram, поэтому бот не повторяет введённые количества и суммы.
В списках — только счета, тикеры, типы и валюты; после ввода количества или суммы — короткое «записано».

Мастера построены на aiogram FSM. Хранилище состояний — MemoryStorage диспетчера (бот однопользовательский),
в данные FSM кладутся только строки и флаги, чтобы хранилище можно было сменить без миграций.
Роутер стоит подключать последним: шаги мастера принимают свободный текст.
"""

from __future__ import annotations

import contextlib
import re
import zlib
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from html import escape
from typing import Any, Final, Protocol, TypeVar

import structlog
from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from investbot.core.models import AccountKind, AssetClass, CouponKind, FundFocus
from investbot.portfolio.buckets import RATING_ORDINAL, rating_ordinal
from investbot.portfolio.manual import ManualAccount, ManualPortfolio, ManualPortfolioError, ManualPosition
from investbot.services.manual import slugify

log = structlog.get_logger(__name__)

T = TypeVar("T")

BTN_MANUAL: Final = "✍️ Ручной портфель"
CB_MAX: Final = 64  # лимит callback_data в байтах (§13.1)
ACC_REF_MAX: Final = 44  # id счёта длиннее — ссылка по индексу (slugify даёт до 40 символов + суффикс «_N»)
DEFAULT_NOMINAL: Final = Decimal(1000)
NAME_MAX: Final = 80

# callback_data: префикс «me:» (manual edit), ссылки на счёт и позицию — см. item_ref
CB_LIST: Final = "me:l"
CB_HELP: Final = "me:h"
CB_NEW: Final = "me:n"
CB_CANCEL: Final = "me:x"
CB_SKIP: Final = "me:s"
CB_SAVE: Final = "me:ok"
CB_CHOICE: Final = "me:c:"
CB_OPEN: Final = "me:o:"
CB_ADD_SECURITY: Final = "me:as:"
CB_ADD_DFA: Final = "me:ad:"
CB_CASH: Final = "me:ac:"
CB_DEL_PICK: Final = "me:dp:"
CB_DEL_ASK: Final = "me:dq:"
CB_DEL_YES: Final = "me:dy:"
CB_DROP_ASK: Final = "me:da:"
CB_DROP_YES: Final = "me:dz:"

PRIVACY_LINE: Final = ("Введённые данные хранятся у Telegram — при желании вместо чата можно править "
                       "<code>config/manual_portfolio.yaml</code> на сервере.")
HELP: Final = "\n".join([
    "<b>Зачем ручные счета</b>",
    "• Бумаги у других брокеров — бот учтёт их в долях, лимитах и плане ребалансировки.",
    "• ЦФА: смарт-счёт Т-Банка (API не отдаёт его позиции) и ЦФА других операторов — карточками.",
    "• Режим без токена (<code>tinvest_sync: off</code>): счета Т-Инвестиций ведутся здесь вручную.",
    "",
    "Счета Т-Инвестиций при подключённом токене синхронизируются автоматически: количества и деньги "
    "берутся из API, ручные позиции на них запрещены, чтобы не было двойного учёта.",
    "",
    "Количества и суммы бот в чат не повторяет. " + PRIVACY_LINE,
])

KIND_TITLES: Final[Mapping[AccountKind, str]] = {
    AccountKind.BROKER: "брокерский",
    AccountKind.IIS: "ИИС",
    AccountKind.DFA: "ЦФА",
    AccountKind.INVEST_BOX: "Инвесткопилка",
    AccountKind.INVEST_FUND: "инвестфонд",
    AccountKind.OTHER: "другое",
}
NEW_ACCOUNT_KINDS: Final = (
    (AccountKind.BROKER, "Брокерский"), (AccountKind.IIS, "ИИС"),
    (AccountKind.DFA, "ЦФА"), (AccountKind.OTHER, "Другое"),
)
CLASS_TITLES: Final[Mapping[AssetClass, str]] = {
    AssetClass.EQUITY: "акция",
    AssetClass.BOND: "облигация",
    AssetClass.FUND: "фонд",
    AssetClass.DFA: "ЦФА",
    AssetClass.CURRENCY: "валюта",
    AssetClass.OTHER: "другое",
}
SECURITY_CLASSES: Final = (
    (AssetClass.EQUITY, "Акция"), (AssetClass.BOND, "Облигация"), (AssetClass.FUND, "Фонд"),
)
COUPON_TITLES: Final[Mapping[CouponKind, str]] = {
    CouponKind.FIXED: "фикс", CouponKind.FLOATING: "флоатер", CouponKind.LINKER: "линкер",
}
FOCUS_TITLES: Final[Mapping[FundFocus, str]] = {
    FundFocus.EQUITY: "акции",
    FundFocus.FIXED_INCOME: "облигации",
    FundFocus.MONEY_MARKET: "денежный рынок",
    FundFocus.GOLD: "золото",
    FundFocus.MIXED: "смешанный",
}
DFA_KINDS: Final[Mapping[str, str]] = {"fix": "фиксированный доход", "ind": "индикаторный"}
FREQUENCIES: Final[Mapping[str, str]] = {
    "m": "ежемесячно", "q": "ежеквартально", "h": "раз в полгода", "y": "раз в год", "e": "в конце срока",
}
CASH_CHOICES: Final = (("rub", "₽"), ("cny", "¥"), ("usd", "$"))
CURRENCY_SIGNS: Final[Mapping[str, str]] = {"rub": "₽", "cny": "¥", "usd": "$", "eur": "€"}
SKIP_WORDS: Final = frozenset({"пропустить", "пропуск", "нет", "skip", "no", "-", "—", "–"})


class ManualEditorLike(Protocol):
    """Методы `services.manual.ManualEditor`, которые нужны каналу."""

    async def portfolio(self) -> ManualPortfolio: ...

    async def create_account(self, name: str, kind: AccountKind,
                             tinvest_account_id: str | None = None) -> ManualAccount: ...

    async def delete_account(self, account_id: str) -> None: ...

    async def save_position(self, account_id: str, position: ManualPosition) -> list[str]: ...

    async def delete_position(self, account_id: str, ticker: str) -> None: ...

    async def set_cash(self, account_id: str, currency: str, amount: Decimal) -> None: ...


class AccountForm(StatesGroup):
    name = State()
    kind = State()
    smart_id = State()


class SecurityForm(StatesGroup):
    asset_class = State()
    ticker = State()
    name = State()
    qty = State()
    price = State()
    nominal = State()
    government = State()
    coupon = State()
    rating = State()
    focus = State()
    confirm = State()


class DfaForm(StatesGroup):
    operator = State()
    issuer = State()
    rating = State()
    kind = State()
    nominal = State()
    qty = State()
    rate = State()
    frequency = State()
    maturity = State()
    market_maker = State()
    underlying = State()
    price = State()
    confirm = State()


class CashForm(StatesGroup):
    currency = State()
    code = State()
    amount = State()


WIZARDS: Final = (AccountForm, SecurityForm, DfaForm, CashForm)
NOT_COMMAND: Final = ~F.text.startswith("/")


# ---------------------------------------------------------------- разбор ввода (чистые функции)

_STRIP = str.maketrans("", "", "    '_")
_UNIT_TAILS = ("руб.", "руб", "р.", "р", "₽", "rub", "шт.", "шт", "$", "¥", "€")
_MULTIPLIERS = (("тыс.", 1000), ("тыс", 1000), ("млн", 1_000_000), ("к", 1000), ("k", 1000),
                ("м", 1_000_000), ("m", 1_000_000))
_NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)")
_GROUPED = {sep: re.compile(rf"[+-]?\d{{1,3}}(?:\{sep}\d{{3}})+") for sep in ",."}
_LIMIT = Decimal("1e15")
_DATE_FORMATS = ("%d.%m.%Y", "%Y-%m-%d", "%d.%m.%y", "%d/%m/%Y")
_TICKER = re.compile(r"[A-ZА-ЯЁ0-9][A-ZА-ЯЁ0-9._\-]{0,31}")
_SMART_ID = re.compile(r"[0-9A-Za-z_\-]{4,40}")
_CURRENCY_CODE = re.compile(r"[a-z]{3}")
_CYR_LOOKALIKES = str.maketrans("АВСЕ", "ABCE")
_CANONICAL_RATING = {v: k for k, v in RATING_ORDINAL.items()}


def tidy(v: Decimal) -> Decimal:
    """Убрать хвостовые нули без экспоненты: 1500.0 → 1500, 98.50 → 98.5."""
    return v.quantize(Decimal(1)) if v == v.to_integral_value() else v.normalize()


def _decimal_point(s: str) -> str:
    """Привести разделители к виду «1500.5»; неразборчивое → "" (дальше отсеется регуляркой)."""
    if "," in s and "." in s:  # «1.500,5» или «1,500.5»: последний разделитель — дробный
        decimal_sep = "," if s.rfind(",") > s.rfind(".") else "."
        thousands = "." if decimal_sep == "," else ","
        whole, _, frac = s.rpartition(decimal_sep)
        if not _GROUPED[thousands].fullmatch(whole):
            return ""
        return whole.replace(thousands, "") + "." + frac
    for sep in ",.":
        if s.count(sep) > 1:  # «1,500,000» / «1.500.000» — только разделители тысяч по 3 цифры
            return s.replace(sep, "") if _GROUPED[sep].fullmatch(s) else ""
    return s.replace(",", ".")  # одна запятая — дробная часть (русская запись): «1,500» = 1,5


def parse_decimal(raw: str) -> Decimal | None:
    """«1 500,5», «1500.5», «1,5к», «2 млн», «150 ₽», «−3» → Decimal; мусор → None. Знак сохраняется."""
    s = raw.strip().lower().translate(_STRIP).replace("−", "-").lstrip("₽$¥€")
    for tail in _UNIT_TAILS:
        if s.endswith(tail):
            s = s[: -len(tail)]
            break
    mult = 1
    for suffix, m in _MULTIPLIERS:
        if s.endswith(suffix):
            s, mult = s[: -len(suffix)], m
            break
    s = _decimal_point(s)
    if not _NUMBER.fullmatch(s):
        return None
    try:
        v = Decimal(s) * mult
    except InvalidOperation:
        return None
    return tidy(v) if abs(v) < _LIMIT else None


def parse_price(raw: str) -> tuple[Decimal, bool] | None:
    """Цена > 0: «98,5%» → (98.5, True) — процент номинала; «1 234,5» → (1234.5, False); мусор → None."""
    is_pct = "%" in raw
    v = parse_decimal(raw.replace("%", ""))
    if v is None or v <= 0:
        return None
    return v, is_pct


def parse_rate(raw: str) -> Decimal | None:
    """Ставка в % годовых: «18,5%», «18.5» → 18.5; вне (0; 100] → None."""
    v = parse_decimal(raw.replace("%", ""))
    return v if v is not None and 0 < v <= 100 else None


def parse_date(raw: str) -> date | None:
    """«30.06.2027», «2027-06-30», «30.06.27», «30/06/2027» → date; иначе None."""
    s = raw.strip().replace(" ", "")
    for fmt in _DATE_FORMATS:
        try:
            d = datetime.strptime(s, fmt).date()
        except ValueError:
            continue
        return d if 1990 <= d.year <= 2100 else None
    return None


def parse_rating(raw: str) -> str | None:
    """«AA-», «ruA+», «A+(RU)», кириллические «АА-» → каноническая буква шкалы; нераспознанное → None."""
    ordinal = rating_ordinal(raw.strip().upper().translate(_CYR_LOOKALIKES).replace(" ", ""))
    return _CANONICAL_RATING.get(ordinal) if ordinal is not None else None


def normalize_ticker(raw: str) -> str | None:
    """Тикер, SECID или ISIN: верхний регистр, без пробелов, до 32 символов."""
    s = raw.strip().upper()
    return s if _TICKER.fullmatch(s) else None


def is_skip(raw: str) -> bool:
    return raw.strip().lower().rstrip(".!") in SKIP_WORDS


def clean_text(raw: str, limit: int = NAME_MAX) -> str | None:
    s = " ".join(raw.split())
    return s if 0 < len(s) <= limit else None


def num(x: Decimal) -> str:
    """1000 → «1 000», 98.5 → «98,5»."""
    v = tidy(x)
    whole, _, frac = f"{abs(v):f}".partition(".")
    sign = "−" if v < 0 else ""
    return sign + f"{int(whole):,}".replace(",", " ") + ("," + frac if frac else "")


# ---------------------------------------------------------------- карточки из ответов мастера

def _dec(v: Any) -> Decimal | None:
    return None if v is None or v == "" else Decimal(str(v))


def security_card(data: Mapping[str, Any], today: date) -> ManualPosition:
    """Ответы мастера «➕ Бумага» → ManualPosition. Ошибки данных — ValueError (включая ValidationError)."""
    cls = AssetClass(data["cls"])
    if cls not in {c for c, _ in SECURITY_CLASSES}:
        raise ValueError(f"мастер бумаг не создаёт класс {cls.value}")
    price, is_pct = _dec(data.get("price")), bool(data.get("pct"))
    bond = cls is AssetClass.BOND
    if is_pct and not bond:
        raise ValueError("цена в % номинала бывает только у облигаций")
    nominal = _dec(data.get("nominal"))
    if is_pct and nominal is None:
        nominal = DEFAULT_NOMINAL
    government = bond and bool(data.get("gov"))
    return ManualPosition(
        ticker=str(data["ticker"]),
        name=str(data.get("name") or ""),
        asset_class=cls,
        qty=Decimal(str(data["qty"])),
        price=None if is_pct else price,
        price_pct=price if is_pct else None,
        nominal=nominal if bond else None,
        price_date=today if price is not None else None,
        is_government=government,
        coupon_kind=CouponKind(data["coupon"]) if bond and data.get("coupon") else CouponKind.UNKNOWN,
        rating=str(data["rating"]) if bond and not government and data.get("rating") else None,
        fund_focus=FundFocus(data["focus"]) if cls is AssetClass.FUND and data.get("focus") else None,
    )


def dfa_ticker(issuer: str, maturity: date | None) -> str:
    """Код карточки ЦФА (ключ позиции на счёте): эмитент латиницей + дата погашения."""
    slug = slugify(issuer).upper()[:16].strip("_")
    return "DFA-" + slug + (maturity.strftime("-%Y%m%d") if maturity else "")


def dfa_name(data: Mapping[str, Any]) -> str:
    """«Эмитент, ставка …» плюс тип по 7176-У и маркетмейкер: отдельных полей для них в модели нет."""
    head = str(data["issuer"]).strip()
    rate = _dec(data.get("rate"))
    if rate is not None:
        freq = FREQUENCIES.get(str(data.get("freq") or ""))
        head += f", {num(rate)}%" + (f" {freq}" if freq else "")
    kind = "7176-У: " + ("фикс. доход" if data.get("dkind") == "fix" else "индикаторный")
    if data.get("dkind") == "ind" and data.get("underlying"):
        kind += f" ({data['underlying']})"
    parts = [head, kind]
    if data.get("mm") is not None:
        parts.append("ММ: есть" if data["mm"] else "ММ: нет")
    return " · ".join(parts)


def dfa_card(data: Mapping[str, Any], today: date) -> ManualPosition:
    """Ответы мастера ЦФА (§13.4, п. 6) → ManualPosition(asset_class=DFA)."""
    maturity = date.fromisoformat(str(data["maturity"])) if data.get("maturity") else None
    price = _dec(data.get("price"))
    return ManualPosition(
        ticker=str(data.get("ticker") or dfa_ticker(str(data["issuer"]), maturity)),
        name=dfa_name(data),
        asset_class=AssetClass.DFA,
        qty=Decimal(str(data["qty"])),
        nominal=_dec(data.get("nominal")),
        price=price,
        price_date=today if price is not None else None,
        rating=str(data["rating"]) if data.get("rating") else None,
        operator=str(data["operator"]) if data.get("operator") else None,
        maturity=maturity,
    )


# ---------------------------------------------------------------- ссылки в callback_data

def _crc(value: str) -> str:
    return f"{zlib.crc32(value.encode()) & 0xFFFF:04x}"


def item_ref(value: str, idx: int, budget: int) -> str:
    """Ссылка на счёт или позицию: сам id, если он короткий и без «:», иначе «#индекс~crc16» (≤ 10 байт)."""
    if value and ":" not in value and not value.startswith("#") and len(value.encode()) <= budget:
        return value
    return f"#{idx}~{_crc(value)}"


def resolve_ref(values: Sequence[str], ref: str) -> int | None:
    """Обратное к item_ref; индекс проверяется по crc, чтобы не удалить не ту позицию после правки файла."""
    if ref.startswith("#"):
        idx, _, crc = ref[1:].partition("~")
        if idx.isascii() and idx.isdigit() and int(idx) < len(values) and _crc(values[int(idx)]) == crc:
            return int(idx)
        return None
    return values.index(ref) if ref in values else None


def acc_ref(account_id: str, idx: int) -> str:
    return item_ref(account_id, idx, ACC_REF_MAX)


def pos_callback(prefix: str, account_ref: str, ticker: str, idx: int) -> str:
    head = f"{prefix}{account_ref}:"
    return head + item_ref(ticker, idx, CB_MAX - len(head.encode()))


# ---------------------------------------------------------------- экраны

Rows = Sequence[Sequence[tuple[str, str]]]
Step = Callable[[Message, FSMContext], Awaitable[None]]


def kb(rows: Rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows]
    )


def with_cancel(rows: Rows = ()) -> InlineKeyboardMarkup:
    return kb([*rows, [("✖ Отмена", CB_CANCEL)]])


def _short(s: str, limit: int) -> str:
    return s if len(s) <= limit else s[: limit - 1] + "…"


def cash_signs(cash: Mapping[str, Decimal]) -> str:
    """Только валюты остатков, без сумм."""
    return ", ".join(CURRENCY_SIGNS.get(c.lower(), c.upper()) for c, v in cash.items() if v != 0)


def _account_line(a: ManualAccount) -> str:
    parts = [f"<b>{escape(a.name)}</b>", KIND_TITLES.get(a.kind, a.kind.value), f"позиций: {len(a.positions)}"]
    signs = cash_signs(a.cash)
    if signs:
        parts.append(f"деньги: {escape(signs)}")
    if a.tinvest_account_id:
        parts.append("смарт-счёт Т-Банка")
    return "• " + " · ".join(parts)


def accounts_view(mp: ManualPortfolio) -> tuple[str, InlineKeyboardMarkup]:
    """Список ручных счетов: имя, тип, число позиций, валюты денег — без количеств и сумм."""
    lines = ["<b>✍️ Ручной портфель</b>"]
    if not mp.accounts:
        lines += ["Ручных счетов пока нет.", "", HELP]
    else:
        lines.append("<i>Счета Т-Инвестиций синхронизируются автоматически и здесь не показываются.</i>")
        lines.append("")
        lines.extend(_account_line(a) for a in mp.accounts)
    rows: list[list[tuple[str, str]]] = [
        [(f"📁 {_short(a.name, 32)}", CB_OPEN + acc_ref(a.id, i))] for i, a in enumerate(mp.accounts)
    ]
    rows.append([("➕ Новый счёт", CB_NEW)] + ([("❓ Справка", CB_HELP)] if mp.accounts else []))
    return "\n".join(lines), kb(rows)


def help_view() -> tuple[str, InlineKeyboardMarkup]:
    return HELP, kb([[("↩️ К счетам", CB_LIST)]])


def position_line(p: ManualPosition) -> str:
    kind = CLASS_TITLES.get(p.asset_class, p.asset_class.value)
    if p.is_government:
        kind += " (ОФЗ)"
    name = f" · {escape(_short(p.name, 48))}" if p.name and p.name != p.ticker else ""
    return f"• <code>{escape(p.ticker)}</code> — {kind}{name}"


def account_view(acc: ManualAccount, idx: int) -> tuple[str, InlineKeyboardMarkup]:
    """Экран счёта: позиции (тикер, тип, название) и валюты денег — без количеств и сумм."""
    ref = acc_ref(acc.id, idx)
    lines = [f"<b>📁 {escape(acc.name)}</b> · {KIND_TITLES.get(acc.kind, acc.kind.value)}"]
    if acc.tinvest_account_id:
        lines.append("Привязан к смарт-счёту ЦФА Т-Банка: сумма карточек сверяется с API.")
    signs = cash_signs(acc.cash)
    lines.append(f"Деньги: {escape(signs)}" if signs else "Деньги: не указаны")
    lines.append("")
    if acc.positions:
        lines.append(f"<b>Позиции</b> ({len(acc.positions)}):")
        lines.extend(position_line(p) for p in acc.positions)
    else:
        lines.append("Позиций пока нет — добавьте бумагу или ЦФА.")
    lines.append("")
    lines.append("<i>Количества и суммы здесь не показываются, чтобы не оставлять их в истории Telegram.</i>")
    rows: list[list[tuple[str, str]]] = [
        [("➕ Бумага", CB_ADD_SECURITY + ref), ("➕ ЦФА", CB_ADD_DFA + ref)],
        [("💰 Деньги", CB_CASH + ref)],
    ]
    if acc.positions:
        rows.append([("🗑 Удалить позицию", CB_DEL_PICK + ref)])
    rows.append([("🗑 Удалить счёт", CB_DROP_ASK + ref)])
    rows.append([("↩️ Назад", CB_LIST)])
    return "\n".join(lines), kb(rows)


def delete_pick_view(acc: ManualAccount, idx: int) -> tuple[str, InlineKeyboardMarkup]:
    ref = acc_ref(acc.id, idx)
    rows = [[(f"🗑 {_short(p.ticker, 32)}", pos_callback(CB_DEL_ASK, ref, p.ticker, i))]
            for i, p in enumerate(acc.positions)]
    rows.append([("↩️ Назад", CB_OPEN + ref)])
    return f"Какую позицию удалить со счёта «{escape(acc.name)}»?", kb(rows)


def delete_confirm_view(acc: ManualAccount, idx: int, pos_idx: int) -> tuple[str, InlineKeyboardMarkup]:
    ref, p = acc_ref(acc.id, idx), acc.positions[pos_idx]
    text = (f"Удалить <code>{escape(p.ticker)}</code> со счёта «{escape(acc.name)}»?\n"
            "Запись об удалении останется в журнале.")
    return text, kb([[("🗑 Да, удалить", pos_callback(CB_DEL_YES, ref, p.ticker, pos_idx)),
                      ("Отмена", CB_OPEN + ref)]])


def drop_confirm_view(acc: ManualAccount, idx: int) -> tuple[str, InlineKeyboardMarkup]:
    ref = acc_ref(acc.id, idx)
    text = (f"Удалить счёт «{escape(acc.name)}» вместе со всеми позициями ({len(acc.positions)}) и деньгами?\n"
            "Бумаги этого счёта перестанут учитываться в долях и плане.")
    return text, kb([[("🗑 Да, удалить счёт", CB_DROP_YES + ref), ("Отмена", CB_OPEN + ref)]])


def card_summary(card: ManualPosition, account_name: str, *, replaces: bool = False) -> str:
    """Экран подтверждения: всё, кроме количества (оно не повторяется в чате)."""
    kind = CLASS_TITLES.get(card.asset_class, card.asset_class.value) + (" · ОФЗ" if card.is_government else "")
    lines = ["<b>Проверьте карточку</b>", f"Счёт: {escape(account_name)}", f"Тип: {kind}",
             f"{'Код карточки' if card.asset_class is AssetClass.DFA else 'Тикер'}: <code>{escape(card.ticker)}</code>"]
    if card.name:
        lines.append(f"Название: {escape(card.name)}")
    if card.operator:
        lines.append(f"Оператор: {escape(card.operator)}")
    lines.append("Количество: записано (в чат не повторяю)")
    if card.price_pct is not None:
        lines.append(f"Цена: {num(card.price_pct)}% номинала")
    elif card.price is not None:
        lines.append(f"Цена: {num(card.price)} ₽ за 1 шт.")
    elif card.asset_class is AssetClass.DFA:
        lines.append("Цена: по номиналу (модельная оценка)")
    else:
        lines.append("Цена: не указана — позиция не войдёт в оценку портфеля")
    if card.nominal is not None:
        lines.append(f"Номинал: {num(card.nominal)} ₽")
    if card.asset_class is AssetClass.BOND:
        lines.append(f"Купон: {COUPON_TITLES.get(card.coupon_kind, 'не указан')}")
    if card.asset_class in (AssetClass.BOND, AssetClass.DFA) and not card.is_government:
        lines.append(f"Рейтинг: {escape(card.rating) if card.rating else 'нет'}")
    if card.fund_focus is not None:
        lines.append(f"Фонд: {FOCUS_TITLES.get(card.fund_focus, card.fund_focus.value)}")
    if card.maturity is not None:
        lines.append(f"Погашение: {card.maturity:%d.%m.%Y}")
    if (card.asset_class is AssetClass.BOND and card.price is not None and card.price_pct is None
            and card.price < 200):
        lines.append("ℹ️ Цена указана в ₽. Если это процент номинала — отмените и введите её со знаком «%».")
    if replaces:
        lines.append(f"⚠️ На счёте уже есть <code>{escape(card.ticker)}</code> — карточка будет заменена.")
    return "\n".join(lines)


# ---------------------------------------------------------------- роутер

class _Failed:
    pass


FAILED: Final = _Failed()


def _choice(cb: CallbackQuery) -> str:
    return (cb.data or "").removeprefix(CB_CHOICE)


def _cb_message(cb: CallbackQuery) -> Message | None:
    return cb.message if isinstance(cb.message, Message) else None


def _reason(e: Exception) -> str:
    return str(e.args[0]) if isinstance(e, KeyError) and e.args else str(e)


async def _mark(msg: Message, label: str) -> None:
    """Отметить выбор под вопросом и убрать кнопки, чтобы их не нажали повторно."""
    with contextlib.suppress(TelegramBadRequest):
        await msg.edit_text(f"{msg.html_text}\n<b>→ {escape(label)}</b>", reply_markup=None)


async def _show(msg: Message, view: tuple[str, InlineKeyboardMarkup], *, edit: bool) -> None:
    text, markup = view
    if edit:
        try:
            await msg.edit_text(text, reply_markup=markup)
            return
        except TelegramBadRequest as e:
            if "not modified" in str(e):
                return
    await msg.answer(text, reply_markup=markup)


async def _ask(target: Message, state: FSMContext, step: State, text: str, rows: Rows = ()) -> None:
    await state.set_state(step)
    await target.answer(text, reply_markup=with_cancel(rows))


async def _reask(target: Message, text: str, rows: Rows = ()) -> None:
    await target.answer(text, reply_markup=with_cancel(rows))


SKIP_ROW: Final = (("Пропустить", CB_SKIP),)
YES_NO: Final = (("Да", CB_CHOICE + "y"), ("Нет", CB_CHOICE + "n"))
BAD_NUMBER: Final = "Не понял число. Примеры: 150, 1 500,5, 1,5к. Попробуйте ещё раз:"
BAD_RATING: Final = "Не распознал рейтинг. Примеры: AA-, ruA+, BBB. Если рейтинга нет — «нет»:"


def build_edit_router(editor: ManualEditorLike, today: date | None = None) -> Router:
    router = Router(name="manual_edit")

    def _today() -> date:
        return today or date.today()

    async def guarded(target: Message, op: Awaitable[T]) -> T | _Failed:
        """Ошибки сервиса → понятное сообщение; без трассировок в чате."""
        try:
            return await op
        except (ManualPortfolioError, KeyError) as e:
            await target.answer(f"⚠️ Не получилось: {escape(_reason(e))}")
        except Exception:
            log.exception("manual_edit.failed")
            await target.answer("⚠️ Не получилось: внутренняя ошибка (подробности — в логах сервера).")
        return FAILED

    async def find_account(msg: Message, ref: str) -> tuple[int, ManualAccount] | None:
        mp = await guarded(msg, editor.portfolio())
        if isinstance(mp, _Failed):
            return None
        i = resolve_ref([a.id for a in mp.accounts], ref)
        if i is None:
            await msg.answer("Счёт не найден — возможно, его уже удалили. Актуальный список:")
            await _show(msg, accounts_view(mp), edit=False)
            return None
        return i, mp.accounts[i]

    async def show_list(msg: Message, *, edit: bool) -> None:
        mp = await guarded(msg, editor.portfolio())
        if not isinstance(mp, _Failed):
            await _show(msg, accounts_view(mp), edit=edit)

    async def show_account(msg: Message, account_id: str, *, edit: bool) -> None:
        mp = await guarded(msg, editor.portfolio())
        if isinstance(mp, _Failed):
            return
        i = next((n for n, a in enumerate(mp.accounts) if a.id == account_id), None)
        await _show(msg, accounts_view(mp) if i is None else account_view(mp.accounts[i], i), edit=edit)

    async def start_wizard(cb: CallbackQuery, state: FSMContext, prefix: str) -> tuple[Message, ManualAccount] | None:
        await cb.answer()
        msg = _cb_message(cb)
        if msg is None:
            return None
        found = await find_account(msg, (cb.data or "").removeprefix(prefix))
        if found is None:
            return None
        acc = found[1]
        await state.clear()
        await state.set_data({"acc": acc.id, "acc_name": acc.name})
        return msg, acc

    # ------------------------------------------------ вход, список, справка, отмена

    @router.message(Command("edit"))
    @router.message(F.text == BTN_MANUAL)
    async def start(message: Message, state: FSMContext) -> None:
        await state.clear()
        await show_list(message, edit=False)

    @router.callback_query(F.data == CB_LIST)
    async def to_list(cb: CallbackQuery, state: FSMContext) -> None:
        await cb.answer()
        await state.clear()
        if (msg := _cb_message(cb)) is not None:
            await show_list(msg, edit=True)

    @router.callback_query(F.data == CB_HELP)
    async def help_screen(cb: CallbackQuery) -> None:
        await cb.answer()
        if (msg := _cb_message(cb)) is not None:
            await _show(msg, help_view(), edit=True)

    @router.callback_query(F.data == CB_CANCEL)
    async def cancel(cb: CallbackQuery, state: FSMContext) -> None:
        data = await state.get_data()
        await state.clear()
        await cb.answer("Отменено")
        msg = _cb_message(cb)
        if msg is None:
            return
        await _mark(msg, "отменено")
        if data.get("acc"):
            await show_account(msg, str(data["acc"]), edit=False)
        else:
            await show_list(msg, edit=False)

    # ------------------------------------------------ счёт: открыть, удалить позицию, удалить счёт

    @router.callback_query(F.data.startswith(CB_OPEN))
    async def open_account(cb: CallbackQuery, state: FSMContext) -> None:
        await cb.answer()
        await state.clear()
        msg = _cb_message(cb)
        if msg is None:
            return
        found = await find_account(msg, (cb.data or "").removeprefix(CB_OPEN))
        if found is not None:
            await _show(msg, account_view(found[1], found[0]), edit=True)

    @router.callback_query(F.data.startswith(CB_DEL_PICK))
    async def delete_pick(cb: CallbackQuery) -> None:
        await cb.answer()
        msg = _cb_message(cb)
        if msg is None:
            return
        found = await find_account(msg, (cb.data or "").removeprefix(CB_DEL_PICK))
        if found is not None:
            await _show(msg, delete_pick_view(found[1], found[0]), edit=True)

    async def find_position(cb: CallbackQuery, prefix: str) -> tuple[Message, int, ManualAccount, int] | None:
        msg = _cb_message(cb)
        if msg is None:
            return None
        account, _, position = (cb.data or "").removeprefix(prefix).partition(":")
        found = await find_account(msg, account)
        if found is None:
            return None
        i, acc = found
        j = resolve_ref([p.ticker for p in acc.positions], position)
        if j is None:
            await msg.answer("Позиция не найдена — список изменился.")
            await _show(msg, account_view(acc, i), edit=False)
            return None
        return msg, i, acc, j

    @router.callback_query(F.data.startswith(CB_DEL_ASK))
    async def delete_ask(cb: CallbackQuery) -> None:
        await cb.answer()
        found = await find_position(cb, CB_DEL_ASK)
        if found is not None:
            msg, i, acc, j = found
            await _show(msg, delete_confirm_view(acc, i, j), edit=True)

    @router.callback_query(F.data.startswith(CB_DEL_YES))
    async def delete_yes(cb: CallbackQuery) -> None:
        await cb.answer()
        found = await find_position(cb, CB_DEL_YES)
        if found is None:
            return
        msg, _, acc, j = found
        ticker = acc.positions[j].ticker
        if isinstance(await guarded(msg, editor.delete_position(acc.id, ticker)), _Failed):
            return
        await _mark(msg, f"{ticker} удалена")
        await show_account(msg, acc.id, edit=False)

    @router.callback_query(F.data.startswith(CB_DROP_ASK))
    async def drop_ask(cb: CallbackQuery) -> None:
        await cb.answer()
        msg = _cb_message(cb)
        if msg is None:
            return
        found = await find_account(msg, (cb.data or "").removeprefix(CB_DROP_ASK))
        if found is not None:
            await _show(msg, drop_confirm_view(found[1], found[0]), edit=True)

    @router.callback_query(F.data.startswith(CB_DROP_YES))
    async def drop_yes(cb: CallbackQuery) -> None:
        await cb.answer()
        msg = _cb_message(cb)
        if msg is None:
            return
        found = await find_account(msg, (cb.data or "").removeprefix(CB_DROP_YES))
        if found is None:
            return
        acc = found[1]
        if isinstance(await guarded(msg, editor.delete_account(acc.id)), _Failed):
            return
        await _mark(msg, "счёт удалён")
        await show_list(msg, edit=False)

    # ------------------------------------------------ мастер нового счёта

    @router.callback_query(F.data == CB_NEW)
    async def new_account(cb: CallbackQuery, state: FSMContext) -> None:
        await cb.answer()
        if (msg := _cb_message(cb)) is None:
            return
        await state.clear()
        await _ask(msg, state, AccountForm.name,
                   "<b>➕ Новый счёт</b>\nКак его назвать? Например: «БКС», «ИИС в ВТБ», «ЦФА Атомайз».")

    @router.message(AccountForm.name, F.text, NOT_COMMAND)
    async def account_name(message: Message, state: FSMContext) -> None:
        name = clean_text(message.text or "", 64)
        if name is None:
            await _reask(message, "Название — от 1 до 64 символов. Попробуйте ещё раз:")
            return
        await state.update_data(name=name)
        await _ask(message, state, AccountForm.kind, f"Тип счёта «{escape(name)}»:",
                   [[(label, CB_CHOICE + kind.value) for kind, label in NEW_ACCOUNT_KINDS]])

    async def create_account(msg: Message, state: FSMContext, kind: AccountKind, smart_id: str | None) -> None:
        data = await state.get_data()
        await state.clear()
        acc = await guarded(msg, editor.create_account(str(data["name"]), kind, smart_id))
        if isinstance(acc, _Failed):
            return
        await msg.answer(f"✅ Счёт «{escape(acc.name)}» создан.")
        await show_account(msg, acc.id, edit=False)

    @router.callback_query(AccountForm.kind, F.data.startswith(CB_CHOICE))
    async def account_kind(cb: CallbackQuery, state: FSMContext) -> None:
        await cb.answer()
        kind = next((k for k, _ in NEW_ACCOUNT_KINDS if k.value == _choice(cb)), None)
        msg = _cb_message(cb)
        if kind is None or msg is None:
            return
        await _mark(msg, KIND_TITLES[kind])
        if kind is AccountKind.DFA:
            await _ask(msg, state, AccountForm.smart_id,
                       "Это смарт-счёт ЦФА в Т-Банке? Пришлите номер счёта Т-Инвестиций — тогда сумма карточек "
                       "будет сверяться с API. Для ЦФА у других операторов — «Пропустить».", [SKIP_ROW])
            return
        await create_account(msg, state, kind, None)

    @router.message(AccountForm.smart_id, F.text, NOT_COMMAND)
    async def account_smart_id(message: Message, state: FSMContext) -> None:
        raw = (message.text or "").strip()
        if is_skip(raw):
            await create_account(message, state, AccountKind.DFA, None)
            return
        if not _SMART_ID.fullmatch(raw):
            await _reask(message, "Номер счёта — цифры (латиница, «-», «_»), 4–40 символов. Или «Пропустить»:",
                         [SKIP_ROW])
            return
        await create_account(message, state, AccountKind.DFA, raw)

    @router.callback_query(AccountForm.smart_id, F.data == CB_SKIP)
    async def account_smart_skip(cb: CallbackQuery, state: FSMContext) -> None:
        await cb.answer()
        if (msg := _cb_message(cb)) is not None:
            await _mark(msg, "пропущено")
            await create_account(msg, state, AccountKind.DFA, None)

    # ------------------------------------------------ мастер «➕ Бумага»

    @router.callback_query(F.data.startswith(CB_ADD_SECURITY))
    async def add_security(cb: CallbackQuery, state: FSMContext) -> None:
        started = await start_wizard(cb, state, CB_ADD_SECURITY)
        if started is None:
            return
        msg, acc = started
        await _ask(msg, state, SecurityForm.asset_class,
                   f"<b>➕ Бумага</b> на счёт «{escape(acc.name)}»\nЧто добавляем?",
                   [[(label, CB_CHOICE + cls.value) for cls, label in SECURITY_CLASSES]])

    @router.callback_query(SecurityForm.asset_class, F.data.startswith(CB_CHOICE))
    async def security_class(cb: CallbackQuery, state: FSMContext) -> None:
        await cb.answer()
        cls = next((c for c, _ in SECURITY_CLASSES if c.value == _choice(cb)), None)
        msg = _cb_message(cb)
        if cls is None or msg is None:
            return
        await _mark(msg, CLASS_TITLES[cls])
        await state.update_data(cls=cls.value)
        await _ask(msg, state, SecurityForm.ticker,
                   "Тикер или код бумаги (SECID/ISIN), например <code>SBER</code>, <code>SU26238RMFS4</code>, "
                   "<code>TMOS</code>:")

    @router.message(SecurityForm.ticker, F.text, NOT_COMMAND)
    async def security_ticker(message: Message, state: FSMContext) -> None:
        ticker = normalize_ticker(message.text or "")
        if ticker is None:
            await _reask(message, "Тикер — буквы, цифры, «.», «-», «_», без пробелов, до 32 символов. "
                                  "Например: SBER, SU26238RMFS4. Ещё раз:")
            return
        await state.update_data(ticker=ticker)
        await _ask(message, state, SecurityForm.name,
                   f"Название для отчётов (например, «Сбербанк ао») или «Пропустить» — будет <code>{escape(ticker)}"
                   "</code>:", [SKIP_ROW])

    async def security_ask_qty(target: Message, state: FSMContext, name: str | None) -> None:
        await state.update_data(name=name or "")
        await _ask(target, state, SecurityForm.qty, "Количество, шт.:")

    @router.message(SecurityForm.name, F.text, NOT_COMMAND)
    async def security_name(message: Message, state: FSMContext) -> None:
        raw = message.text or ""
        if is_skip(raw):
            await security_ask_qty(message, state, None)
            return
        name = clean_text(raw)
        if name is None:
            await _reask(message, f"Название — до {NAME_MAX} символов. Ещё раз или «Пропустить»:", [SKIP_ROW])
            return
        await security_ask_qty(message, state, name)

    @router.callback_query(SecurityForm.name, F.data == CB_SKIP)
    async def security_name_skip(cb: CallbackQuery, state: FSMContext) -> None:
        await cb.answer()
        if (msg := _cb_message(cb)) is not None:
            await _mark(msg, "пропущено")
            await security_ask_qty(msg, state, None)

    @router.message(SecurityForm.qty, F.text, NOT_COMMAND)
    async def security_qty(message: Message, state: FSMContext) -> None:
        qty = parse_decimal(message.text or "")
        if qty is None or qty <= 0:
            await _reask(message, "Нужно число больше нуля. Примеры: 10, 1 500, 0,5. Ещё раз:")
            return
        data = await state.update_data(qty=str(qty))
        if data.get("cls") == AssetClass.BOND.value:
            prompt = ("Цена: в % номинала («98,5%») или в ₽ за 1 облигацию. "
                      "Без цены позиция не войдёт в оценку — можно «Пропустить».")
        else:
            prompt = "Цена за 1 шт. в ₽ («1 234,5»). Без цены позиция не войдёт в оценку — можно «Пропустить»."
        await _ask(message, state, SecurityForm.price, "✓ Количество записано.\n\n" + prompt, [SKIP_ROW])

    async def security_after_price(target: Message, state: FSMContext) -> None:
        data = await state.get_data()
        if data.get("cls") == AssetClass.BOND.value:
            if data.get("pct"):
                await _ask(target, state, SecurityForm.nominal, "Номинал облигации в ₽:",
                           [[(f"{num(DEFAULT_NOMINAL)} ₽ (обычно)", CB_SKIP)]])
            else:
                await _ask(target, state, SecurityForm.government, "Это ОФЗ?", [YES_NO])
        elif data.get("cls") == AssetClass.FUND.value:
            await _ask(target, state, SecurityForm.focus, "Во что вложен фонд?",
                       [[(t, CB_CHOICE + f.value)] for f, t in FOCUS_TITLES.items()])
        else:
            await security_confirm(target, state)

    @router.message(SecurityForm.price, F.text, NOT_COMMAND)
    async def security_price(message: Message, state: FSMContext) -> None:
        raw = message.text or ""
        if is_skip(raw):
            await state.update_data(price=None, pct=False)
            await security_after_price(message, state)
            return
        parsed = parse_price(raw)
        data = await state.get_data()
        if parsed is None:
            await _reask(message, "Не понял цену. Примеры: 285,4 или 98,5% (для облигаций). Ещё раз:", [SKIP_ROW])
            return
        price, is_pct = parsed
        if is_pct and data.get("cls") != AssetClass.BOND.value:
            await _reask(message, "Процент номинала бывает только у облигаций. Введите цену в ₽:", [SKIP_ROW])
            return
        if is_pct and price > 1000:
            await _reask(message, "Слишком большой процент номинала. Проверьте и введите ещё раз:", [SKIP_ROW])
            return
        await state.update_data(price=str(price), pct=is_pct)
        await message.answer("✓ Цена записана.")
        await security_after_price(message, state)

    @router.callback_query(SecurityForm.price, F.data == CB_SKIP)
    async def security_price_skip(cb: CallbackQuery, state: FSMContext) -> None:
        await cb.answer()
        if (msg := _cb_message(cb)) is not None:
            await _mark(msg, "без цены")
            await state.update_data(price=None, pct=False)
            await security_after_price(msg, state)

    @router.message(SecurityForm.nominal, F.text, NOT_COMMAND)
    async def security_nominal(message: Message, state: FSMContext) -> None:
        raw = message.text or ""
        nominal = DEFAULT_NOMINAL if is_skip(raw) else parse_decimal(raw)
        if nominal is None or nominal <= 0:
            await _reask(message, BAD_NUMBER, [[(f"{num(DEFAULT_NOMINAL)} ₽", CB_SKIP)]])
            return
        await state.update_data(nominal=str(nominal))
        await _ask(message, state, SecurityForm.government, "Это ОФЗ?", [YES_NO])

    @router.callback_query(SecurityForm.nominal, F.data == CB_SKIP)
    async def security_nominal_default(cb: CallbackQuery, state: FSMContext) -> None:
        await cb.answer()
        if (msg := _cb_message(cb)) is not None:
            await _mark(msg, f"{num(DEFAULT_NOMINAL)} ₽")
            await state.update_data(nominal=str(DEFAULT_NOMINAL))
            await _ask(msg, state, SecurityForm.government, "Это ОФЗ?", [YES_NO])

    @router.callback_query(SecurityForm.government, F.data.in_({CB_CHOICE + "y", CB_CHOICE + "n"}))
    async def security_government(cb: CallbackQuery, state: FSMContext) -> None:
        await cb.answer()
        if (msg := _cb_message(cb)) is None:
            return
        government = _choice(cb) == "y"
        await _mark(msg, "да" if government else "нет")
        await state.update_data(gov=government)
        await _ask(msg, state, SecurityForm.coupon, "Тип купона:",
                   [[(t, CB_CHOICE + k.value) for k, t in COUPON_TITLES.items()]])

    @router.callback_query(SecurityForm.coupon, F.data.startswith(CB_CHOICE))
    async def security_coupon(cb: CallbackQuery, state: FSMContext) -> None:
        await cb.answer()
        kind = next((k for k in COUPON_TITLES if k.value == _choice(cb)), None)
        msg = _cb_message(cb)
        if kind is None or msg is None:
            return
        await _mark(msg, COUPON_TITLES[kind])
        data = await state.update_data(coupon=kind.value)
        if data.get("gov"):
            await security_confirm(msg, state)
            return
        await _ask(msg, state, SecurityForm.rating,
                   "Кредитный рейтинг эмитента (например, AA- или ruA+). Если рейтинга нет — «нет»:",
                   [[("Нет рейтинга", CB_SKIP)]])

    async def rating_step(message: Message, state: FSMContext, after: Step) -> None:
        raw = message.text or ""
        rating = None if is_skip(raw) else parse_rating(raw)
        if rating is None and not is_skip(raw):
            await _reask(message, BAD_RATING, [[("Нет рейтинга", CB_SKIP)]])
            return
        await state.update_data(rating=rating)
        await after(message, state)

    @router.message(SecurityForm.rating, F.text, NOT_COMMAND)
    async def security_rating(message: Message, state: FSMContext) -> None:
        await rating_step(message, state, security_confirm)

    @router.callback_query(SecurityForm.rating, F.data == CB_SKIP)
    async def security_rating_none(cb: CallbackQuery, state: FSMContext) -> None:
        await cb.answer()
        if (msg := _cb_message(cb)) is not None:
            await _mark(msg, "нет рейтинга")
            await state.update_data(rating=None)
            await security_confirm(msg, state)

    @router.callback_query(SecurityForm.focus, F.data.startswith(CB_CHOICE))
    async def security_focus(cb: CallbackQuery, state: FSMContext) -> None:
        await cb.answer()
        focus = next((f for f in FOCUS_TITLES if f.value == _choice(cb)), None)
        msg = _cb_message(cb)
        if focus is None or msg is None:
            return
        await _mark(msg, FOCUS_TITLES[focus])
        await state.update_data(focus=focus.value)
        await security_confirm(msg, state)

    async def confirm(target: Message, state: FSMContext, step: State, card: ManualPosition) -> None:
        data = await state.get_data()
        mp = await guarded(target, editor.portfolio())
        if isinstance(mp, _Failed):
            await state.clear()
            return
        acc = next((a for a in mp.accounts if a.id == data.get("acc")), None)
        replaces = acc is not None and any(p.ticker == card.ticker for p in acc.positions)
        await _ask(target, state, step, card_summary(card, str(data.get("acc_name", "")), replaces=replaces),
                   [[("✅ Сохранить", CB_SAVE)]])

    async def security_confirm(target: Message, state: FSMContext) -> None:
        try:
            card = security_card(await state.get_data(), _today())
        except (ValueError, KeyError):
            log.warning("manual_edit.bad_security_card", exc_info=True)
            await state.clear()
            await target.answer("Не получилось собрать карточку — начните заново: /edit")
            return
        await confirm(target, state, SecurityForm.confirm, card)

    # ------------------------------------------------ мастер «➕ ЦФА» (§13.4, п. 6)

    @router.callback_query(F.data.startswith(CB_ADD_DFA))
    async def add_dfa(cb: CallbackQuery, state: FSMContext) -> None:
        started = await start_wizard(cb, state, CB_ADD_DFA)
        if started is None:
            return
        msg, acc = started
        await _ask(msg, state, DfaForm.operator,
                   f"<b>➕ ЦФА</b> на счёт «{escape(acc.name)}»\n"
                   "Оператор информационной системы (например, Атомайз, Мастерчейн, Сбербанк, Альфа-Банк). "
                   "Лимит 600 тыс. ₽ в год для неквала считается по оператору.", [SKIP_ROW])

    async def dfa_ask_issuer(target: Message, state: FSMContext, operator: str | None) -> None:
        await state.update_data(operator=operator)
        await _ask(target, state, DfaForm.issuer, "Эмитент:")

    @router.message(DfaForm.operator, F.text, NOT_COMMAND)
    async def dfa_operator(message: Message, state: FSMContext) -> None:
        raw = message.text or ""
        if is_skip(raw):
            await dfa_ask_issuer(message, state, None)
            return
        operator = clean_text(raw, 64)
        if operator is None:
            await _reask(message, "Название оператора — до 64 символов. Ещё раз или «Пропустить»:", [SKIP_ROW])
            return
        await dfa_ask_issuer(message, state, operator)

    @router.callback_query(DfaForm.operator, F.data == CB_SKIP)
    async def dfa_operator_skip(cb: CallbackQuery, state: FSMContext) -> None:
        await cb.answer()
        if (msg := _cb_message(cb)) is not None:
            await _mark(msg, "пропущено")
            await dfa_ask_issuer(msg, state, None)

    @router.message(DfaForm.issuer, F.text, NOT_COMMAND)
    async def dfa_issuer(message: Message, state: FSMContext) -> None:
        issuer = clean_text(message.text or "", 64)
        if issuer is None:
            await _reask(message, "Название эмитента — от 1 до 64 символов. Ещё раз:")
            return
        await state.update_data(issuer=issuer)
        await _ask(message, state, DfaForm.rating, "Рейтинг эмитента (например, A- или ruBBB+) или «нет»:",
                   [[("Нет рейтинга", CB_SKIP)]])

    async def dfa_ask_kind(target: Message, state: FSMContext) -> None:
        await _ask(target, state, DfaForm.kind,
                   "Тип по Указанию 7176-У: «фиксированный доход» (выплаты известны заранее) "
                   "или «индикаторный» (выплата зависит от базового актива)?",
                   [[(t.capitalize(), CB_CHOICE + code)] for code, t in DFA_KINDS.items()])

    @router.message(DfaForm.rating, F.text, NOT_COMMAND)
    async def dfa_rating(message: Message, state: FSMContext) -> None:
        await rating_step(message, state, dfa_ask_kind)

    @router.callback_query(DfaForm.rating, F.data == CB_SKIP)
    async def dfa_rating_none(cb: CallbackQuery, state: FSMContext) -> None:
        await cb.answer()
        if (msg := _cb_message(cb)) is not None:
            await _mark(msg, "нет рейтинга")
            await state.update_data(rating=None)
            await dfa_ask_kind(msg, state)

    @router.callback_query(DfaForm.kind, F.data.startswith(CB_CHOICE))
    async def dfa_kind(cb: CallbackQuery, state: FSMContext) -> None:
        await cb.answer()
        code, msg = _choice(cb), _cb_message(cb)
        if code not in DFA_KINDS or msg is None:
            return
        await _mark(msg, DFA_KINDS[code])
        await state.update_data(dkind=code)
        await _ask(msg, state, DfaForm.nominal, "Номинал 1 ЦФА в ₽:")

    @router.message(DfaForm.nominal, F.text, NOT_COMMAND)
    async def dfa_nominal(message: Message, state: FSMContext) -> None:
        nominal = parse_decimal(message.text or "")
        if nominal is None or nominal <= 0:
            await _reask(message, BAD_NUMBER)
            return
        await state.update_data(nominal=str(nominal))
        await _ask(message, state, DfaForm.qty, "Количество ЦФА, шт.:")

    @router.message(DfaForm.qty, F.text, NOT_COMMAND)
    async def dfa_qty(message: Message, state: FSMContext) -> None:
        qty = parse_decimal(message.text or "")
        if qty is None or qty <= 0:
            await _reask(message, "Нужно число больше нуля. Ещё раз:")
            return
        await state.update_data(qty=str(qty))
        await _ask(message, state, DfaForm.rate,
                   "✓ Количество записано.\n\nСтавка, % годовых (например, 18,5%). "
                   "Если ставки нет (индикаторный выпуск) — «Пропустить».", [SKIP_ROW])

    async def dfa_ask_maturity(target: Message, state: FSMContext) -> None:
        await _ask(target, state, DfaForm.maturity, "Дата погашения (30.06.2027 или 2027-06-30):", [SKIP_ROW])

    @router.message(DfaForm.rate, F.text, NOT_COMMAND)
    async def dfa_rate(message: Message, state: FSMContext) -> None:
        raw = message.text or ""
        if is_skip(raw):
            await state.update_data(rate=None, freq=None)
            await dfa_ask_maturity(message, state)
            return
        rate = parse_rate(raw)
        if rate is None:
            await _reask(message, "Ставка — число от 0 до 100, например 18,5%. Ещё раз или «Пропустить»:",
                         [SKIP_ROW])
            return
        await state.update_data(rate=str(rate))
        await _ask(message, state, DfaForm.frequency, "Периодичность выплат:",
                   [[(t, CB_CHOICE + code)] for code, t in FREQUENCIES.items()])

    @router.callback_query(DfaForm.rate, F.data == CB_SKIP)
    async def dfa_rate_skip(cb: CallbackQuery, state: FSMContext) -> None:
        await cb.answer()
        if (msg := _cb_message(cb)) is not None:
            await _mark(msg, "без ставки")
            await state.update_data(rate=None, freq=None)
            await dfa_ask_maturity(msg, state)

    @router.callback_query(DfaForm.frequency, F.data.startswith(CB_CHOICE))
    async def dfa_frequency(cb: CallbackQuery, state: FSMContext) -> None:
        await cb.answer()
        code, msg = _choice(cb), _cb_message(cb)
        if code not in FREQUENCIES or msg is None:
            return
        await _mark(msg, FREQUENCIES[code])
        await state.update_data(freq=code)
        await dfa_ask_maturity(msg, state)

    async def dfa_ask_market_maker(target: Message, state: FSMContext, maturity: date | None) -> None:
        await state.update_data(maturity=maturity.isoformat() if maturity else None)
        await _ask(target, state, DfaForm.market_maker,
                   "Есть маркетмейкер (можно продать до погашения)?", [YES_NO])

    @router.message(DfaForm.maturity, F.text, NOT_COMMAND)
    async def dfa_maturity(message: Message, state: FSMContext) -> None:
        raw = message.text or ""
        if is_skip(raw):
            await dfa_ask_market_maker(message, state, None)
            return
        maturity = parse_date(raw)
        if maturity is None:
            await _reask(message, "Не понял дату. Формат: 30.06.2027 или 2027-06-30. Ещё раз или «Пропустить»:",
                         [SKIP_ROW])
            return
        if maturity < _today():
            await _reask(message, "Эта дата уже прошла — проверьте год. Ещё раз:", [SKIP_ROW])
            return
        await dfa_ask_market_maker(message, state, maturity)

    @router.callback_query(DfaForm.maturity, F.data == CB_SKIP)
    async def dfa_maturity_skip(cb: CallbackQuery, state: FSMContext) -> None:
        await cb.answer()
        if (msg := _cb_message(cb)) is not None:
            await _mark(msg, "без даты")
            await dfa_ask_market_maker(msg, state, None)

    @router.callback_query(DfaForm.market_maker, F.data.in_({CB_CHOICE + "y", CB_CHOICE + "n"}))
    async def dfa_market_maker(cb: CallbackQuery, state: FSMContext) -> None:
        await cb.answer()
        if (msg := _cb_message(cb)) is None:
            return
        market_maker = _choice(cb) == "y"
        await _mark(msg, "да" if market_maker else "нет")
        data = await state.update_data(mm=market_maker)
        if data.get("dkind") == "ind":
            await _ask(msg, state, DfaForm.underlying,
                       "Базовый актив и формула выплаты — коротко (например, «золото, 100% роста»), "
                       "или «Пропустить»:", [SKIP_ROW])
            return
        await dfa_confirm(msg, state)

    async def dfa_ask_price(target: Message, state: FSMContext, underlying: str | None) -> None:
        await state.update_data(underlying=underlying)
        await _ask(target, state, DfaForm.price,
                   "Текущая цена 1 ЦФА в ₽ (или в % номинала), если знаете. «Пропустить» — оценка по номиналу.",
                   [SKIP_ROW])

    @router.message(DfaForm.underlying, F.text, NOT_COMMAND)
    async def dfa_underlying(message: Message, state: FSMContext) -> None:
        raw = message.text or ""
        if is_skip(raw):
            await dfa_ask_price(message, state, None)
            return
        underlying = clean_text(raw, 64)
        if underlying is None:
            await _reask(message, "Коротко, до 64 символов. Ещё раз или «Пропустить»:", [SKIP_ROW])
            return
        await dfa_ask_price(message, state, underlying)

    @router.callback_query(DfaForm.underlying, F.data == CB_SKIP)
    async def dfa_underlying_skip(cb: CallbackQuery, state: FSMContext) -> None:
        await cb.answer()
        if (msg := _cb_message(cb)) is not None:
            await _mark(msg, "пропущено")
            await dfa_ask_price(msg, state, None)

    @router.message(DfaForm.price, F.text, NOT_COMMAND)
    async def dfa_price(message: Message, state: FSMContext) -> None:
        raw = message.text or ""
        if is_skip(raw):
            await state.update_data(price=None)
            await dfa_confirm(message, state)
            return
        parsed = parse_price(raw)
        if parsed is None:
            await _reask(message, "Не понял цену. Примеры: 1 050 или 105%. Ещё раз или «Пропустить»:", [SKIP_ROW])
            return
        price, is_pct = parsed
        if is_pct:
            nominal = _dec((await state.get_data()).get("nominal")) or DEFAULT_NOMINAL
            price = tidy(nominal * price / 100)
        await state.update_data(price=str(price))
        await message.answer("✓ Цена записана.")
        await dfa_confirm(message, state)

    @router.callback_query(DfaForm.price, F.data == CB_SKIP)
    async def dfa_price_skip(cb: CallbackQuery, state: FSMContext) -> None:
        await cb.answer()
        if (msg := _cb_message(cb)) is not None:
            await _mark(msg, "по номиналу")
            await state.update_data(price=None)
            await dfa_confirm(msg, state)

    async def dfa_confirm(target: Message, state: FSMContext) -> None:
        try:
            card = dfa_card(await state.get_data(), _today())
        except (ValueError, KeyError):
            log.warning("manual_edit.bad_dfa_card", exc_info=True)
            await state.clear()
            await target.answer("Не получилось собрать карточку ЦФА — начните заново: /edit")
            return
        await confirm(target, state, DfaForm.confirm, card)

    # ------------------------------------------------ сохранение карточки

    @router.callback_query(StateFilter(SecurityForm.confirm, DfaForm.confirm), F.data == CB_SAVE)
    async def save(cb: CallbackQuery, state: FSMContext) -> None:
        await cb.answer()
        if (msg := _cb_message(cb)) is None:
            return
        data, current = await state.get_data(), await state.get_state()
        await state.clear()
        try:
            card = (dfa_card if current == DfaForm.confirm.state else security_card)(data, _today())
        except (ValueError, KeyError):
            log.warning("manual_edit.bad_card", exc_info=True)
            await msg.answer("Не получилось собрать карточку — начните заново: /edit")
            return
        account_id = str(data["acc"])
        warnings = await guarded(msg, editor.save_position(account_id, card))
        if isinstance(warnings, _Failed):
            await show_list(msg, edit=False)
            return
        await _mark(msg, "сохранено")
        lines = [f"✅ Сохранено: <code>{escape(card.ticker)}</code>"]
        lines.extend(f"⚠️ {escape(w)}" for w in warnings)
        await msg.answer("\n".join(lines))
        await show_account(msg, account_id, edit=False)

    # ------------------------------------------------ «💰 Деньги»

    @router.callback_query(F.data.startswith(CB_CASH))
    async def add_cash(cb: CallbackQuery, state: FSMContext) -> None:
        started = await start_wizard(cb, state, CB_CASH)
        if started is None:
            return
        msg, acc = started
        await _ask(msg, state, CashForm.currency, f"<b>💰 Деньги</b> на счёте «{escape(acc.name)}»\nВалюта:",
                   [[(sign, CB_CHOICE + code) for code, sign in CASH_CHOICES] + [("Другая", CB_CHOICE + "other")]])

    async def cash_ask_amount(target: Message, state: FSMContext, currency: str) -> None:
        await state.update_data(cur=currency)
        sign = CURRENCY_SIGNS.get(currency, currency.upper())
        await _ask(target, state, CashForm.amount, f"Остаток в {escape(sign)}. «0» — убрать валюту со счёта:")

    @router.callback_query(CashForm.currency, F.data.startswith(CB_CHOICE))
    async def cash_currency(cb: CallbackQuery, state: FSMContext) -> None:
        await cb.answer()
        code, msg = _choice(cb), _cb_message(cb)
        if msg is None:
            return
        if code == "other":
            await _mark(msg, "другая")
            await _ask(msg, state, CashForm.code, "Код валюты — 3 латинские буквы (например, EUR, HKD):")
            return
        if code not in dict(CASH_CHOICES):
            return
        await _mark(msg, CURRENCY_SIGNS[code])
        await cash_ask_amount(msg, state, code)

    @router.message(CashForm.code, F.text, NOT_COMMAND)
    async def cash_code(message: Message, state: FSMContext) -> None:
        code = (message.text or "").strip().lower()
        if not _CURRENCY_CODE.fullmatch(code):
            await _reask(message, "Нужен код из 3 латинских букв, например EUR. Ещё раз:")
            return
        await cash_ask_amount(message, state, code)

    @router.message(CashForm.amount, F.text, NOT_COMMAND)
    async def cash_amount(message: Message, state: FSMContext) -> None:
        amount = parse_decimal(message.text or "")
        if amount is None or amount < 0:
            await _reask(message, "Нужно число не меньше нуля. Примеры: 25 000, 1,5к, 0. Ещё раз:")
            return
        data = await state.get_data()
        await state.clear()
        currency = str(data["cur"])
        if isinstance(await guarded(message, editor.set_cash(str(data["acc"]), currency, amount)), _Failed):
            return
        sign = escape(CURRENCY_SIGNS.get(currency, currency.upper()))
        await message.answer(f"✓ Остаток в {sign} записан." if amount > 0 else f"✓ {sign} убраны со счёта.")
        await show_account(message, str(data["acc"]), edit=False)

    # ------------------------------------------------ запасные обработчики

    @router.message(StateFilter(*WIZARDS), NOT_COMMAND)
    async def unexpected_input(message: Message) -> None:
        await message.answer("Жду ответа на вопрос выше — текстом или кнопкой. Выйти — «✖ Отмена» или /edit.")

    @router.callback_query(F.data.startswith("me:"))
    async def stale_button(cb: CallbackQuery) -> None:
        await cb.answer("Эта кнопка устарела — откройте /edit заново.")

    return router
