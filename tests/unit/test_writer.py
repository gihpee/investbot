"""Writer и валидатор текста: числа только через плейсхолдеры (G4), compliance (G9), приватность FactSheet (G8)."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import BaseModel, ValidationError

from investbot.core.models import (
    Account,
    AccountKind,
    AccountSource,
    AssetClass,
    BrokerPortfolio,
    CashBalance,
    CouponKind,
    Fact,
    FundFocus,
    InstrumentMeta,
    Position,
    UserInfo,
)
from investbot.ideas.models import EntryIdea
from investbot.journal.store import Journal
from investbot.llm_roles.validator import (
    NBSP,
    compliance_errors,
    format_fact,
    numeral_words,
    render,
    validate_draft,
    validate_rendered,
)
from investbot.llm_roles.writer import (
    DEFAULT_PROMPT_PATH,
    ReportText,
    Writer,
    check_report,
    factsheet_message,
    load_prompt,
)
from investbot.rebalance.models import CostModel, TaxModel
from investbot.reports.factsheet import FactSheet, build_weekly_factsheet
from investbot.services.portfolio import PortfolioService
from investbot.services.rebalance import PreparedPlan, RebalanceService

D = Decimal
NOW = datetime(2026, 10, 10, 20, 0, tzinfo=UTC)
TODAY = date(2026, 10, 10)


def fact(fid: str, value: Decimal | str | date, unit: str) -> Fact:
    return Fact(fact_id=fid, value=value, unit=unit, as_of=NOW, source="test")  # type: ignore[arg-type]


FACTS = {f.fact_id: f for f in [
    fact("w:before:equity_ru", D("0.452"), "pct"),
    fact("target:equity_ru", D("0.40"), "pct"),
    fact("dev:before:equity_ru", D("0.052"), "pp"),
    fact("act:1:share", D("-0.025"), "pp"),
    fact("tax:rub", D("1950.4"), "rub"),
    fact("act:1:valid_until", date(2026, 10, 16), "date"),
    fact("idea:1:d_mod", D("3.4"), "years"),
    fact("idea:1:g_spread", D("40"), "bp"),
    fact("actions:count", D(3), "count"),
    fact("verdict", "нужна ребалансировка", "text"),
    fact("fees:pct_nav", D("0.0015"), "pct"),
]}
NAMES = {"I1": "ОФЗ 26238", "I2": "Эмитент Б БО-02", "I3": "Эмитент В 001Р-05", "b:equity_ru": "Акции РФ"}


# --- форматирование и рендер ---------------------------------------------------------------------------------------

@pytest.mark.parametrize(("fid", "expected"), [
    ("w:before:equity_ru", "45,2%"),
    ("fees:pct_nav", "0,15%"),
    ("dev:before:equity_ru", f"+5,2{NBSP}п.п."),
    ("act:1:share", f"−2,5{NBSP}п.п."),
    ("tax:rub", f"1{NBSP}950{NBSP}₽"),
    ("act:1:valid_until", "16.10.2026"),
    ("idea:1:d_mod", f"3,4{NBSP}года"),
    ("idea:1:g_spread", f"40{NBSP}б.п."),
    ("actions:count", "3"),
    ("verdict", "нужна ребалансировка"),
])
def test_format_fact(fid: str, expected: str) -> None:
    assert format_fact(FACTS[fid]) == expected


@pytest.mark.parametrize(("value", "expected"), [(D(1), "1 год"), (D(2), "2 года"), (D(5), "5 лет"),
                                                 (D(11), "11 лет"), (D(21), "21 год")])
def test_years_plural(value: Decimal, expected: str) -> None:
    assert format_fact(fact("y", value, "years")) == expected


def test_render_substitutes_placeholders_and_masks_names() -> None:
    draft = ("Доля {{i:b:equity_ru}} {{f:w:before:equity_ru}} при цели {{f:target:equity_ru}} "
             "({{f:dev:before:equity_ru}}). Действие по IPS: {{f:verdict}}, {{i:I1}} до {{f:act:1:valid_until}}.")
    assert validate_draft(draft, FACTS, NAMES) == []
    text, spans = render(draft, FACTS, NAMES)
    assert text == (f"Доля Акции РФ 45,2% при цели 40,0% (+5,2{NBSP}п.п.). Действие по IPS: нужна ребалансировка, "
                    "ОФЗ 26238 до 16.10.2026.")
    assert [text[a:b] for a, b in spans] == ["Акции РФ", "нужна ребалансировка", "ОФЗ 26238"]
    assert validate_rendered(text, spans, FACTS) == []


def test_render_rejects_unknown_placeholder() -> None:
    with pytest.raises(ValueError, match="неизвестный"):
        render("{{f:nope}}", FACTS, NAMES)


# --- черновик: цифры, числительные, плейсхолдеры, compliance -------------------------------------------------------

@pytest.mark.parametrize("draft", [
    "Доля акций 45%.",
    "Отклонение 5,2 п.п. от цели.",
    "Действие до 16.10.",
    "Купон дважды в год? Нет: {{f:actions:count}} раза в 2026 году.",
    "Налог около 1 950 ₽.",
    "Доля ２５ — полноширинные цифры тоже цифры.",
])
def test_raw_digits_rejected(draft: str) -> None:
    errs = validate_draft(draft, FACTS, NAMES)
    assert any("цифры вне плейсхолдеров" in e for e in errs), errs


@pytest.mark.parametrize(("draft", "word"), [
    ("Доля акций выросла вдвое.", "вдвое"),
    ("Акции занимают треть портфеля.", "треть"),
    ("Около четверти портфеля — облигации.", "четверти"),
    ("Это пятнадцать процентов портфеля.", "пятнадцать"),
    ("Это пятнадцать процентов портфеля.", "процентов"),
    ("Горизонт полтора года.", "полтора"),
    ("Отклонение уже третью неделю подряд.", "третью"),
    ("Половина плана — продажи.", "Половина"),
    ("Три действия по IPS.", "Три"),
    ("Две бумаги и сто тысяч рублей.", "тысяч"),
    ("Это двухлетние облигации.", "двухлетние"),
    ("Продано на два миллиона.", "миллиона"),
    ("Срок — полгода.", "полгода"),
    ("Купон платится дважды.", "дважды"),
])
def test_numeral_words_rejected(draft: str, word: str) -> None:
    assert word in numeral_words(draft)
    errs = validate_draft(draft, FACTS, NAMES)
    assert any("числительные словами" in e for e in errs), errs


@pytest.mark.parametrize("draft", [
    "Один из классов вне коридора; первый шаг — продажа, второй — покупка; оба действия по IPS.",
    "Во-первых, доля выше цели. Во-вторых, налог учтён. В-третьих, лимит соблюдён.",
    "Процентный риск растёт, проценты по купонам приходят на счёт.",
    "Пятница, четверг, семья, сотрудник, стабильный, трейдинг, третейский суд, удобный.",
])
def test_ordinary_words_pass(draft: str) -> None:
    assert validate_draft(draft, FACTS, NAMES) == []


@pytest.mark.parametrize("draft", [
    "Продажа внутри счёта по правилам 39-ФЗ.",
    "ЦФА доступны по указанию 7176-У.",
    "Позиция на ИИС-3 не создаёт текущего налога.",
    "Расчёты по T+1, деньги придут на следующий торговый день.",
    "Расчёты по Т+1 с кириллической буквой.",
    "1. Продажа {{i:I1}}.\n2. Покупка {{i:I2}}.\n3) Покупка {{i:I3}}.",
])
def test_allowlist_passes(draft: str) -> None:
    assert validate_draft(draft, FACTS, NAMES) == []
    text, spans = render(draft, FACTS, NAMES)
    assert validate_rendered(text, spans, FACTS) == []


@pytest.mark.parametrize("draft", ["По ИИС-30 налог иной.", "Закон 1239-ФЗ.", "Пункт посреди строки 1. текст"])
def test_allowlist_is_narrow(draft: str) -> None:
    assert any("цифры" in e for e in validate_draft(draft, FACTS, NAMES))


def test_unknown_and_malformed_placeholders_rejected() -> None:
    errs = validate_draft("Доля {{f:w:before:bonds}} у {{i:I9}}, {f:verdict}, {{f:verdict}}{{f:actions:count}}",
                          FACTS, NAMES)
    assert any("{{f:w:before:bonds}}" in e and "{{i:I9}}" in e for e in errs)
    assert any("фигурные скобки" in e for e in errs)
    assert any("подряд" in e for e in errs)


def test_unit_after_placeholder_rejected() -> None:
    errs = validate_draft("Отклонение {{f:dev:before:equity_ru}} п.п., доля {{f:w:before:equity_ru}}%", FACTS, NAMES)
    assert any("единица" in e for e in errs)
    assert any("«%»" in e for e in errs)


@pytest.mark.parametrize(("draft", "hit"), [
    ("Бумага гарантированно вырастет.", "гарантированно"),
    ("Эта облигация точно вырастет.", "точно вырастет"),
    ("Вложение без риска.", "без риска"),
    ("Срочно купите {{i:I1}}.", "срочно купите"),
    ("Купите {{i:I1}} на свободные деньги.", "купите"),
    ("Продайте {{i:I2}}.", "продайте"),
    ("Доходность гарантирована договором.", "гарантирована"),
    ("Тут работает инсайд.", "инсайд"),
])
def test_compliance_banned(draft: str, hit: str) -> None:
    errs = validate_draft(draft, FACTS, NAMES)
    assert any(hit in e and "запрещённая формулировка" in e for e in errs), errs


def test_compliance_allows_neutral_wording() -> None:
    assert compliance_errors("Действие по IPS: рассмотреть покупку. Доходность не гарантирована.") == []


# --- названия с цифрами и даты ------------------------------------------------------------------------------------

def test_instrument_names_with_digits_only_via_placeholder() -> None:
    raw = "Действие по IPS: покупка ОФЗ 26238 и Эмитент В 001Р-05."
    assert any("цифры" in e for e in validate_draft(raw, FACTS, NAMES))
    draft = "Действие по IPS: покупка {{i:I1}}, {{i:I2}} и {{i:I3}}."
    assert validate_draft(draft, FACTS, NAMES) == []
    text, spans = render(draft, FACTS, NAMES)
    assert "ОФЗ 26238" in text and "БО-02" in text and "001Р-05" in text
    assert validate_rendered(text, spans, FACTS) == []
    assert validate_rendered(text, [], FACTS) != []  # без маскирования цифры названий не сверяются с фактами


def test_dates_checked_against_date_facts() -> None:
    assert validate_rendered("Зона входа действует до 16.10.2026, то есть до 16.10.", [], FACTS) == []
    errs = validate_rendered("Действует до 17.10.2026.", [], FACTS)
    assert errs == ["дата без факта: «17.10.2026»"]
    # дата не принимается за число и наоборот: «16,10» — число, а не дата
    assert validate_rendered("Значение 16,10.", [], FACTS) == ["число без факта: «16,10»"]


def test_numeric_grounding() -> None:
    assert validate_rendered(f"Налог 1{NBSP}950 ₽, доля 45,2%, издержки 0,15%, сделок 3.", [], FACTS) == []
    errs = validate_rendered("Доля 45,3%, налог 1 950 ₽.", [], FACTS)  # обычный пробел — это два числа
    assert errs == ["число без факта: «45,3»", "число без факта: «1»", "число без факта: «950»"]
    assert "число без факта: «−45,2»" in validate_rendered("Доля −45,2%.", [], FACTS)
    assert validate_rendered("Коридор 40,0%-45,2%.", [], FACTS) == []  # дефис между значениями — диапазон


# --- hypothesis: всё, что выдаёт render из черновика из одних плейсхолдеров, проходит validate_rendered ------------

SEPARATORS = [" ", ", ", "; ", " и ", " — ", " → ", "\n", " (", ") ", ": ", " по 39-ФЗ ", " на ИИС-3 ", " T+1 "]
NUMERIC_VALUES = {
    "pct": st.decimals(min_value=-50, max_value=50, places=6),
    "pp": st.decimals(min_value=-5, max_value=5, places=6),
    "rub": st.decimals(min_value=-10**9, max_value=10**9, places=2),
    "bp": st.decimals(min_value=-5000, max_value=5000, places=1),
    "years": st.decimals(min_value=0, max_value=50, places=4),
    "count": st.decimals(min_value=0, max_value=10**7, places=0) | st.decimals(min_value=0, max_value=100, places=3),
}


@st.composite
def fact_strategy(draw: st.DrawFn, idx: int) -> Fact:
    unit = draw(st.sampled_from([*NUMERIC_VALUES, "date", "text"]))
    value: Decimal | str | date
    if unit == "date":
        value = draw(st.dates(min_value=date(2000, 1, 1), max_value=date(2099, 12, 31)))
    elif unit == "text":
        value = draw(st.text(alphabet="абвгдеж ОФЗ0123456789-.,%№", min_size=1, max_size=20))
    else:
        value = draw(NUMERIC_VALUES[unit])
    return fact(f"x:{idx}", value, unit)


@st.composite
def draft_strategy(draw: st.DrawFn) -> tuple[str, dict[str, Fact], dict[str, str]]:
    n = draw(st.integers(min_value=1, max_value=8))
    facts = {f.fact_id: f for f in (draw(fact_strategy(i)) for i in range(n))}
    names = {f"I{i}": draw(st.sampled_from(["ОФЗ 26238", "Эмитент Б БО-02", "001Р-05", "Фонд 1", "ГТЛК 2Р-01"]))
             for i in range(1, 4)}
    tokens = draw(st.lists(st.sampled_from([*(f"{{{{f:{k}}}}}" for k in facts), *(f"{{{{i:{k}}}}}" for k in names)]),
                           min_size=1, max_size=12))
    seps = draw(st.lists(st.sampled_from(SEPARATORS), min_size=len(tokens), max_size=len(tokens)))
    draft = "".join(t + s for t, s in zip(tokens, seps, strict=True))
    return draft, facts, names


@settings(max_examples=300, deadline=None)
@given(draft_strategy())
def test_render_output_always_passes_grounding(case: tuple[str, dict[str, Fact], dict[str, str]]) -> None:
    draft, facts, names = case
    assert validate_draft(draft, facts, names) == []
    text, spans = render(draft, facts, names)
    assert validate_rendered(text, spans, facts) == []


# --- FactSheet недельного отчёта ----------------------------------------------------------------------------------

def meta(ticker: str, cls: AssetClass, lot: int = 1, **kw: Any) -> InstrumentMeta:
    return InstrumentMeta(ticker=ticker, asset_class=cls, lot=lot, **kw)


EQ = meta("AAAA", AssetClass.EQUITY, lot=10, name="Компания А")
OFZ = meta("SU26238RMFS4", AssetClass.BOND, name="ОФЗ 26238", is_government=True, coupon_kind=CouponKind.FIXED,
           nominal=D(1000))
MM = meta("MMFUND", AssetClass.FUND, name="Фонд денежного рынка", fund_focus=FundFocus.MONEY_MARKET)


class FakeBroker:
    async def accounts(self) -> list[Account]:
        return [Account(account_id="B", name="Брокерский", kind=AccountKind.BROKER, source=AccountSource.TINVEST),
                Account(account_id="I", name="ИИС", kind=AccountKind.IIS, source=AccountSource.TINVEST)]

    async def user_info(self) -> UserInfo:
        return UserInfo(qualified=False)

    async def portfolio(self, account_id: str) -> BrokerPortfolio:
        if account_id == "B":
            return BrokerPortfolio(account_id="B", positions=(
                Position(account_id="B", instrument=EQ, qty=D(400), price=D(150), avg_price=D(100)),
                Position(account_id="B", instrument=OFZ, qty=D(20), price=D(950), accrued=D(10)),
            ), cash=(CashBalance(account_id="B", currency="rub", amount=D(20000)),))
        return BrokerPortfolio(account_id="I", positions=(
            Position(account_id="I", instrument=EQ, qty=D(100), price=D(150), avg_price=D(140)),
            Position(account_id="I", instrument=MM, qty=D(10), price=D(1000)),
        ), cash=(CashBalance(account_id="I", currency="rub", amount=D(5000)),))


IPS_YAML = """version: 3
buckets:
  equity_ru: {target: 0.40, band_abs: 0.05, band_rel: 0.25}
  ofz_fixed: {target: 0.45, band_abs: 0.05, band_rel: 0.25}
  money_market: {target: 0.15, band_abs: 0.05, band_rel: 0.30}
