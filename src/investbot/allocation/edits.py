"""Правки черновика аллокации (кнопки конструктора, ARCHITECTURE §10.8): пересчёт и diff «было → стало».

Правило: молча анкету не нарушаем. Всё, что правка не смогла сделать из-за границ анкеты или что
расходится с анкетой после правки (границы, исключения, фиксированные доли, стресс), попадает
в `DraftDiff.violations`. Решает пользователь: вернуть правку, поменять анкету или оставить с пометкой.

- shift_bucket: +value к бакету за счёт остальных редактируемых пропорционально их долям
  (value < 0 — отдать остальным пропорционально); доноры не ниже минимума, получатели не выше максимума.
- less_risk / more_risk: value (по умолчанию 5 п.п.) из растущих бакетов в защитные пропорционально или наоборот,
  в тех же границах.
- exclude: бакет в 0, его доля — остальным пропорционально (сверх потолков — с пометкой).
- set_weight: доля бакета ровно value, остальные редактируемые масштабируются.
Редактируемые — бакеты черновика, кроме фиксированных и исключённых анкетой. Доли после правки округляются
до 0,01 п.п. методом наибольших остатков; сумма сохраняется точно.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import ROUND_FLOOR, ROUND_HALF_EVEN, Decimal

from investbot.allocation.models import (
    AllocationEdit,
    DraftDiff,
    ReturnsData,
    RiskProfile,
    StressScenario,
    bucket_title,
)
from investbot.allocation.proposer import check_violations, fmt_pct, metrics
from investbot.allocation.risk import ScenarioTable
from investbot.core.money import ONE, ZERO, round_money

DEFAULT_RISK_STEP = Decimal("0.05")
QUANTUM = Decimal("0.0001")
_EPS = Decimal("1e-12")

Weights = dict[str, Decimal]


def _fmt_pp(x: Decimal) -> str:
    """0.05 → «+5,0 п.п.», −0.05 → «−5,0 п.п.» (core.money.pp заменяет точки и в «п.п.»)."""
    v = round_money(x * 100, 1)
    sign = "+" if v > 0 else "−" if v < 0 else ""
    return f"{sign}{abs(v)}".replace(".", ",") + " п.п."


def _take(w: Weights, donors: Sequence[str], amount: Decimal, lo: Mapping[str, Decimal]) -> Decimal:
    """Снимает до `amount` с доноров пропорционально долям, не ниже `lo`. Возвращает снятое."""
    rest = amount
    active = [d for d in donors if w[d] - lo[d] > _EPS]
    while rest > _EPS and active:
        base = sum((w[d] for d in active), ZERO)
        share = {d: rest * w[d] / base for d in active}
        capped = [d for d in active if share[d] >= w[d] - lo[d]]
        if not capped:
            for d in active:
                w[d] -= share[d]
            rest = ZERO
            break
        for d in capped:
            rest -= w[d] - lo[d]
            w[d] = lo[d]
        active = [d for d in active if d not in capped]
    return amount - rest


def _give(w: Weights, receivers: Sequence[str], amount: Decimal, hi: Mapping[str, Decimal]) -> Decimal:
    """Добавляет до `amount` получателям пропорционально долям (все нули — поровну), не выше `hi`."""
    rest = amount
    active = [r for r in receivers if hi[r] - w[r] > _EPS]
    while rest > _EPS and active:
        base = sum((w[r] for r in active), ZERO)
        share = {r: rest * (w[r] / base if base > 0 else ONE / len(active)) for r in active}
        capped = [r for r in active if share[r] >= hi[r] - w[r]]
        if not capped:
            for r in active:
                w[r] += share[r]
            rest = ZERO
            break
        for r in capped:
            rest -= hi[r] - w[r]
            w[r] = hi[r]
        active = [r for r in active if r not in capped]
    return amount - rest


def _transfer(
    w: Weights,
    donors: Sequence[str],
    receivers: Sequence[str],
    amount: Decimal,
    lo: Mapping[str, Decimal],
    hi: Mapping[str, Decimal],
) -> Decimal:
    room_d = sum((max(w[d] - lo[d], ZERO) for d in donors), ZERO)
    room_r = sum((max(hi[r] - w[r], ZERO) for r in receivers), ZERO)
    moved = min(amount, room_d, room_r)
    if moved <= _EPS:
        return ZERO
    _take(w, donors, moved, lo)
    _give(w, receivers, moved, hi)
    return moved


def _quantize(w: Weights, total: Decimal) -> Weights:
    """Наибольшие остатки на сетке QUANTUM: Σ = total (округлённая до сетки), доли ≥ 0."""
    target = int((total / QUANTUM).to_integral_value(ROUND_HALF_EVEN))
    raw = {b: max(v, ZERO) / QUANTUM for b, v in w.items()}
    units = {b: int(r.to_integral_value(ROUND_FLOOR)) for b, r in raw.items()}
    rem = {b: raw[b] - units[b] for b in raw}
    diff = target - sum(units.values())
    by_rem = sorted(rem, key=lambda b: rem[b], reverse=True)
    for b in by_rem[: max(diff, 0)]:
        units[b] += 1
    for b in [b for b in reversed(by_rem) if units[b] > 0][: max(-diff, 0)]:
        units[b] -= 1
    return {b: Decimal(u) * QUANTUM for b, u in units.items()}


def apply_edit(
    weights: Mapping[str, Decimal],
    edit: AllocationEdit,
    profile: RiskProfile,
    data: ReturnsData,
    scenarios: Sequence[StressScenario],
    bucket_yields: Mapping[str, Decimal] | None = None,
    *,
    current_weights: Mapping[str, Decimal] | None = None,
) -> DraftDiff:
    """Применяет правку к черновику и пересчитывает метрики.

    `current_weights` — текущий портфель для оборота перехода; без него оборот считается от черновика до правки
    (тогда в metrics_before он 0, а в metrics_after — оборот самой правки).
    """
    before: Weights = dict(weights)
    w: Weights = dict(before)
    if edit.bucket is not None:
        w.setdefault(edit.bucket, ZERO)
    lo: Weights = {}
    hi: Weights = {}
    for b in w:
        if b in profile.fixed:
            lo[b] = hi[b] = profile.fixed[b]
        elif b in profile.excluded:
            lo[b] = hi[b] = ZERO
        else:
            lo[b] = profile.bucket_min.get(b, ZERO)
            hi[b] = min(profile.bucket_max.get(b, ONE), ONE)
    editable = [b for b in w if b not in profile.fixed and b not in profile.excluded]
    msgs: list[str] = []

    bucket, value = edit.bucket, edit.value
    if edit.op in ("less_risk", "more_risk"):
        msgs += _risk(w, edit.op == "less_risk", value or DEFAULT_RISK_STEP, editable, lo, hi, profile)
    elif bucket is None:
        raise ValueError(f"{edit.op}: в правке не указан бакет")
    elif edit.op == "exclude":
        msgs += _exclude(w, bucket, editable, hi)
    elif value is None:
        raise ValueError(f"{edit.op}: в правке не указано значение")
    elif edit.op == "shift_bucket":
        msgs += _shift(w, bucket, value, editable, lo, hi, profile)
    else:
        msgs += _set(w, bucket, value, editable)

    after = _quantize(w, sum(before.values(), ZERO))
    ref = current_weights if current_weights is not None else before
    table = ScenarioTable(data, scenarios)
    m_before = metrics(before, data, scenarios, ref, bucket_yields, table=table)
    m_after = metrics(after, data, scenarios, ref, bucket_yields, table=table)
    return DraftDiff(
        before=before,
        after=after,
        metrics_before=m_before,
        metrics_after=m_after,
        violations=tuple(msgs) + check_violations(after, profile, m_after.stress),
    )


def _shift(
    w: Weights,
    bucket: str,
    value: Decimal,
    editable: list[str],
    lo: Weights,
    hi: Weights,
    profile: RiskProfile,
) -> list[str]:
    title = bucket_title(bucket)
    if bucket in profile.fixed:
        return [
            f"{title}: доля зафиксирована анкетой ({fmt_pct(profile.fixed[bucket])}), правка не применена — "
            "измените её в анкете."
        ]
    if bucket in profile.excluded and value > 0:
        return [f"{title}: класс исключён анкетой, правка не применена."]
    others = [b for b in editable if b != bucket]
    if value > 0:
        moved = _transfer(w, others, [bucket], value, lo, hi)
    else:
        moved = -_transfer(w, [bucket], others, -value, lo, hi)
    if abs(moved - value) > _EPS:
        return [f"{title}: сдвиг {_fmt_pp(value)} ограничен границами анкеты, применено {_fmt_pp(moved)}."]
    return []


def _risk(
    w: Weights,
    less: bool,
    value: Decimal,
    editable: list[str],
    lo: Weights,
    hi: Weights,
    profile: RiskProfile,
) -> list[str]:
    growth = [b for b in editable if b in profile.growth_buckets]
    defensive = [b for b in editable if b not in profile.growth_buckets]
    donors, receivers = (growth, defensive) if less else (defensive, growth)
    label = "Меньше риска" if less else "Больше риска"
    if not donors or not receivers:
        return [f"«{label}»: нет растущих или защитных бакетов, которые можно менять; правка не применена."]
    moved = _transfer(w, donors, receivers, value, lo, hi)
    if moved < value - _EPS:
        return [
            f"«{label}»: сдвинуто {_fmt_pp(moved).lstrip('+')} из {_fmt_pp(value).lstrip('+')} — "
            "дальше не пускают границы анкеты."
        ]
    return []


def _exclude(w: Weights, bucket: str, editable: list[str], hi: Weights) -> list[str]:
    amount = w[bucket]
    if amount <= 0:
        return []
    others = [b for b in editable if b != bucket]
    if not others:
        return [f"{bucket_title(bucket)}: долю некуда перераспределить, правка не применена."]
    w[bucket] = ZERO
    placed = _give(w, others, amount, hi)
    if amount - placed > _EPS:
        _give(w, others, amount - placed, dict.fromkeys(others, ONE))  # сверх потолков — нарушения покажет проверка
    return []


def _set(w: Weights, bucket: str, value: Decimal, editable: list[str]) -> list[str]:
    """Расхождение с фиксированной долей или исключением анкеты покажет общая проверка."""
    msgs: list[str] = []
    title = bucket_title(bucket)
    others = [b for b in editable if b != bucket]
    total = sum(w.values(), ZERO)
    locked = sum((v for b, v in w.items() if b != bucket and b not in others), ZERO)
    avail = total - locked
    if value > avail:
        msgs.append(f"{title}: больше {fmt_pct(avail)} нельзя — остальное занято фиксированными долями.")
        value = avail
    rest = avail - value
    current = sum((w[b] for b in others), ZERO)
    if not others and rest > _EPS:
        return [f"{title}: остаток {fmt_pct(rest)} некуда перераспределить, правка не применена."]
    w[bucket] = value
    for b in others:
        w[b] = w[b] * rest / current if current > 0 else rest / len(others)
    return msgs
