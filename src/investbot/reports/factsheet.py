"""FactSheet недельного отчёта (§9.5): все числа, даты и названия, на которые может сослаться Writer.

Код считает — LLM объясняет (§9.1): Writer видит только факты отсюда и пишет `{{f:ID}}` и `{{i:ID}}`,
значения подставляет рендерер (llm_roles/validator.py).

Схема идентификаторов фактов (стабильная и читаемая; `<b>` — ключ бакета IPS, N — seq действия плана,
K — номер идеи в ленте, J — номер формулировки):
- общее: `asof`, `data:snapshot` (date), `ips:version` (count), `verdict` (text), `drift:before`,
  `drift:after` (pct), `limit:weekly_turnover` (pct);
- классы активов: `w:before:<b>`, `w:after:<b>`, `target:<b>`, `band:low:<b>`, `band:high:<b>` (pct),
  `dev:before:<b>`, `dev:after:<b>` (pp, отклонение от цели), `status:before:<b>`, `status:after:<b>` (text);
  название класса — `{{i:b:<b>}}`;
- действия: `act:N:side`, `act:N:reason`, `act:N:account` (text), `act:N:w_before`, `act:N:w_after` (pct, доля
  бакета до и после), `act:N:share` (pp, объём сделки в долях портфеля со знаком), `act:N:valid_until` (date);
  бумага действия — `links["act:N"]` → `I1…`, его бакет — `links["act:N:bucket"]` → `b:<b>`;
- итоги: `actions:count`, `actions:sell`, `actions:buy` (count), `turnover:pct_nav`, `sells:pct_nav`,
  `fees:pct_nav`, `tax:pct_nav` (pct), `deferred:count`, `unfunded:count`, `violations:count`, `notes:count`;
- идеи: `ideas:count`, `idea:K:score`, `idea:K:ytm`, `idea:K:target_yield`, `idea:K:price`, `idea:K:entry_low`,
  `idea:K:entry_high` (pct), `idea:K:d_mod` (years), `idea:K:g_spread`, `idea:K:breakeven` (bp),
  `idea:K:horizon_weeks` (count), `idea:K:valid_until` (date), `idea:K:benchmark`, `idea:K:underweight`,
  `idea:K:note`, `idea:K:reason:J`, `idea:K:risk:J`, `idea:K:invalidation:J` (text); `links["idea:K"]`;
- качество данных: `quality:level`, `quality:gates_failed`, `quality:unpriced` (count), `quality:flag:J` (text),
  `quality:broker_as_of` (date);
- только в режиме `full`: `nav:rub`, `turnover:rub`, `sells:rub`, `fees:rub`, `tax:rub`, `act:N:amount_rub`,
  `act:N:fee_rub`, `act:N:tax_rub` (rub), `act:N:qty`, `act:N:lots` (count).

Приватность (G8, N4): в режиме `weights` нет ни одного рублёвого факта и количества — только доли, п.п. и
проценты; это проверяет сама модель `FactSheet`. Свободные тексты сервиса (примечания, deferred, unfunded) могут
содержать суммы и количества — в FactSheet попадает только их число; формулировки идей с «₽» в `weights`
отбрасываются. Названия бумаг передаются через `instruments`: цифры в них («ОФЗ 26238») допустимы только так.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Literal

from pydantic import Field, model_validator

from investbot.channels.telegram.render import title
from investbot.core.models import AccountKind, Fact, Strict
from investbot.ideas.models import EntryIdea
from investbot.ops.degradation import MSK
from investbot.rebalance.models import RebalancePlan
from investbot.services.rebalance import PreparedPlan

PrivacyMode = Literal["weights", "full"]
Unit = Literal["pct", "rub", "bp", "years", "pp", "count", "text", "date"]
QTY_SUFFIXES = (":qty", ":lots")
MAX_NAME = 80
IDEA_TEXTS = (("reason", "reasons", 3), ("risk", "risks", 3), ("invalidation", "invalidation", 2))

SRC_PLAN = "rebalance_plan"
SRC_PORTFOLIO = "portfolio"
SRC_IPS = "ips"
SRC_QUALITY = "degradation"

VERDICT_TEXT = {
    "rebalance": "нужна ребалансировка",
    "cashflow_only": "распределить свободные деньги без продаж",
    "no_action": "действий не требуется",
    "blocked": "план заблокирован лимитами",
}
PLAN_DEFERRED = "план отложен: данных недостаточно для списка сделок"
SIDE_TEXT = {"sell": "продажа", "buy": "покупка"}
STATUS_TEXT = {
    "ok": "в коридоре",
    "below": "ниже коридора",
    "above": "выше коридора",
    "cap_breach": "выше потолка",
    "not_in_ips": "нет в IPS",
}
REASON_TEXT = {
    "BAND_RETURN": "возврат в коридор",
    "CASHFLOW_FIRST": "свободные деньги в недовес",
    "UNDERWEIGHT": "недовес класса",
    "CAP_BREACH": "превышен потолок",
    "CASH_SURPLUS": "излишек денег",
}
ACCOUNT_TEXT = {
    AccountKind.BROKER: "брокерский счёт",
    AccountKind.IIS: "ИИС",
    AccountKind.DFA: "счёт ЦФА",
    AccountKind.INVEST_BOX: "Инвесткопилка",
    AccountKind.INVEST_FUND: "счёт фондов",
    AccountKind.OTHER: "другой счёт",
}
FLAG_TEXT = {
    "data_fallback": "часть данных из резервных источников",
    "broker_stale": "портфель по сохранённому снимку брокера, продажи не предлагаются",
    "broker_too_old": "снимок брокера устарел, список сделок не строится",
    "gate_critical": "критичная проверка данных не пройдена, план отложен",
}


class FactSheet(Strict):
    """Факты (fact_id → Fact), названия (i-id → отображаемое имя) и связи сущностей с названиями."""

    facts: dict[str, Fact]
    instruments: dict[str, str] = Field(default_factory=dict)  # I1… — бумаги, b:<bucket> — классы активов
    links: dict[str, str] = Field(default_factory=dict)  # "act:1" → "I1", "act:1:bucket" → "b:equity_ru"
    privacy_mode: PrivacyMode = "weights"
    asof: date | None = None
    kind: str = "weekly_report"

    @model_validator(mode="after")
    def _check(self) -> FactSheet:
        if bad := [k for k, f in self.facts.items() if k != f.fact_id]:
            raise ValueError(f"ключ факта не совпадает с fact_id: {bad}")
        if self.privacy_mode == "weights" and (
            leaked := [k for k, f in self.facts.items() if f.unit == "rub" or k.endswith(QTY_SUFFIXES)]
        ):
            raise ValueError(f"режим weights: рублёвые суммы и количества запрещены: {leaked}")
        if dangling := [k for k, v in self.links.items() if v not in self.instruments]:
            raise ValueError(f"связи без названия: {dangling}")
        return self


def _msk_date(dt: datetime) -> date:
    return dt.astimezone(MSK).date() if dt.tzinfo is not None else dt.date()


class _Builder:
    def __init__(self, mode: PrivacyMode, as_of: datetime) -> None:
        self.full = mode == "full"
        self.as_of = as_of
        self.facts: dict[str, Fact] = {}
        self.instruments: dict[str, str] = {}
        self.links: dict[str, str] = {}
        self._by_ticker: dict[str, str] = {}

    def add(self, fid: str, value: Decimal | str | date | None, unit: Unit, source: str,
            as_of: datetime | None = None) -> None:
        if value is None or (not self.full and (unit == "rub" or fid.endswith(QTY_SUFFIXES))):
            return
        self.facts[fid] = Fact(fact_id=fid, value=value, unit=unit, as_of=as_of or self.as_of, source=source)

    def count(self, fid: str, n: int, source: str, *, skip_zero: bool = False) -> None:
        if n or not skip_zero:
            self.add(fid, Decimal(n), "count", source)

    def instrument(self, ticker: str, name: str) -> str:
        iid = self._by_ticker.get(ticker)
        if iid is None:
            iid = f"I{len(self._by_ticker) + 1}"
            self._by_ticker[ticker] = iid
            self.instruments[iid] = (name.strip() or ticker)[:MAX_NAME]
        return iid

    def bucket(self, bucket: str) -> str:
        iid = f"b:{bucket}"
        self.instruments[iid] = title(bucket)
        return iid


def _reasons(codes: tuple[str, ...]) -> str:
    return ", ".join(dict.fromkeys(REASON_TEXT.get(c.split(":")[0], "правило IPS") for c in codes)) or "правило IPS"


def _bucket_facts(bld: _Builder, bucket: str, before: Decimal, after: Decimal | None, target: Decimal | None,
                  low: Decimal | None, high: Decimal | None, status_before: str, status_after: str | None,
                  source: str) -> None:
    bld.bucket(bucket)
    bld.add(f"w:before:{bucket}", before, "pct", source)
    bld.add(f"w:after:{bucket}", after, "pct", source)
    bld.add(f"target:{bucket}", target, "pct", SRC_IPS)
    bld.add(f"band:low:{bucket}", low, "pct", SRC_IPS)
    bld.add(f"band:high:{bucket}", high, "pct", SRC_IPS)
    if target is not None:
        bld.add(f"dev:before:{bucket}", before - target, "pp", source)
        if after is not None:
            bld.add(f"dev:after:{bucket}", after - target, "pp", source)
    bld.add(f"status:before:{bucket}", STATUS_TEXT.get(status_before, status_before), "text", source)
    if status_after is not None:
        bld.add(f"status:after:{bucket}", STATUS_TEXT.get(status_after, status_after), "text", source)


def _plan_facts(bld: _Builder, plan: RebalancePlan, kinds: dict[str, AccountKind], extra_notes: int) -> None:
    bld.add("verdict", VERDICT_TEXT.get(plan.verdict, plan.verdict), "text", SRC_PLAN)
    bld.add("drift:before", plan.drift_before, "pct", SRC_PLAN)
    bld.add("drift:after", plan.drift_after, "pct", SRC_PLAN)
    for b in plan.buckets:
        _bucket_facts(bld, b.bucket, b.weight_before, b.weight_after, b.target, b.low, b.high,
                      b.status_before, b.status_after, SRC_PLAN)

    actions = sorted(plan.actions, key=lambda a: a.seq)
    nav = plan.nav_rub
    sells = sum((a.est_amount_rub for a in actions if a.side == "sell"), Decimal(0))
    bld.count("actions:count", len(actions), SRC_PLAN)
    bld.count("actions:sell", sum(1 for a in actions if a.side == "sell"), SRC_PLAN)
    bld.count("actions:buy", sum(1 for a in actions if a.side == "buy"), SRC_PLAN)
    for a in actions:
        p = f"act:{a.seq}"
        bld.links[p] = bld.instrument(a.ticker, a.name)
        bld.links[f"{p}:bucket"] = bld.bucket(a.bucket)
        bld.add(f"{p}:side", SIDE_TEXT[a.side], "text", SRC_PLAN)
        bld.add(f"{p}:reason", _reasons(a.reason_codes), "text", SRC_PLAN)
        kind = kinds.get(a.account_id)
        bld.add(f"{p}:account", ACCOUNT_TEXT.get(kind, "счёт") if kind is not None else "счёт", "text", SRC_PLAN)
        bld.add(f"{p}:w_before", a.w_bucket_before, "pct", SRC_PLAN)
        bld.add(f"{p}:w_after", a.w_bucket_after, "pct", SRC_PLAN)
        if nav > 0:
            sign = -1 if a.side == "sell" else 1
            bld.add(f"{p}:share", sign * a.est_amount_rub / nav, "pp", SRC_PLAN)
        bld.add(f"{p}:valid_until", a.valid_until, "date", SRC_PLAN)
        if bld.full:
            bld.add(f"{p}:amount_rub", a.est_amount_rub, "rub", SRC_PLAN)
            bld.add(f"{p}:fee_rub", a.est_fee_rub, "rub", SRC_PLAN)
            if a.est_tax_rub:
                bld.add(f"{p}:tax_rub", a.est_tax_rub, "rub", SRC_PLAN)
            bld.count(f"{p}:qty", a.qty, SRC_PLAN)
            bld.count(f"{p}:lots", a.lots, SRC_PLAN)
    if nav > 0:
        bld.add("turnover:pct_nav", plan.turnover_rub / nav, "pct", SRC_PLAN)
        bld.add("sells:pct_nav", sells / nav, "pct", SRC_PLAN)
        bld.add("fees:pct_nav", plan.fees_rub / nav, "pct", SRC_PLAN)
        bld.add("tax:pct_nav", plan.tax_rub / nav, "pct", SRC_PLAN)
    if bld.full:
        bld.add("nav:rub", nav, "rub", SRC_PLAN)
        bld.add("turnover:rub", plan.turnover_rub, "rub", SRC_PLAN)
        bld.add("sells:rub", sells, "rub", SRC_PLAN)
        bld.add("fees:rub", plan.fees_rub, "rub", SRC_PLAN)
        bld.add("tax:rub", plan.tax_rub, "rub", SRC_PLAN)
    bld.count("deferred:count", len(plan.deferred), SRC_PLAN, skip_zero=True)
    bld.count("unfunded:count", len(plan.unfunded), SRC_PLAN, skip_zero=True)
    bld.count("violations:count", len(plan.violations), SRC_PLAN, skip_zero=True)
    bld.count("notes:count", len(plan.notes) + extra_notes, SRC_PLAN, skip_zero=True)


def _quality_facts(bld: _Builder, prep: PreparedPlan) -> None:
    deg = prep.degradation
    if deg.level > 0:
        bld.count("quality:level", deg.level, SRC_QUALITY)
    for j, flag in enumerate((f for f in deg.flags if f in FLAG_TEXT), 1):
        bld.add(f"quality:flag:{j}", FLAG_TEXT[flag], "text", SRC_QUALITY)
    bld.count("quality:gates_failed", sum(1 for g in deg.gates if not g.ok), SRC_QUALITY, skip_zero=True)
    if deg.broker_as_of is not None:
        bld.add("quality:broker_as_of", _msk_date(deg.broker_as_of), "date", SRC_QUALITY)
    bld.count("quality:unpriced", len(prep.overview.valuation.unpriced), SRC_PORTFOLIO, skip_zero=True)


def _idea_facts(bld: _Builder, ideas: Sequence[EntryIdea]) -> None:
    if ideas:
        bld.count("ideas:count", len(ideas), "ideas")
    for k, idea in enumerate(ideas, 1):
        p = f"idea:{k}"
        src = f"ideas:{idea.source_arm}"
        at = datetime.combine(idea.as_of, time(), tzinfo=UTC)
        bld.links[p] = bld.instrument(idea.ticker, idea.name)
        bld.links[f"{p}:bucket"] = bld.bucket(idea.bucket)
        m = idea.metrics
        bld.add(f"{p}:score", idea.score, "pct", src, at)
        bld.add(f"{p}:ytm", m.get("ytm"), "pct", src, at)
        bld.add(f"{p}:target_yield", idea.target_yield, "pct", src, at)
        bld.add(f"{p}:d_mod", m.get("d_mod"), "years", src, at)
        bld.add(f"{p}:g_spread", m.get("g_spread_bp"), "bp", src, at)
        bld.add(f"{p}:breakeven", m.get("breakeven_bp"), "bp", src, at)
        if idea.price_unit == "pct_nominal":  # цена в % номинала — это доля, а не сумма в рублях
            bld.add(f"{p}:price", idea.price / 100, "pct", src, at)
            bld.add(f"{p}:entry_low", idea.entry_low / 100, "pct", src, at)
            bld.add(f"{p}:entry_high", idea.entry_high / 100, "pct", src, at)
        bld.count(f"{p}:horizon_weeks", idea.horizon_weeks, src)
        bld.add(f"{p}:valid_until", idea.valid_until, "date", src, at)
        bld.add(f"{p}:benchmark", idea.benchmark, "text", src, at)
        bld.add(f"{p}:underweight", "да" if idea.for_underweight_bucket else "нет", "text", src, at)
        bld.add(f"{p}:note", idea.note, "text", src, at)
        for key, attr, limit in IDEA_TEXTS:
            texts: tuple[str, ...] = getattr(idea, attr)
            kept = [t for t in texts if bld.full or "₽" not in t][:limit]
            for j, t in enumerate(kept, 1):
                bld.add(f"{p}:{key}:{j}", t, "text", src, at)


def build_weekly_factsheet(prep: PreparedPlan, ideas: Sequence[EntryIdea] = (),
                           privacy_mode: str = "weights") -> FactSheet:
    """Факты недельного отчёта. Неизвестный режим приватности трактуется как самый строгий (`weights`)."""
    mode: PrivacyMode = "full" if privacy_mode == "full" else "weights"
    ov = prep.overview
    taken = ov.snapshot.taken_at
    plan = prep.plan
    asof = plan.as_of if plan is not None else _msk_date(taken)
    bld = _Builder(mode, taken)
    bld.add("asof", asof, "date", SRC_PLAN)
    bld.add("data:snapshot", _msk_date(taken), "date", SRC_PORTFOLIO)
    version = plan.ips_version if plan is not None else (ov.ips.version if ov.ips is not None else None)
    if version is not None:
        bld.count("ips:version", version, SRC_IPS)
    if ov.ips is not None:
        bld.add("limit:weekly_turnover", ov.ips.limits.max_weekly_turnover, "pct", SRC_IPS)
    if plan is None:
        bld.add("verdict", PLAN_DEFERRED, "text", SRC_QUALITY)
        for d in ov.drift:
            _bucket_facts(bld, d.bucket, d.weight, None, d.target, d.low, d.high, d.status, None, SRC_PORTFOLIO)
    else:
        kinds = {a.account_id: a.kind for a in ov.snapshot.accounts}
        _plan_facts(bld, plan, kinds, len(prep.notes) + len(ov.warnings))
    _quality_facts(bld, prep)
    _idea_facts(bld, ideas)
    return FactSheet(facts=bld.facts, instruments=bld.instruments, links=bld.links, privacy_mode=mode, asof=asof)
