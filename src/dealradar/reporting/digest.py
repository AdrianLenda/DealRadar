"""The daily digest (Zadanie 3): one data model, two renderers (terminal/mail text, standalone HTML).

Structure, in this exact order (task spec, non-negotiable -- "Nigdy nie chowaj tego na dole"):

    1. UWAGI       -- system-health alarms (reporting.health), always first, even when empty.
    2. Top okazji  -- deal_score-ranked deals, each showing *why* it is a deal, not just that it is one.
    3. Watchlista  -- watch hits, independent of any deal_score threshold.
    4. Wskaznik sciemy -- the 5 shops with the worst claimed-vs-real discount gap.
    5. Stopka      -- run statistics (Z7) and duration.

Hard rule enforced throughout this module: every number displayed is either read verbatim from
`deal.evidence_json` / `quality_score.evidence_json` / already-stored `offer`/`price_point` columns, or a
trivial, transparent aggregation over those (min/count/percentage-of-two-stored-numbers) performed purely
for display. Nothing here recomputes a deal_score, a quality score, or a discount using its own formula --
see the module-level comments below each place that could be mistaken for one.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.orm import Session

from dealradar.models import RunReport
from dealradar.reporting.health import HealthAlert, check_health, latest_stage_reports
from dealradar.reporting.watch import WatchHit, evaluate_watches
from dealradar.scoring.history import price_series

DEFAULT_TOP_N = 20


# --------------------------------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class DealLine:
    """One top-deal row: everything needed to show *why* this is a deal, not just that it scored well."""

    offer_id: int
    product_id: int
    title: str
    shop: str
    price_total: int
    """grosze -- always price_total, per F3 (never a price without shipping)."""
    shipping_cost: int | None
    """grosze; None means unknown (offer.price_incomplete) -- rendered as "?", never as 0."""
    deal_score: float
    basis: Literal["history", "cross_shop"]
    median_90d: int | None
    """grosze; deal.evidence["med"], rounded for display. None for a cross_shop (cold-start) basis."""
    alltime_low_price: int | None
    """grosze; min observed price_total across the product's history (price_series), for display next to
    is_alltime_low -- see _alltime_low_price's docstring for why this one number is computed here."""
    is_alltime_low: bool
    n_competing_offers: int
    n_sources: int
    n_days_history: int
    quality_total_1_10: float | None
    quality_missing_reason: str | None
    peer_group_size: int | None
    peer_band_low: int | None
    """grosze; peer group's price band lower bound, from quality_score.evidence["peer_group"]."""
    peer_band_high: int | None
    claimed_discount_pct: float | None
    """Only set when basis=="history" AND the shop published a list_price_claimed (task spec's exact gate:
    "pokazuj wtedy i tylko wtedy, gdy istnieje realna historia")."""
    real_discount_pct: float | None
    """= 100 * deal.depth_vs_median_90d, already computed and stored by A4 -- read verbatim, not recomputed."""
    warranty_months: int | None


@dataclass(frozen=True)
class WatchHitLine:
    """One watchlist hit, enriched with the product title for display."""

    hit: WatchHit
    title: str


@dataclass(frozen=True)
class BullshitLine:
    """One row of the "wskaznik sciemy" table: a shop's claimed-vs-real discount gap."""

    source_name: str
    bullshit_index: float | None
    claimed_discount_avg: float | None
    real_discount_avg: float | None
    n_offers: int


@dataclass(frozen=True)
class Digest:
    """Everything the text/HTML renderers need, already assembled from the database. Nothing in here is
    computed by this module beyond simple display aggregation -- see this module's docstring."""

    generated_at: datetime
    alerts: list[HealthAlert]
    deals: list[DealLine]
    watch_hits: list[WatchHitLine]
    bullshit: list[BullshitLine]
    footer_reports: dict[str, RunReport]
    top_n: int


# --------------------------------------------------------------------------------------------------
# Assembly (Zadanie 3's "kazda liczba ma byc sprawdzalna" -- every field below traces to a DB column)
# --------------------------------------------------------------------------------------------------


