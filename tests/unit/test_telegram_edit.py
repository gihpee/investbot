from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from aiogram import Bot, Dispatcher, Router
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from aiogram.types import CallbackQuery, Chat, InlineKeyboardMarkup, Message, Update, User

from investbot.channels.telegram.edit import (
    CB_CANCEL,
    CB_MAX,
    CB_NEW,
    CB_SAVE,
    CB_SKIP,
    PRIVACY_LINE,
    account_view,
    accounts_view,
    build_edit_router,
    card_summary,
    delete_confirm_view,
    delete_pick_view,
    dfa_card,
    dfa_ticker,
    drop_confirm_view,
    item_ref,
    normalize_ticker,
    num,
    parse_date,
    parse_decimal,
    parse_price,
    parse_rate,
    parse_rating,
    resolve_ref,
    security_card,
)
from investbot.core.models import AccountKind, AssetClass, CouponKind, FundFocus
from investbot.journal.store import Journal
from investbot.portfolio.manual import ManualAccount, ManualPortfolio, ManualPosition
from investbot.portfolio.manual_store import YamlManualStore
from investbot.services.manual import ManualEditor, slugify

D = Decimal
TODAY = date(2026, 10, 9)


# ---------------------------------------------------------------- разбор ввода

@pytest.mark.parametrize(("raw", "expected"), [
    ("1 500,5", D("1500.5")),
    ("1500.5", D("1500.5")),
    ("1,5к", D(1500)),
    ("1,5k", D(1500)),
    ("2 млн", D(2_000_000)),
    ("1,5 тыс.", D(1500)),
    ("150 ₽", D(150)),
    ("12 шт", D(12)),
    ("1 000", D(1000)),
    ("1.500,5", D("1500.5")),
    ("1,500.5", D("1500.5")),
    ("1,500,000", D(1_500_000)),
    ("1,500", D("1.5")),  # одна запятая — дробная часть (русская запись)
    ("−3", D(-3)),
    ("0", D(0)),
    (".5", D("0.5")),
])
def test_parse_decimal(raw: str, expected: Decimal) -> None:
    assert parse_decimal(raw) == expected


@pytest.mark.parametrize("raw", ["", "abc", "1e5", "nan", "Infinity", "1..5", "1.5.5", "1,5,5", "²",
                                 "1 000 000 000 000 000", "12 рублей", "--1"])
def test_parse_decimal_rejects_garbage(raw: str) -> None:
    assert parse_decimal(raw) is None


def test_parse_decimal_has_no_exponent_noise() -> None:
    assert str(parse_decimal("1,5к")) == "1500" and str(parse_decimal("98,50")) == "98.5"


def test_parse_price_and_rate() -> None:
    assert parse_price("98,5%") == (D("98.5"), True)
    assert parse_price("98.5 %") == (D("98.5"), True)
    assert parse_price("1 234,5") == (D("1234.5"), False)
    assert parse_price("1,5к ₽") == (D(1500), False)
    for bad in ("0", "-5", "0%", "x%", "", "цена"):
        assert parse_price(bad) is None
    assert parse_rate("18,5%") == D("18.5") and parse_rate("21") == D(21)
    assert parse_rate("0") is None and parse_rate("150%") is None and parse_rate("много") is None


def test_parse_date() -> None:
    assert parse_date("30.06.2027") == date(2027, 6, 30)
    assert parse_date(" 2027-06-30 ") == date(2027, 6, 30)
    assert parse_date("30.06.27") == date(2027, 6, 30)
    assert parse_date("30/06/2027") == date(2027, 6, 30)
    for bad in ("31.02.2027", "1.1.1800", "завтра", "", "2027-13-01"):
        assert parse_date(bad) is None


def test_parse_rating_and_ticker() -> None:
    assert parse_rating("AA-") == "AA-" and parse_rating("ruA+") == "A+" and parse_rating("A+(RU)") == "A+"
    assert parse_rating("АА-") == "AA-"  # кириллица вместо латиницы
    assert parse_rating("bbb") == "BBB"
    assert parse_rating("нет") is None and parse_rating("XYZ") is None
    assert normalize_ticker(" su26238rmfs4 ") == "SU26238RMFS4" and normalize_ticker("цфа-1") == "ЦФА-1"
    assert normalize_ticker("a b") is None and normalize_ticker("/edit") is None and normalize_ticker("") is None
    assert num(D(1000)) == "1 000" and num(D("98.50")) == "98,5" and num(D(-5)) == "−5"