"""

IDEA = EntryIdea(
    idea_id="bond_carry_roll:SU26238RMFS4:2026-10-10", as_of=TODAY, asset_class=AssetClass.BOND,
    ticker="SU26238RMFS4", name="ОФЗ 26238", bucket="ofz_fixed", source_arm="bond_carry_roll", score=D("0.012"),
    metrics={"ytm": D("0.1763"), "d_mod": D("3.4"), "g_spread_bp": D("40")}, price=D("97.8"),
    price_unit="pct_nominal", entry_low=D("97.5"), entry_high=D("98.1"), valid_until=date(2026, 10, 23),
    horizon_weeks=26, benchmark="RGBITR", invalidation=("Доходность выше 18,5%",),
    reasons=("Доходность 17,63% к погашению",),
    risks=("Процентный риск: дюрация 3,4 г.", "Ликвидность: оборот 12,5 млн ₽ (прокси ADV20)"),
)


async def weekly_prep(tmp_path: Path) -> PreparedPlan:
    (tmp_path / "ips.yaml").write_text(IPS_YAML, encoding="utf-8")
    portfolio = PortfolioService(FakeBroker(), tmp_path / "manual.yaml", tmp_path / "ips.yaml")
    svc = RebalanceService(portfolio, None, Journal(tmp_path / "journal.jsonl"), CostModel(), TaxModel())
    return await svc.weekly_plan(TODAY)


async def test_weights_factsheet_has_no_rub_or_qty(tmp_path: Path) -> None:
    prep = await weekly_prep(tmp_path)
    assert prep.plan is not None and prep.plan.actions
    fs = build_weekly_factsheet(prep, [IDEA], "weights")
    assert fs.privacy_mode == "weights"
    assert all(f.unit != "rub" for f in fs.facts.values())
    assert not any(k.endswith((":qty", ":lots", "_rub", ":rub")) for k in fs.facts)
    assert '"unit":"rub"' not in factsheet_message(fs)
    assert not any("млн ₽" in str(f.value) for f in fs.facts.values())  # тексты идей с рублями отброшены
    seq = prep.plan.actions[0].seq
    expected = {"asof", "verdict", "drift:before", "drift:after", "w:before:equity_ru", "w:after:equity_ru",
                "target:equity_ru", "band:low:equity_ru", "band:high:equity_ru", "dev:before:equity_ru",
                f"act:{seq}:side", f"act:{seq}:w_before", f"act:{seq}:share", f"act:{seq}:valid_until",
                "turnover:pct_nav", "fees:pct_nav", "tax:pct_nav", "idea:1:score", "idea:1:ytm",
                "idea:1:valid_until", "idea:1:entry_low", "ips:version"}
    assert expected <= set(fs.facts)
    assert fs.facts["asof"].unit == "date" and fs.facts["idea:1:valid_until"].unit == "date"
    assert format_fact(fs.facts["idea:1:ytm"]) == "17,6%"
    assert format_fact(fs.facts["idea:1:entry_low"]) == "97,5%"
    assert fs.instruments[fs.links["idea:1"]] == "ОФЗ 26238"
    assert fs.links[f"act:{seq}"] in fs.instruments and fs.links[f"act:{seq}:bucket"].startswith("b:")
    # неизвестный режим приватности — самый строгий
    assert build_weekly_factsheet(prep, [IDEA], "nonsense").privacy_mode == "weights"

    full = build_weekly_factsheet(prep, [IDEA], "full")
    assert {"nav:rub", "turnover:rub", f"act:{seq}:amount_rub", f"act:{seq}:qty"} <= set(full.facts)
    assert any("млн ₽" in str(f.value) for f in full.facts.values())


def test_weights_factsheet_model_refuses_rub_facts() -> None:
    with pytest.raises(ValidationError, match="weights"):
        FactSheet(facts={"tax:rub": FACTS["tax:rub"]}, privacy_mode="weights")
    with pytest.raises(ValidationError, match="weights"):
        FactSheet(facts={"act:1:qty": fact("act:1:qty", D(10), "count")})
    assert FactSheet(facts={"tax:rub": FACTS["tax:rub"]}, privacy_mode="full").facts


async def test_partial_report_factsheet_without_plan(tmp_path: Path) -> None:
    prep = await weekly_prep(tmp_path)
    fs = build_weekly_factsheet(prep.model_copy(update={"plan": None}))
    assert format_fact(fs.facts["verdict"]).startswith("план отложен")
    assert "w:before:equity_ru" in fs.facts and "w:after:equity_ru" not in fs.facts
    assert not any(k.startswith("act:") for k in fs.facts)


# --- Writer -------------------------------------------------------------------------------------------------------

class FakeLLM:
    """Отдаёт заранее заготовленные ответы и запоминает вызовы."""

    def __init__(self, outputs: list[dict[str, Any] | None]) -> None:
        self.outputs = list(outputs)
        self.calls: list[dict[str, Any]] = []

    async def complete_json(self, role: str, messages: list[dict[str, str]], output: type[BaseModel], *,
                            has_portfolio_ctx: bool, prompt_id: str, max_tokens: int = 2000) -> Any:
        self.calls.append({"role": role, "messages": [dict(m) for m in messages], "output": output,
                           "has_portfolio_ctx": has_portfolio_ctx, "prompt_id": prompt_id})
        nxt = self.outputs.pop(0)
        return None if nxt is None else output.model_validate(nxt)


FS = FactSheet(facts=FACTS, instruments=NAMES, links={"act:1": "I1"}, privacy_mode="full", asof=TODAY)


def section(kind: str, text: str, used: list[str] | None = None) -> dict[str, Any]:
    return {"kind": kind, "text": text, "used_fact_ids": used or []}


GOOD = {"sections": [
    section("summary", "Вердикт: {{f:verdict}}. Доля {{i:b:equity_ru}} {{f:w:before:equity_ru}} при цели "
                       "{{f:target:equity_ru}}.", ["verdict", "w:before:equity_ru", "target:equity_ru"]),
    section("actions_why", "Действие по IPS: продажа {{i:I1}} до {{f:act:1:valid_until}}, объём "
                           "{{f:act:1:share}} портфеля.", ["act:1:valid_until", "act:1:share"]),
]}
BAD_DIGITS = {"sections": [section("summary", "Доля акций 45% при цели 40%.")]}


async def test_writer_success_first_attempt() -> None:
    llm = FakeLLM([GOOD])
    res = await Writer(llm).write(FS)
    assert res.attempts == 1 and res.errors == []
    assert res.html is not None
    assert res.html.startswith("Вердикт: нужна ребалансировка. Доля Акции РФ 45,2% при цели 40,0%.")
    assert "<b>Почему такие действия</b>\nДействие по IPS: продажа ОФЗ 26238 до 16.10.2026" in res.html
    assert f"−2,5{NBSP}п.п." in res.html
    call = llm.calls[0]
    assert call["role"] == "writer" and call["output"] is ReportText and call["has_portfolio_ctx"] is True
    assert call["prompt_id"].startswith("writer.v1:")
    system, user = call["messages"]
    assert system["role"] == "system" and "{{f:ID}}" in system["content"]
    payload = json.loads(user["content"])
    assert {"id": "w:before:equity_ru", "value": "45,2%", "unit": "pct"} in payload["facts"]
    assert payload["names"]["I1"] == "ОФЗ 26238" and payload["links"] == {"act:1": "I1"}
    assert res.report is not None and res.prompt_id == call["prompt_id"]


async def test_writer_regenerates_with_feedback_then_succeeds() -> None:
    llm = FakeLLM([BAD_DIGITS, GOOD])
    res = await Writer(llm).write(FS)
    assert res.html is not None and res.attempts == 2
    assert res.errors and all(e.startswith("попытка 1:") for e in res.errors)
    retry = llm.calls[1]["messages"]
    assert [m["role"] for m in retry] == ["system", "user", "assistant", "user"]
    assert "45%" in retry[2]["content"]  # модель видит свой отклонённый черновик
    assert "цифры вне плейсхолдеров" in retry[3]["content"] and "«45%»" in retry[3]["content"]


async def test_writer_gives_up_after_three_attempts() -> None:
    banned = {"sections": [section("summary", "Срочно купите {{i:I1}}, бумага гарантированно вырастет.")]}
    llm = FakeLLM([BAD_DIGITS, banned, BAD_DIGITS])
    res = await Writer(llm).write(FS)
    assert res.html is None and res.attempts == 3 and len(llm.calls) == 3
    assert any(e.startswith("попытка 2:") and "запрещённая формулировка" in e for e in res.errors)
    assert any(e.startswith("попытка 3:") for e in res.errors)


async def test_writer_llm_unavailable_falls_back_immediately() -> None:
    llm = FakeLLM([None])
    res = await Writer(llm).write(FS)
    assert res.html is None and res.attempts == 1 and len(llm.calls) == 1


async def test_writer_rejects_values_glued_after_render() -> None:
    """Черновик без цифр, но «{{f:a}},{{f:b}}» после подстановки даёт число, которого нет в фактах."""
    glued = {"sections": [section("summary", "Сделок {{f:actions:count}},{{f:actions:count}} за неделю.")]}
    llm = FakeLLM([glued, GOOD])
    res = await Writer(llm).write(FS)
    assert res.attempts == 2 and any("после подстановки" in e and "3,3" in e for e in res.errors)


def test_check_report_rejects_duplicates_unknown_ids_and_long_text() -> None:
    dup = ReportText.model_validate({"sections": [section("summary", "Текст."), section("summary", "Ещё.")]})
    html, errs = check_report(dup, FS, 3000)
    assert html is None and any("повторяются" in e for e in errs)
    unknown = ReportText.model_validate({"sections": [section("summary", "Текст.", ["nope"])]})
    assert any("used_fact_ids" in e for e in check_report(unknown, FS, 3000)[1])
    html, errs = check_report(ReportText.model_validate(GOOD), FS, 50)
    assert html is None and "слишком длинный" in errs[0]


def test_html_is_escaped() -> None:
    fs = FactSheet(facts=FACTS, instruments={"I1": "Эмитент <А> & Ко"}, privacy_mode="full")
    rep = ReportText.model_validate({"sections": [section("risks", "Риск по {{i:I1}} & <b>не тег</b>.")]})
    html, errs = check_report(rep, fs, 3000)
    assert errs == []
    assert html == "<b>Риски и издержки</b>\nРиск по Эмитент &lt;А&gt; &amp; Ко &amp; &lt;b&gt;не тег&lt;/b&gt;."


async def test_end_to_end_from_weekly_plan(tmp_path: Path) -> None:
    prep = await weekly_prep(tmp_path)
    fs = build_weekly_factsheet(prep, [IDEA])
    seq = prep.plan.actions[0].seq if prep.plan else 1
    out = {"sections": [
        section("summary", "Вердикт: {{f:verdict}}. Отклонение от целей IPS {{f:drift:before}}, после плана "
                           "{{f:drift:after}}.", ["verdict", "drift:before", "drift:after"]),
        section("actions_why", f"Действие по IPS: {{{{f:act:{seq}:side}}}} {{{{i:{fs.links[f'act:{seq}']}}}}}, "
                               f"объём {{{{f:act:{seq}:share}}}} портфеля."),
        section("ideas", "Идея: {{i:" + fs.links["idea:1"] + "}}, доходность {{f:idea:1:ytm}}, зона входа "
                         "{{f:idea:1:entry_low}}–{{f:idea:1:entry_high}} до {{f:idea:1:valid_until}}."),
    ]}
    res = await Writer(FakeLLM([out])).write(fs)
    assert res.html is not None, res.errors
    assert "Идея: ОФЗ 26238, доходность 17,6%, зона входа 97,5%–98,1% до 23.10.2026." in res.html
    assert "₽" not in res.html


def test_prompt_front_matter_and_hash(tmp_path: Path) -> None:
    p = load_prompt(DEFAULT_PROMPT_PATH)
    assert (p.id, p.version, p.role, p.output) == ("writer", 1, "writer", "ReportText")
    assert p.sha256 == hashlib.sha256(DEFAULT_PROMPT_PATH.read_bytes()).hexdigest()
    assert p.prompt_id == f"writer.v1:{p.sha256[:12]}" and not p.body.startswith("---")
    other = tmp_path / "critic.v1.md"
    other.write_text("---\nid: critic\nversion: 1\nrole: critic\noutput: RiskReview\n---\nТекст\n", encoding="utf-8")
    with pytest.raises(ValueError, match="ожидался writer"):
        Writer(FakeLLM([]), other)
    broken = tmp_path / "broken.md"
    broken.write_text("без front-matter", encoding="utf-8")
    with pytest.raises(ValueError, match="front-matter"):
        load_prompt(broken)