def _latest_deals(session: Session, *, limit: int) -> list[dict[str, Any]]:
    """Returns the best (highest deal_score, cheapest tiebreak) currently-latest deal row per product,
    joined with its offer/source, ordered by deal_score descending, capped at `limit` distinct products.

    "Latest" = the deal row with the highest id for that offer (score_deals may have run more than once).
    Deduping to one row per product (rather than per offer) is what lets each digest line represent "this
    product", matching the example format in the task spec (one line, one shop, one price).
    """
    rows = session.execute(
        text(
            """
            SELECT d.offer_id, d.deal_score, d.basis, d.depth_vs_median_90d, d.is_alltime_low,
                   d.n_competing_offers, d.n_days_history, d.evidence_json,
                   o.product_id, o.title, o.price_total, o.shipping_cost, o.price_incomplete,
                   o.list_price_claimed, o.warranty_months, s.name AS shop_name,
                   p.canonical_title
            FROM deal d
            JOIN offer o ON o.id = d.offer_id
            JOIN source s ON s.id = o.source_id
            JOIN product p ON p.id = o.product_id
            WHERE d.id = (SELECT MAX(d2.id) FROM deal d2 WHERE d2.offer_id = d.offer_id)
              AND o.price_incomplete = 0
            ORDER BY d.deal_score DESC, o.price_total ASC
            """
        )
    ).mappings().all()

    best_per_product: dict[int, dict[str, Any]] = {}
    for row in rows:
        product_id = int(row["product_id"])
        if product_id in best_per_product:
            continue  # rows are deal_score-sorted; the first occurrence of a product is already its best
        best_per_product[product_id] = dict(row)
        if len(best_per_product) >= limit:
            break
    return list(best_per_product.values())


def _latest_quality(session: Session, product_id: int) -> dict[str, Any] | None:
    """Returns the most recent quality_score row for product_id, or None if it was never scored."""
    row = session.execute(
        text(
            "SELECT total_1_10, missing_reason, evidence_json FROM quality_score "
            "WHERE product_id = :pid ORDER BY id DESC LIMIT 1"
        ),
        {"pid": product_id},
    ).mappings().first()
    return dict(row) if row is not None else None


def _alltime_low_price(session: Session, product_id: int) -> int | None:
    """Returns the lowest observed price_total (grosze) across product_id's entire recorded price history.

    Reuses `scoring.history.price_series` (A4's own read-only accessor over price_point) and takes a plain
    min() purely to *display* the number behind `deal.is_alltime_low` (a bool `deal` already computes and
    stores, but not the underlying price value) -- this is display aggregation over already-persisted
    observations, not a new pricing/scoring algorithm. Returns None if there is no history yet.
    """
    points = price_series(session, product_id, days=3650)
    prices = [p.price_total for p in points if p.in_stock]
    return min(prices) if prices else None


def _round_half_up(value: float) -> int:
    """Rounds a float to the nearest int using round-half-up, for display of an already-computed grosze value."""
    return int(math.floor(value + 0.5)) if value >= 0 else -int(math.floor(-value + 0.5))