# ---------------------------------------------------------------- карточки

def test_bond_card_with_percent_price_defaults_nominal() -> None:
    data = {"acc": "x", "cls": "bond", "ticker": "SU26238RMFS4", "name": "", "qty": "15", "price": "61.5",
            "pct": True, "gov": True, "coupon": "fixed", "rating": "AA"}
    card = security_card(data, TODAY)
    assert card.asset_class is AssetClass.BOND and card.price is None and card.price_pct == D("61.5")
    assert card.nominal == D(1000) and card.price_date == TODAY and card.unit_price() == D(615)
    assert card.is_government and card.coupon_kind is CouponKind.FIXED
    assert card.rating is None  # у ОФЗ рейтинг не нужен для бакета
    corp = security_card({**data, "gov": False, "coupon": "floating", "nominal": "500"}, TODAY)
    assert corp.rating == "AA" and corp.nominal == D(500) and corp.coupon_kind is CouponKind.FLOATING


def test_equity_and_fund_cards() -> None:
    eq = security_card({"cls": "equity", "ticker": "SBER", "name": "Сбербанк", "qty": "10", "price": "291.5"}, TODAY)
    assert eq.price == D("291.5") and eq.nominal is None and eq.coupon_kind is CouponKind.UNKNOWN
    no_price = security_card({"cls": "equity", "ticker": "SBER", "qty": "10", "price": None}, TODAY)
    assert no_price.unit_price() is None and no_price.price_date is None
    fund = security_card({"cls": "fund", "ticker": "TMOS", "qty": "3", "price": "7", "focus": "equity"}, TODAY)
    assert fund.fund_focus is FundFocus.EQUITY
    with pytest.raises(ValueError, match="облигаций"):
        security_card({"cls": "equity", "ticker": "SBER", "qty": "1", "price": "98", "pct": True}, TODAY)
    with pytest.raises(ValueError):
        security_card({"cls": "dfa", "ticker": "X", "qty": "1"}, TODAY)
    with pytest.raises(ValueError):  # ValidationError: количество должно быть > 0
        security_card({"cls": "equity", "ticker": "SBER", "qty": "0"}, TODAY)


def test_dfa_card_keeps_7176_type_rate_and_market_maker_in_name() -> None:
    data = {"acc": "cfa", "operator": "Атомайз", "issuer": "ООО Ромашка", "rating": "BBB+", "dkind": "fix",
            "nominal": "1000", "qty": "50", "rate": "18.5", "freq": "m", "maturity": "2027-06-30", "mm": True}
    card = dfa_card(data, TODAY)
    assert card.asset_class is AssetClass.DFA and card.ticker == "DFA-OOO_ROMASHKA-20270630"
    assert card.name == "ООО Ромашка, 18,5% ежемесячно · 7176-У: фикс. доход · ММ: есть"
    assert card.operator == "Атомайз" and card.rating == "BBB+" and card.maturity == date(2027, 6, 30)
    assert card.nominal == D(1000) and card.qty == D(50) and card.price is None and card.unit_price() == D(1000)
    ind = dfa_card({**data, "dkind": "ind", "rate": None, "underlying": "золото", "price": "1050", "mm": False,
                    "operator": None, "maturity": None}, TODAY)
    assert ind.name == "ООО Ромашка · 7176-У: индикаторный (золото) · ММ: нет"
    assert ind.price == D(1050) and ind.price_date == TODAY and ind.operator is None
    assert ind.ticker == "DFA-OOO_ROMASHKA"


