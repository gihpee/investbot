"""Сквозной сценарий конструктора: анкета → варианты → правка → утверждение IPS → журнал."""

from __future__ import annotations

import math
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from investbot.allocation.models import AllocationEdit
from investbot.channels.telegram.allocation import Session, draft_view, variants_view
from investbot.journal.store import Journal
from investbot.rebalance.ips import load_ips
from investbot.services.allocation import AllocationService, to_risk_profile
from investbot.services.portfolio import PortfolioService
from investbot.services.proxies import ProxyStore
from investbot.services.questionnaire import QUESTIONS, ProfileAnswers

D = Decimal
ROOT = Path(__file__).resolve().parents[2]
TODAY = date(2026, 10, 9)

# Детерминированные «индексы»: тренд + синусоида + провалы в кризисные окна.
SHAPES = {
    "MCFTR": (0.0025, 0.03, -0.35),
    "RGBITR": (0.0012, 0.008, -0.12),
    "RUCBTRNS": (0.0015, 0.006, -0.10),
    "RUCBHYTR": (0.0020, 0.012, -0.18),
}


def synthetic(secid: str, frm: date, to: date) -> list[tuple[date, Decimal]]:
    drift, amp, crash = SHAPES[secid]
    out, level, d, i = [], 1000.0, max(frm, date(2014, 1, 6)), 0
    while d <= to:
        if d.weekday() == 4:
            shock = crash / 4 if (date(2014, 12, 1) <= d <= date(2014, 12, 31)
                                  or date(2022, 2, 1) <= d <= date(2022, 3, 31)) else 0.0
            level *= 1 + drift + amp * math.sin(i / 3 + len(secid)) + shock
            out.append((d, D(str(round(level, 4)))))
            i += 1
        d += timedelta(days=1)
    return out


async def fetch(secid: str, frm: date, to: date) -> list[tuple[date, Decimal]]:
    return synthetic(secid, frm, to)


def service(tmp_path: Path) -> AllocationService:
    portfolio = PortfolioService(None, tmp_path / "manual.yaml", tmp_path / "ips.yaml")
    return AllocationService(
        portfolio=portfolio,
        proxies=ProxyStore(tmp_path / "proxies", fetch),
        config_path=ROOT / "config" / "allocation.yaml",
        answers_path=tmp_path / "profile.yaml",
        ips_path=tmp_path / "ips.yaml",
        journal=Journal(tmp_path / "journal.jsonl"),
    )


ANSWERS = {"horizon": "10", "max_drawdown": "0.20", "needs_income": "no", "dfa_share": "0.05",
           "allow_hy": "no", "gold_share": "0"}


def test_questionnaire_codes_cover_all_questions() -> None:
    assert {q.key for q in QUESTIONS} == set(ANSWERS)
    for q in QUESTIONS:
        assert ANSWERS[q.key] in {code for code, _ in q.options}


def test_profile_mapping_excludes_and_fixes() -> None:
    svc_cfg = service(Path("/nonexistent")).config
    p = to_risk_profile(ProfileAnswers.from_codes(ANSWERS), svc_cfg)
    assert p.fixed == {"dfa": D("0.05")}
    assert {"gold", "corp_hy", "fx_cash", "ofz_float_linker"} <= set(p.excluded)
    assert "corp_hy" not in p.growth_buckets


async def test_end_to_end_propose_edit_approve(tmp_path: Path) -> None:
    svc = service(tmp_path)
    svc.save_answers(ProfileAnswers.from_codes(ANSWERS))
    variants = await svc.propose(TODAY)
    assert [v.code for v in variants] == ["A", "B", "C"]
    for v in variants:
        assert sum(v.weights.values()) == D(1)
        assert v.weights.get("dfa") == D("0.05")
        assert v.weights.get("gold", D(0)) == 0

    text, _ = variants_view(variants)
    assert "Варианты аллокации" in text and "не прогноз" in text

    c = next(v for v in variants if v.code == "C")
    diff = await svc.edit(c.weights, AllocationEdit(op="less_risk", value=D("0.05")), TODAY)
    assert sum(diff.after.values()) == D(1)
    assert diff.after["equity_ru"] < c.weights["equity_ru"]

    s = Session(draft=dict(diff.after), draft_metrics=diff.metrics_after, source="C")
    text, _ = draft_view(s, diff)
    assert "Черновик" in text and "п.п." in text

    ips = svc.approve(diff.after, "C")
    assert ips.version == 1
    loaded = load_ips(tmp_path / "ips.yaml")
    assert loaded.buckets["dfa"].cap is not None

    kinds = [r["kind"] for r in Journal(tmp_path / "journal.jsonl").records()]
    assert kinds == ["profile_answers", "ips_proposal", "ips_edit", "ips_version"]
    assert Journal(tmp_path / "journal.jsonl").verify_chain() == 4


async def test_propose_without_answers_fails_clearly(tmp_path: Path) -> None:
    with pytest.raises(LookupError):
        await service(tmp_path).propose(TODAY)