def _build_deal_line(session: Session, row: dict[str, Any]) -> DealLine:
    """Converts one `_latest_deals` row (+ a quality_score lookup) into a fully-populated DealLine."""
    evidence = json.loads(row["evidence_json"]) if row["evidence_json"] else {}
    basis: Literal["history", "cross_shop"] = row["basis"]
    product_id = int(row["product_id"])

    median_90d = _round_half_up(float(evidence["med"])) if basis == "history" and evidence.get("med") is not None else None

    claimed_discount_pct: float | None = None
    if basis == "history" and row["list_price_claimed"]:
        # Same formula A4's score_deals uses internally for the identical purpose (see scoring/deal.py's
        # shop_credibility block) -- reproduced here only for *display* of this one offer's own claimed
        # discount, gated on the exact same "real history exists" condition the task spec requires.
        claimed_discount_pct = 100.0 * (1.0 - row["price_total"] / row["list_price_claimed"])

    real_discount_pct = 100.0 * row["depth_vs_median_90d"] if row["depth_vs_median_90d"] is not None else None

    quality = _latest_quality(session, product_id)
    quality_total: float | None = None
    quality_missing: str | None = None
    peer_size: int | None = None
    peer_low: int | None = None
    peer_high: int | None = None
    if quality is not None:
        quality_total = quality["total_1_10"]
        quality_missing = quality["missing_reason"]
        q_evidence = json.loads(quality["evidence_json"]) if quality["evidence_json"] else {}
        peer_group = q_evidence.get("peer_group")
        if peer_group:
            members = peer_group.get("members") or []
            peer_size = len(members)
            band = peer_group.get("band")
            ref_price = peer_group.get("reference_price")
            if band is not None and ref_price is not None:
                peer_low = _round_half_up(ref_price * (1 - band))
                peer_high = _round_half_up(ref_price * (1 + band))
    else:
        quality_missing = "brak oceny: nie mam formuly dla tej kategorii lub brak wystarczajacych danych"

    return DealLine(
        offer_id=int(row["offer_id"]),
        product_id=product_id,
        title=row["canonical_title"] or row["title"],
        shop=row["shop_name"],
        price_total=int(row["price_total"]),
        shipping_cost=None if row["shipping_cost"] is None else int(row["shipping_cost"]),
        deal_score=float(row["deal_score"]),
        basis=basis,
        median_90d=median_90d,
        alltime_low_price=_alltime_low_price(session, product_id),
        is_alltime_low=bool(row["is_alltime_low"]),
        n_competing_offers=int(row["n_competing_offers"]),
        n_sources=int(evidence.get("n_sources", 0)),
        n_days_history=int(row["n_days_history"]),
        quality_total_1_10=quality_total,
        quality_missing_reason=quality_missing,
        peer_group_size=peer_size,
        peer_band_low=peer_low,
        peer_band_high=peer_high,
        claimed_discount_pct=claimed_discount_pct,
        real_discount_pct=real_discount_pct,
        warranty_months=row["warranty_months"],
    )


def _bullshit_lines(session: Session, *, limit: int = 5) -> list[BullshitLine]:
    """Returns the `limit` sources with the worst (highest) bullshit_index, most recent period per source."""
    rows = session.execute(
        text(
            """
            SELECT sc.source_id, sc.bullshit_index, sc.claimed_discount_avg, sc.real_discount_avg,
                   sc.n_offers, s.name AS source_name
            FROM shop_credibility sc
            JOIN source s ON s.id = sc.source_id
            WHERE sc.period = (SELECT MAX(sc2.period) FROM shop_credibility sc2 WHERE sc2.source_id = sc.source_id)
            ORDER BY sc.bullshit_index DESC NULLS LAST
            LIMIT :limit
            """
        ),
        {"limit": limit},
    ).mappings().all()
    return [
        BullshitLine(
            source_name=r["source_name"],
            bullshit_index=r["bullshit_index"],
            claimed_discount_avg=r["claimed_discount_avg"],
            real_discount_avg=r["real_discount_avg"],
            n_offers=int(r["n_offers"]),
        )
        for r in rows
    ]


def _product_title(session: Session, product_id: int) -> str:
    """Returns product_id's canonical_title, or a placeholder if the product no longer exists."""
    row = session.execute(
        text("SELECT canonical_title FROM product WHERE id = :id"), {"id": product_id}
    ).scalar_one_or_none()
    return str(row) if row is not None else f"product #{product_id} (usuniety)"


def build_digest(session: Session, *, top_n: int = DEFAULT_TOP_N) -> Digest:
    """Assembles a Digest entirely from already-computed database state: run_log (health), `deal` +
    `quality_score` + `offer` (top deals), `watch` + `deal` (watchlist hits), and `shop_credibility`
    (bullshit index). Never calls any scoring function -- see module docstring.
    """
    footer_reports = latest_stage_reports(session)
    alerts = check_health(session, footer_reports)

    deal_rows = _latest_deals(session, limit=top_n)
    deals = [_build_deal_line(session, row) for row in deal_rows]

    hits = evaluate_watches(session)
    watch_hits = [WatchHitLine(hit=hit, title=_product_title(session, hit.product_id)) for hit in hits]

    bullshit = _bullshit_lines(session)

    return Digest(
        generated_at=datetime.now(timezone.utc),
        alerts=alerts,
        deals=deals,
        watch_hits=watch_hits,
        bullshit=bullshit,
        footer_reports=footer_reports,
        top_n=top_n,
    )


# --------------------------------------------------------------------------------------------------
# Formatting helpers shared by both renderers
# --------------------------------------------------------------------------------------------------