def test_card_summary_never_repeats_quantity() -> None:
    card = security_card({"cls": "bond", "ticker": "RU000A105TJ2", "qty": "4321", "price": "98.5", "pct": True,
                          "gov": False, "coupon": "fixed", "rating": "A-"}, TODAY)
    text = card_summary(card, "Брокер <X>", replaces=True)
    assert "4321" not in text and "4 321" not in text
    assert "98,5% номинала" in text and "Номинал: 1 000 ₽" in text and "Рейтинг: A-" in text
    assert "Брокер &lt;X&gt;" in text and "будет заменена" in text
    rub_bond = security_card({"cls": "bond", "ticker": "X", "qty": "1", "price": "98"}, TODAY)
    assert "со знаком «%»" in card_summary(rub_bond, "a")


# ---------------------------------------------------------------- экраны

def portfolio() -> ManualPortfolio:
    return ManualPortfolio(accounts=(
        ManualAccount(id="bks", name="БКС <тест> & co", kind=AccountKind.BROKER, positions=(
            ManualPosition(ticker="SBER", name="Сбер <ао>", asset_class=AssetClass.EQUITY, qty=D(777),
                           price=D("291.37")),
            ManualPosition(ticker="SU26238RMFS4", asset_class=AssetClass.BOND, qty=D(4321), price_pct=D("61.5"),
                           nominal=D(1000), is_government=True),
        ), cash={"rub": D("1234567.89"), "usd": D(55)}),
        ManualAccount(id="cfa", name="ЦФА Т-Банк", kind=AccountKind.DFA, tinvest_account_id="2000123456", positions=(
            ManualPosition(ticker="DFA-X", name="Эмитент, 20%", asset_class=AssetClass.DFA, qty=D(9),
                           nominal=D(10000), operator="Т-Банк"),
        )),
    ))


SECRETS = ("777", "291", "4321", "4 321", "1234567", "1 234 567", "55", "2000123456", "10000", "10 000")


def test_accounts_list_shows_no_amounts_and_escapes() -> None:
    text, markup = accounts_view(portfolio())
    assert "БКС &lt;тест&gt; &amp; co" in text and "<тест>" not in text
    assert "брокерский · позиций: 2 · деньги: ₽, $" in text and "ЦФА · позиций: 1" in text
    assert "синхронизируются автоматически" in text
    assert not [s for s in SECRETS if s in text]
    buttons = [b.text for row in markup.inline_keyboard for b in row]
    assert "➕ Новый счёт" in buttons and any(b.startswith("📁 БКС") for b in buttons)


def test_empty_list_explains_purpose_and_privacy() -> None:
    text, _ = accounts_view(ManualPortfolio())
    assert "Ручных счетов пока нет" in text and "других брокеров" in text and "без токена" in text
    assert PRIVACY_LINE in text
    assert "Введённые данные хранятся у Telegram — при желании вместо чата можно править" in text


def test_account_screen_lists_positions_without_amounts() -> None:
    mp = portfolio()
    text, markup = account_view(mp.accounts[0], 0)
    assert "<code>SBER</code> — акция · Сбер &lt;ао&gt;" in text
    assert "<code>SU26238RMFS4</code> — облигация (ОФЗ)" in text and "Деньги: ₽, $" in text
    assert not [s for s in SECRETS if s in text]
    buttons = [b.text for row in markup.inline_keyboard for b in row]
    for b in ("➕ Бумага", "➕ ЦФА", "💰 Деньги", "🗑 Удалить позицию", "🗑 Удалить счёт", "↩️ Назад"):
        assert b in buttons
    dfa_text, _ = account_view(mp.accounts[1], 1)
    assert "смарт-счёту ЦФА Т-Банка" in dfa_text and "2000123456" not in dfa_text and "10000" not in dfa_text


def _callbacks(markup: InlineKeyboardMarkup) -> list[str]:
    return [b.callback_data or "" for row in markup.inline_keyboard for b in row]


