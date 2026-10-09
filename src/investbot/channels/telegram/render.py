"""Рендер доменных объектов в HTML-сообщения Telegram.

Приватность (N4): в режиме `weights` в Telegram уходят только доли и изменения долей —
никаких рублёвых сумм и количеств (сообщения хранятся на серверах Telegram).
"""

from __future__ import annotations

from html import escape

from investbot.core.money import pct, pp, round_money
from investbot.rebalance.drift import BucketDrift
from investbot.services.portfolio import PortfolioOverview

BUCKET_TITLES = {
    "equity_ru": "Акции РФ",
    "ofz_fixed": "ОФЗ с фикс. купоном",
    "ofz_float_linker": "ОФЗ флоатеры и линкеры",
    "corp_ig": "Корп. облигации (≥ A-)",
    "corp_hy": "ВДО",
    "money_market": "Денежный рынок и ₽",
    "gold": "Золото",
    "dfa": "ЦФА",
    "fx_cash": "Валюта и валютные бумаги",
    "unclassified": "Без бакета",
}
STATUS_ICON = {"ok": "✅", "below": "🔻", "above": "🔺", "cap_breach": "⛔", "not_in_ips": "❔"}

DISCLAIMER = "<i>Не инвестиционная рекомендация. Решения принимаете вы.</i>"


def title(bucket: str) -> str:
    return BUCKET_TITLES.get(bucket, bucket)


def _drift_line(d: BucketDrift) -> str:
    icon = STATUS_ICON[d.status]
    if d.target is None:
        return f"{icon} {escape(title(d.bucket))}: {pct(d.weight)} — нет в IPS"
    corridor = f"{pct(d.low or d.target, 0)}–{pct(d.high or d.target, 0)}"
    return (f"{icon} {escape(title(d.bucket))}: {pct(d.weight)} "
            f"(цель {pct(d.target, 0)}, коридор {corridor}, {pp(d.deviation)})")


def portfolio_message(ov: PortfolioOverview, privacy_mode: str = "weights") -> str:
    v = ov.valuation
    lines = ["<b>📊 Портфель</b>"]
    lines.append(f"Счетов: {len(ov.snapshot.accounts)} · позиций: {len(ov.snapshot.positions)}")
    if privacy_mode == "full":
        lines.append(f"Оценка: {round_money(v.total_rub, 0):,} ₽".replace(",", " "))
    lines.append("")

    if ov.ips is None:
        lines.append("<b>Доли по классам</b> (IPS ещё не утверждён):")
        for bucket, w in sorted(v.bucket_weights.items(), key=lambda kv: -kv[1]):
            lines.append(f"• {escape(title(bucket))}: {pct(w)}")
        lines.append("")
        lines.append("Целевая аллокация не задана — бот работает в режиме «только мониторинг». "
                     "Конструктор аллокации: /allocation")
    else:
        lines.append(f"<b>Доли и коридоры IPS v{ov.ips.version}</b>")
        lines.extend(_drift_line(d) for d in ov.drift)
        off = [d for d in ov.drift if d.status != "ok"]
        lines.append("")
        lines.append("Есть выходы из коридора — план ребалансировки появится в недельном отчёте."
                     if off else "Все классы в коридорах — действий не требуется.")

    top = [p for p in v.positions if p.value_rub is not None][:8]
    if top:
        lines.append("")
        lines.append("<b>Крупнейшие позиции</b>")
        for p in top:
            src = "" if p.source == "broker" else " · вручную" if p.source == "manual" else " · ЦФА"
            lines.append(f"• {escape(p.ticker)} — {pct(p.weight)} ({escape(title(p.bucket))}{src})")

    if ov.warnings:
        lines.append("")
        lines.append("<b>⚠️ Внимание</b>")
        lines.extend(f"• {escape(w)}" for w in ov.warnings)
    lines.append("")
    lines.append(DISCLAIMER)
    return "\n".join(lines)