def _fmt_pln(grosze: int | None) -> str:
    """Formats a grosze amount as Polish-style zloty text ("649,00 zl"), or a placeholder when None (Z4)."""
    if grosze is None:
        return "brak danych"
    zloty, gr = divmod(grosze, 100)
    return f"{zloty:,}".replace(",", " ") + f",{gr:02d} zl"


def _fmt_pct(value: float | None, *, signed: bool = False) -> str:
    """Formats a percentage value to one decimal place, or a placeholder when None (Z4)."""
    if value is None:
        return "n/d"
    return f"{value:+.1f}%" if signed else f"{value:.1f}%"


def _fmt_score(value: float | None) -> str:
    """Formats a 0-10 quality score to one decimal place using a comma (Polish convention), or a placeholder."""
    if value is None:
        return "n/d"
    return f"{value:.1f}".replace(".", ",")


_REASON_LABELS_PL: dict[str, str] = {
    "no_utility_formula": "brak formuly uzytecznosci dla tej kategorii",
    "peer_group_too_small": "za malo porownywalnych produktow w tym samym przedziale cenowym",
    "insufficient_signals": "za malo danych (mniej niz 2 z 4 skladowych oceny)",
}


def _missing_reason_pl(reason: str | None) -> str:
    """Translates a quality_score.missing_reason code into the human-readable Polish sentence the digest shows."""
    if reason is None:
        return "brak danych"
    return _REASON_LABELS_PL.get(reason, reason)


# --------------------------------------------------------------------------------------------------
# Text renderer (terminal / mail)
# --------------------------------------------------------------------------------------------------


def _render_deal_line_text(d: DealLine) -> list[str]:
    """Returns the multi-line plain-text block for one DealLine, in the task spec's tree-drawing format."""
    lines: list[str] = []
    header = f"{d.deal_score:5.0f}  {d.title:<50s} {_fmt_pln(d.price_total):>14s}"
    lines.append(header)

    if d.basis == "cross_shop":
        lines.append("     |- BEZ HISTORII -- porownanie tylko miedzysklepowe (cold start)")
    elif d.median_90d is not None:
        low_line = f"     |- mediana 90 dni: {_fmt_pln(d.median_90d)}"
        if d.alltime_low_price is not None:
            low_line += f"  * minimum historyczne: {_fmt_pln(d.alltime_low_price)}"
            if d.is_alltime_low:
                low_line += " <- NOWE MINIMUM"
        lines.append(low_line)

    cheapest = "najtansza dzisiaj" if d.n_competing_offers and d.price_total else "oferta"
    lines.append(
        f"     |- {cheapest} z {d.n_competing_offers} ofert w {d.n_sources} sklepach"
        f"  * historia: {d.n_days_history} dni"
    )

    if d.quality_total_1_10 is not None:
        band = ""
        if d.peer_group_size is not None and d.peer_band_low is not None and d.peer_band_high is not None:
            band = f" ({d.peer_group_size} konkurentow w pasmie {_fmt_pln(d.peer_band_low)}-{_fmt_pln(d.peer_band_high)})"
        lines.append(f"     |- jakosc/cena: {_fmt_score(d.quality_total_1_10)}/10{band}")
    else:
        lines.append(f"     |- jakosc/cena: brak oceny: {_missing_reason_pl(d.quality_missing_reason)}")

    if d.claimed_discount_pct is not None and d.real_discount_pct is not None:
        lines.append(
            f"     |- sklep deklaruje {_fmt_pct(-d.claimed_discount_pct, signed=True)}, "
            f"realnie {_fmt_pct(-d.real_discount_pct, signed=True)}"
        )

    warranty = f"gwarancja {d.warranty_months} mc" if d.warranty_months is not None else "gwarancja: brak danych"
    shipping = _fmt_pln(d.shipping_cost) if d.shipping_cost is not None else "brak danych"
    lines.append(f"     `- {d.shop} * {warranty} * dostawa {shipping}")
    return lines