def test_callback_data_fits_64_bytes_and_resolves() -> None:
    long_id = slugify("Очень длинное название счёта у другого брокера номер один") + "_12"
    weird_id = "счёт:с:двоеточиями-" + "я" * 60  # руками в YAML
    tickers = ["SU26238RMFS4", "RU000A105TJ2", dfa_ticker("ООО «Очень длинный эмитент финанс»", date(2027, 6, 30)),
               "ЦФА-ОЧЕНЬ-ДЛИННЫЙ-КОД-ВЫПУСКА-1", "A:B"]
    positions = tuple(ManualPosition(ticker=t, asset_class=AssetClass.DFA, qty=D(1)) for t in tickers)
    accounts = tuple(ManualAccount(id=i, name=i, positions=positions) for i in (long_id, weird_id, "bks"))
    mp = ManualPortfolio(accounts=accounts)
    views = [accounts_view(mp)]
    for i, acc in enumerate(accounts):
        views += [account_view(acc, i), delete_pick_view(acc, i), drop_confirm_view(acc, i)]
        views += [delete_confirm_view(acc, i, j) for j in range(len(positions))]
    data = [d for _, markup in views for d in _callbacks(markup)]
    assert data and all(len(d.encode()) <= CB_MAX for d in data)
    # короткие id идут как есть, длинные и с «:» — по индексу с контрольной суммой
    assert "me:o:bks" in data and f"me:o:{long_id}" in data
    for i, acc in enumerate(accounts):
        for j, cb in enumerate(_callbacks(delete_pick_view(acc, i)[1])[:-1]):
            account, _, position = cb.removeprefix("me:dq:").partition(":")
            assert resolve_ref([a.id for a in accounts], account) == i
            assert resolve_ref(tickers, position) == j


def test_item_ref_checks_crc() -> None:
    ref = item_ref("A:B", 4, 30)
    assert ref.startswith("#4~") and resolve_ref(["a", "b", "c", "d", "A:B"], ref) == 4
    assert resolve_ref(["a", "b", "c", "d", "other"], ref) is None  # список изменился — не та позиция
    assert resolve_ref(["a"], "#9~0000") is None and resolve_ref(["a"], "#x~1") is None
    assert resolve_ref(["a", "b"], "b") == 1 and resolve_ref(["a"], "zzz") is None


# ---------------------------------------------------------------- роутер

class FakeEditor:
    async def portfolio(self) -> ManualPortfolio:
        return ManualPortfolio()

    async def create_account(self, name: str, kind: AccountKind, tinvest_account_id: str | None = None) -> Any:
        raise NotImplementedError

    async def delete_account(self, account_id: str) -> None: ...

    async def save_position(self, account_id: str, position: ManualPosition) -> list[str]:
        return []

    async def delete_position(self, account_id: str, ticker: str) -> None: ...

    async def set_cash(self, account_id: str, currency: str, amount: Decimal) -> None: ...


def test_router_builds_and_registers_handlers() -> None:
    router = build_edit_router(FakeEditor())
    assert isinstance(router, Router)
    assert len(router.message.handlers) >= 20 and len(router.callback_query.handlers) >= 30
    Dispatcher().include_router(router)


class FakeBot(Bot):
    """Записывает вызовы Bot API вместо отправки в Telegram."""

    def __init__(self) -> None:
        super().__init__("42:TEST")
        self.calls: list[Any] = []

    async def __call__(self, method: Any, request_timeout: int | None = None) -> Any:
        self.calls.append(method)
        return True


USER = User(id=7, is_bot=False, first_name="Owner")
BOT_USER = User(id=42, is_bot=True, first_name="Bot")
CHAT = Chat(id=7, type="private")


class Conversation:
    def __init__(self, editor: ManualEditor) -> None:
        self.bot = FakeBot()
        self.dp = Dispatcher()
        self.dp.include_router(build_edit_router(editor, today=TODAY))
        self.n = 0
        self.transcript: list[str] = []

    async def _feed(self, update: Update) -> list[Any]:
        start = len(self.bot.calls)
        await self.dp.feed_update(self.bot, update)
        calls = self.bot.calls[start:]
        self.transcript += [c.text for c in calls if isinstance(c, (SendMessage, EditMessageText))]
        return calls

    async def say(self, text: str) -> str:
        self.n += 1
        msg = Message(message_id=self.n, date=datetime.now(UTC), chat=CHAT, from_user=USER, text=text)
        calls = await self._feed(Update(update_id=self.n, message=msg))
        return "\n".join(c.text for c in calls if isinstance(c, SendMessage))

    async def press(self, data: str) -> str:
        self.n += 1
        msg = Message(message_id=self.n, date=datetime.now(UTC), chat=CHAT, from_user=BOT_USER, text="вопрос")
        cb = CallbackQuery(id=str(self.n), from_user=USER, chat_instance="ci", data=data, message=msg)
        calls = await self._feed(Update(update_id=self.n, callback_query=cb))
        toasts = [c.text or "" for c in calls if isinstance(c, AnswerCallbackQuery)]
        return "\n".join([c.text for c in calls if isinstance(c, (SendMessage, EditMessageText))] + toasts)

    def last_markup(self) -> list[str]:
        sent = [c for c in self.bot.calls if isinstance(c, (SendMessage, EditMessageText)) and c.reply_markup]
        return _callbacks(sent[-1].reply_markup)

    async def state(self) -> str | None:
        return await self.dp.fsm.get_context(self.bot, chat_id=CHAT.id, user_id=USER.id).get_state()