def render_text(digest: Digest) -> str:
    """Renders `digest` as plain text for the terminal or a plain-text email body (Zadanie 3)."""
    out: list[str] = []
    out.append(f"=== DealRadar -- raport dzienny ({digest.generated_at.isoformat(timespec='minutes')}) ===")
    out.append("")

    out.append(f"## UWAGI ({len(digest.alerts)})")
    if not digest.alerts:
        out.append("Brak alarmow -- wskazniki w normie wzgledem ostatnich przebiegow.")
    else:
        for alert in digest.alerts:
            src = f" source={alert.source_name}" if alert.source_name else ""
            out.append(f"- [{alert.kind}]{src} {alert.detail}")
    out.append("")

    out.append(f"## Top okazji ({len(digest.deals)}/{digest.top_n})")
    if not digest.deals:
        out.append("Brak ofert spelniajacych kryteria okazji w tym przebiegu.")
    else:
        for d in digest.deals:
            out.extend(_render_deal_line_text(d))
    out.append("")

    out.append(f"## Watchlista ({len(digest.watch_hits)} trafien)")
    if not digest.watch_hits:
        out.append("Brak trafien na obserwowanych produktach.")
    else:
        for wh in digest.watch_hits:
            out.append(
                f"- watch #{wh.hit.watch_id} [{wh.hit.reason}]: {wh.title} @ {_fmt_pln(wh.hit.price_total)}"
                f" (deal_score={wh.hit.deal_score if wh.hit.deal_score is not None else 'n/d'})"
            )
    out.append("")

    out.append("## Wskaznik sciemy (top 5 sklepow)")
    if not digest.bullshit:
        out.append("Brak wystarczajacych danych (potrzebna realna historia cen u danego sklepu).")
    else:
        for i, b in enumerate(digest.bullshit, start=1):
            idx_str = f"{b.bullshit_index * 100:+.1f}pp" if b.bullshit_index is not None else "n/d (za mala probka)"
            out.append(
                f"{i}. {b.source_name}: bullshit_index={idx_str} "
                f"(deklarowane {_fmt_pct(b.claimed_discount_avg * 100 if b.claimed_discount_avg is not None else None, signed=True)}, "
                f"realne {_fmt_pct(b.real_discount_avg * 100 if b.real_discount_avg is not None else None, signed=True)}, "
                f"n={b.n_offers})"
            )
    out.append("")

    out.append("## Stopka -- statystyki przebiegu")
    if not digest.footer_reports:
        out.append("Brak zapisanych przebiegow (run_log jest pusty).")
    else:
        for stage in ("collect", "normalize", "match", "score_deals", "score_quality"):
            report = digest.footer_reports.get(stage)
            if report is not None:
                out.append(report.summary_line())
    out.append(f"wygenerowano: {digest.generated_at.isoformat()}")

    return "\n".join(out)


# --------------------------------------------------------------------------------------------------
# HTML renderer -- standalone, no external resources, readable in light and dark mode
# --------------------------------------------------------------------------------------------------


def _html_escape(value: str) -> str:
    """Escapes the five HTML-significant characters in `value`; used for every piece of DB-sourced text."""
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