def make_editor(tmp_path: Path) -> tuple[ManualEditor, YamlManualStore]:
    store = YamlManualStore(tmp_path / "manual.yaml")
    return ManualEditor(store, Journal(tmp_path / "j.jsonl")), store


async def test_wizard_new_account_and_ofz_with_percent_price(tmp_path: Path) -> None:
    editor, store = make_editor(tmp_path)
    c = Conversation(editor)
    assert "Ручных счетов пока нет" in await c.say("/edit")
    assert "Как его назвать" in await c.press(CB_NEW)
    assert "Тип счёта" in await c.say("Другой брокер")
    assert "создан" in await c.press("me:c:broker")
    assert await c.state() is None
    [acc] = (await store.load()).accounts
    assert acc.id == "drugoi_broker" and acc.kind is AccountKind.BROKER

    assert "Что добавляем" in await c.press(f"me:as:{acc.id}")
    assert "Тикер" in await c.press("me:c:bond")
    assert "Название" in await c.say("su26238rmfs4")
    assert "Количество" in await c.say("пропустить")
    assert "больше нуля" in await c.say("много")  # переспрашивает, без трассировки
    reply = await c.say("1 250")
    assert "Количество записано" in reply and "1 250" not in reply and "1250" not in reply
    assert "Это ОФЗ" not in await c.say("98,5%")  # сначала номинал
    assert "Это ОФЗ" in await c.press(CB_SKIP)  # номинал по умолчанию 1000
    assert "Жду ответа" in await c.say("да")  # на шаге с кнопками текст не принимается
    assert "Тип купона" in await c.press("me:c:y")
    summary = await c.press("me:c:fixed")
    assert "Проверьте карточку" in summary and "98,5% номинала" in summary and "Номинал: 1 000 ₽" in summary
    saved = await c.press(CB_SAVE)
    assert "Сохранено" in saved and "SU26238RMFS4" in saved and await c.state() is None

    [pos] = (await store.load()).accounts[0].positions
    assert pos.ticker == "SU26238RMFS4" and pos.asset_class is AssetClass.BOND and pos.qty == D(1250)
    assert pos.price_pct == D("98.5") and pos.nominal == D(1000) and pos.price_date == TODAY
    assert pos.is_government and pos.coupon_kind is CouponKind.FIXED
    # приватность: введённое количество бот не повторил ни разу
    assert not [t for t in c.transcript if "1250" in t or "1 250" in t]