_HTML_STYLE = """
:root { color-scheme: light dark; }
body {
  font-family: -apple-system, Segoe UI, Roboto, Arial, sans-serif;
  max-width: 900px; margin: 0 auto; padding: 1.5rem;
  background: #ffffff; color: #1a1a1a;
}
@media (prefers-color-scheme: dark) {
  body { background: #16181d; color: #e6e6e6; }
  .deal { background: #20232b; border-color: #333; }
  .badge-cold { background: #4a3a12; color: #ffd27a; }
  .alert { background: #3a1f1f; border-color: #8a3a3a; color: #ffb3b3; }
  a { color: #7fb3ff; }
  table { border-color: #333; }
  th, td { border-color: #333; }
  details summary { color: #9fb8ff; }
}
h1 { font-size: 1.4rem; }
h2 { font-size: 1.1rem; border-bottom: 1px solid currentColor; padding-bottom: 0.25rem; margin-top: 2rem; }
.alert { background: #ffecec; border: 1px solid #d98a8a; color: #7a1f1f; padding: 0.5rem 0.75rem; border-radius: 6px; margin: 0.4rem 0; }
.deal { border: 1px solid #ddd; border-radius: 8px; padding: 0.75rem 1rem; margin: 0.6rem 0; background: #fafafa; }
.deal-head { display: flex; justify-content: space-between; align-items: baseline; gap: 1rem; }
.deal-score { font-weight: 700; font-size: 1.3rem; min-width: 3rem; }
.deal-title { flex: 1; font-weight: 600; }
.deal-price { font-weight: 700; white-space: nowrap; }
.deal ul { margin: 0.4rem 0 0 0; padding-left: 1.2rem; }
.deal li { margin: 0.15rem 0; }
.badge-cold { display: inline-block; background: #fff3cd; color: #7a5b00; border-radius: 4px; padding: 0.05rem 0.4rem; font-size: 0.8rem; margin-left: 0.5rem; }
.muted { opacity: 0.7; font-size: 0.9rem; }
table { border-collapse: collapse; width: 100%; margin: 0.5rem 0; }
th, td { border: 1px solid #ccc; padding: 0.35rem 0.6rem; text-align: left; font-size: 0.92rem; }
details summary { cursor: pointer; color: #3355aa; font-size: 0.85rem; margin-top: 0.3rem; }
pre.evidence { white-space: pre-wrap; font-size: 0.78rem; background: rgba(127,127,127,0.12); padding: 0.5rem; border-radius: 6px; }
footer { margin-top: 2rem; font-size: 0.85rem; opacity: 0.75; }
"""


def _render_deal_html(session: Session, d: DealLine) -> str:
    """Renders one DealLine as an HTML card, with a collapsible <details> showing the raw evidence JSON."""
    cold_badge = '<span class="badge-cold">bez historii -- porownanie tylko miedzysklepowe</span>' if d.basis == "cross_shop" else ""
    parts: list[str] = [f'<div class="deal">']
    parts.append(
        f'<div class="deal-head"><span class="deal-score">{d.deal_score:.0f}</span>'
        f'<span class="deal-title">{_html_escape(d.title)}{cold_badge}</span>'
        f'<span class="deal-price">{_html_escape(_fmt_pln(d.price_total))}</span></div>'
    )
    items: list[str] = []
    if d.basis == "history" and d.median_90d is not None:
        low = f"mediana 90 dni: {_fmt_pln(d.median_90d)}"
        if d.alltime_low_price is not None:
            low += f" &middot; minimum historyczne: {_fmt_pln(d.alltime_low_price)}"
            if d.is_alltime_low:
                low += " <strong>&larr; nowe minimum</strong>"
        items.append(low)
    items.append(f"najtansza z {d.n_competing_offers} ofert w {d.n_sources} sklepach &middot; historia: {d.n_days_history} dni")
    if d.quality_total_1_10 is not None:
        band = ""
        if d.peer_group_size is not None and d.peer_band_low is not None and d.peer_band_high is not None:
            band = f" ({d.peer_group_size} konkurentow w pasmie {_fmt_pln(d.peer_band_low)}-{_fmt_pln(d.peer_band_high)})"
        items.append(f"jakosc/cena: {_fmt_score(d.quality_total_1_10)}/10{band}")
    else:
        items.append(f"jakosc/cena: <em>brak oceny: {_html_escape(_missing_reason_pl(d.quality_missing_reason))}</em>")
    if d.claimed_discount_pct is not None and d.real_discount_pct is not None:
        items.append(
            f"sklep deklaruje {_fmt_pct(-d.claimed_discount_pct, signed=True)}, realnie {_fmt_pct(-d.real_discount_pct, signed=True)}"
        )
    warranty = f"gwarancja {d.warranty_months} mc" if d.warranty_months is not None else "gwarancja: brak danych"
    shipping = _fmt_pln(d.shipping_cost) if d.shipping_cost is not None else "brak danych"
    items.append(f"{_html_escape(d.shop)} &middot; {warranty} &middot; dostawa {shipping}")
    parts.append("<ul>" + "".join(f"<li>{i}</li>" for i in items) + "</ul>")

    evidence = session.execute(
        text("SELECT evidence_json FROM deal WHERE offer_id = :oid ORDER BY id DESC LIMIT 1"), {"oid": d.offer_id}
    ).scalar_one_or_none()
    if evidence:
        pretty = json.dumps(json.loads(evidence), indent=2, ensure_ascii=False, sort_keys=True)
        parts.append(f"<details><summary>dowody (deal.evidence)</summary><pre class=\"evidence\">{_html_escape(pretty)}</pre></details>")
    parts.append("</div>")
    return "".join(parts)


def render_html(digest: Digest, session: Session) -> str:
    """Renders `digest` as a standalone HTML document (no external resources), readable in light/dark mode.

    Takes `session` (unlike render_text) so each deal's raw evidence_json can be embedded behind a
    <details> disclosure -- the task spec's "raport HTML ma je pokazywac po rozwinieciu" requirement.
    """
    parts: list[str] = [
        "<!doctype html>",
        '<html lang="pl"><head><meta charset="utf-8">',
        "<title>DealRadar -- raport dzienny</title>",
        f"<style>{_HTML_STYLE}</style></head><body>",
        f"<h1>DealRadar -- raport dzienny</h1><p class=\"muted\">wygenerowano: {_html_escape(digest.generated_at.isoformat())}</p>",
    ]

    parts.append(f"<h2>UWAGI ({len(digest.alerts)})</h2>")
    if not digest.alerts:
        parts.append('<p class="muted">Brak alarmow -- wskazniki w normie wzgledem ostatnich przebiegow.</p>')
    else:
        for alert in digest.alerts:
            src = f" zrodlo={_html_escape(alert.source_name)}" if alert.source_name else ""
            parts.append(f'<div class="alert"><strong>[{alert.kind}]</strong>{src} {_html_escape(alert.detail)}</div>')

    parts.append(f"<h2>Top okazji ({len(digest.deals)}/{digest.top_n})</h2>")
    if not digest.deals:
        parts.append('<p class="muted">Brak ofert spelniajacych kryteria okazji w tym przebiegu.</p>')
    else:
        for d in digest.deals:
            parts.append(_render_deal_html(session, d))

    parts.append(f"<h2>Watchlista ({len(digest.watch_hits)} trafien)</h2>")
    if not digest.watch_hits:
        parts.append('<p class="muted">Brak trafien na obserwowanych produktach.</p>')
    else:
        parts.append("<table><tr><th>watch</th><th>produkt</th><th>powod</th><th>cena</th><th>deal_score</th></tr>")
        for wh in digest.watch_hits:
            ds = f"{wh.hit.deal_score:.1f}" if wh.hit.deal_score is not None else "n/d"
            parts.append(
                f"<tr><td>#{wh.hit.watch_id}</td><td>{_html_escape(wh.title)}</td><td>{_html_escape(wh.hit.reason)}</td>"
                f"<td>{_html_escape(_fmt_pln(wh.hit.price_total))}</td><td>{ds}</td></tr>"
            )
        parts.append("</table>")

    parts.append("<h2>Wskaznik sciemy (top 5 sklepow)</h2>")
    if not digest.bullshit:
        parts.append('<p class="muted">Brak wystarczajacych danych.</p>')
    else:
        parts.append("<table><tr><th>sklep</th><th>bullshit_index</th><th>deklarowane</th><th>realne</th><th>n</th></tr>")
        for b in digest.bullshit:
            idx_str = f"{b.bullshit_index * 100:+.1f}pp" if b.bullshit_index is not None else "n/d"
            claimed = _fmt_pct(b.claimed_discount_avg * 100 if b.claimed_discount_avg is not None else None, signed=True)
            real = _fmt_pct(b.real_discount_avg * 100 if b.real_discount_avg is not None else None, signed=True)
            parts.append(
                f"<tr><td>{_html_escape(b.source_name)}</td><td>{idx_str}</td><td>{claimed}</td><td>{real}</td>"
                f"<td>{b.n_offers}</td></tr>"
            )
        parts.append("</table>")

    parts.append("<footer><h2>Stopka -- statystyki przebiegu</h2><ul>")
    if not digest.footer_reports:
        parts.append("<li>Brak zapisanych przebiegow (run_log jest pusty).</li>")
    else:
        for stage in ("collect", "normalize", "match", "score_deals", "score_quality"):
            report = digest.footer_reports.get(stage)
            if report is not None:
                parts.append(f"<li>{_html_escape(report.summary_line())}</li>")
    parts.append("</ul></footer>")

    parts.append("</body></html>")
    return "\n".join(parts)