async def test_wizard_dfa_card_cash_and_delete(tmp_path: Path) -> None:
    editor, store = make_editor(tmp_path)
    acc = await editor.create_account("ЦФА Атомайз", AccountKind.DFA)
    c = Conversation(editor)
    assert "Оператор" in await c.press(f"me:ad:{acc.id}")
    assert "Эмитент" in await c.say("Атомайз")
    assert "Рейтинг" in await c.say("ООО Ромашка")
    assert "Не распознал" in await c.say("хороший")
    assert "7176-У" in await c.say("BBB+")
    assert "Номинал" in await c.press("me:c:fix")
    assert "Количество" in await c.say("1 000")
    assert "Ставка" in await c.say("50")
    assert "Периодичность" in await c.say("18,5%")
    assert "погашения" in await c.press("me:c:m")
    assert "уже прошла" in await c.say("30.06.2020")
    assert "маркетмейкер" in await c.say("30.06.2027")
    summary = await c.press("me:c:y")
    assert "Проверьте карточку" in summary and "Оператор: Атомайз" in summary and "Погашение: 30.06.2027" in summary
    saved = await c.press(CB_SAVE)
    assert "Сохранено" in saved and "⚠️" not in saved  # оператор и погашение указаны, счёт типа ЦФА
    [card] = (await store.load()).accounts[0].positions
    assert card.ticker == "DFA-OOO_ROMASHKA-20270630" and card.qty == D(50) and card.nominal == D(1000)
    assert card.name == "ООО Ромашка, 18,5% ежемесячно · 7176-У: фикс. доход · ММ: есть"
    assert card.operator == "Атомайз" and card.rating == "BBB+" and card.maturity == date(2027, 6, 30)

    assert "Валюта" in await c.press(f"me:ac:{acc.id}")
    assert "Остаток в ₽" in await c.press("me:c:rub")
    reply = await c.say("25 000")
    assert "Остаток в ₽ записан" in reply and "25 000" not in reply and "25000" not in reply
    assert (await store.load()).accounts[0].cash == {"rub": D(25000)}

    assert "Какую позицию удалить" in await c.press(f"me:dp:{acc.id}")
    ask = next(d for d in c.last_markup() if d.startswith("me:dq:"))
    assert "Удалить <code>DFA-OOO_ROMASHKA-20270630</code>" in await c.press(ask)
    yes = next(d for d in c.last_markup() if d.startswith("me:dy:"))
    await c.press(yes)
    assert (await store.load()).accounts[0].positions == ()

    assert "Что добавляем" in await c.press(f"me:as:{acc.id}")
    await c.press(CB_CANCEL)
    assert await c.state() is None
    assert "устарела" in await c.press("me:c:bond")

    assert "Удалить счёт" in await c.press(f"me:da:{acc.id}")
    await c.press(f"me:dz:{acc.id}")
    assert (await store.load()).accounts == ()


async def test_wizard_shows_adr06_rejection_without_traceback(tmp_path: Path) -> None:
    editor, store = make_editor(tmp_path)
    acc = await editor.create_account("Счёт", AccountKind.BROKER)
    editor.set_broker_accounts({acc.id})  # счёт стал читаться из API Т-Инвестиций
    c = Conversation(editor)
    await c.press(f"me:as:{acc.id}")
    await c.press("me:c:equity")
    await c.say("SBER")
    await c.press(CB_SKIP)
    await c.say("10")
    assert "Проверьте карточку" in await c.press(CB_SKIP)  # без цены → сразу подтверждение
    reply = await c.press(CB_SAVE)
    assert "Не получилось" in reply and "запрещены" in reply and "Traceback" not in reply
    assert (await store.load()).accounts[0].positions == () and await c.state() is None


async def test_wizard_indicative_dfa_with_skips_shows_service_warnings(tmp_path: Path) -> None:
    editor, store = make_editor(tmp_path)
    acc = await editor.create_account("Другой брокер", AccountKind.BROKER)
    c = Conversation(editor)
    await c.press(f"me:ad:{acc.id}")
    assert "Эмитент" in await c.press(CB_SKIP)  # оператор не указан
    await c.say("АО Золото")
    await c.press(CB_SKIP)  # без рейтинга
    await c.press("me:c:ind")
    await c.say("1000")
    await c.say("3")
    assert "погашения" in await c.press(CB_SKIP)  # без ставки — сразу дата
    assert "маркетмейкер" in await c.say("пропустить")
    assert "Базовый актив" in await c.press("me:c:n")
    assert "Текущая цена" in await c.say("золото, 100% роста")
    summary = await c.say("105%")
    assert "Цена: 1 050 ₽ за 1 шт." in summary and "Рейтинг: нет" in summary
    saved = await c.press(CB_SAVE)
    # предупреждения сервиса: не тот тип счёта, нет оператора, нет даты погашения
    assert saved.count("⚠️") == 3 and "оператор" in saved and "погашения" in saved
    [card] = (await store.load()).accounts[0].positions
    assert card.ticker == "DFA-AO_ZOLOTO" and card.price == D(1050) and card.operator is None
    assert card.name == "АО Золото · 7176-У: индикаторный (золото, 100% роста) · ММ: нет"
