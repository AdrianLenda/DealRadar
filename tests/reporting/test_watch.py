"""Tests for dealradar.reporting.watch -- watchlist CRUD, hit evaluation, and antispam (Zadanie 4). Owner: A6."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session

from conftest import make_deal, make_offer, make_product, make_source

from dealradar.models import Watch
from dealradar.reporting.watch import (
    ConsoleNotifier,
    FileNotifier,
    WatchHit,
    add_watch,
    evaluate_watches,
    get_watch,
    get_watch_any_alltime_low,
    list_watches,
    notify_watch_hits,
    remove_watch,
    resolve_query_watch_product_id,
    set_watch_active,
    should_notify,
    update_watch,
)


# --------------------------------------------------------------------------------------------------
# CRUD
# --------------------------------------------------------------------------------------------------


def test_add_list_get_remove_watch(db: Session) -> None:
    watch_id = add_watch(db, Watch(query="some product", max_price=60000))
    db.commit()
    assert get_watch(db, watch_id) is not None
    assert len(list_watches(db)) == 1
    assert remove_watch(db, watch_id) is True
    assert get_watch(db, watch_id) is None
    assert remove_watch(db, watch_id) is False  # already gone


def test_add_watch_with_query_variant_for_a_not_yet_existing_product(db: Session) -> None:
    watch_id = add_watch(db, Watch(query="Samsung 990 PRO 2TB", min_deal_score=70.0))
    db.commit()
    w = get_watch(db, watch_id)
    assert w is not None
    assert w.product_id is None
    assert w.query == "Samsung 990 PRO 2TB"


def test_any_alltime_low_flag_round_trips_via_the_companion_table(db: Session) -> None:
    watch_id = add_watch(db, Watch(query="product a"), any_alltime_low=True)
    db.commit()
    assert get_watch_any_alltime_low(db, watch_id) is True

    other_id = add_watch(db, Watch(query="product b"), any_alltime_low=False)
    db.commit()
    assert get_watch_any_alltime_low(db, other_id) is False


def test_update_watch_leaves_unspecified_fields_unchanged(db: Session) -> None:
    watch_id = add_watch(db, Watch(query="some product", max_price=60000, min_deal_score=50.0))
    db.commit()
    update_watch(db, watch_id, max_price=50000)
    db.commit()
    w = get_watch(db, watch_id)
    assert w is not None
    assert w.max_price == 50000
    assert w.min_deal_score == 50.0  # untouched


def test_set_watch_active_pause_resume(db: Session) -> None:
    watch_id = add_watch(db, Watch(query="some product", max_price=1))
    db.commit()
    assert set_watch_active(db, watch_id, False) is True
    db.commit()
    assert list_watches(db, active_only=True) == []
    assert set_watch_active(db, watch_id, True) is True
    db.commit()
    assert len(list_watches(db, active_only=True)) == 1


def test_resolve_query_watch_matches_existing_product_by_title(db: Session) -> None:
    product_id = make_product(db, "Samsung 990 PRO 2TB M.2 NVMe")
    db.commit()
    watch = Watch(query="990 PRO 2TB", max_price=1)
    resolved = resolve_query_watch_product_id(db, watch)
    assert resolved == product_id


def test_resolve_query_watch_returns_none_when_nothing_matches_yet(db: Session) -> None:
    watch = Watch(query="Nieistniejacy produkt xyz", max_price=1)
    assert resolve_query_watch_product_id(db, watch) is None


# --------------------------------------------------------------------------------------------------
# evaluate_watches
# --------------------------------------------------------------------------------------------------


def _seed_deal(db: Session, *, price_gross: int, deal_score: float, is_alltime_low: bool = False) -> int:
    source_id = make_source(db, "x-kom")
    product_id = make_product(db, "Watched Product")
    offer_id = make_offer(db, source_id, product_id, f"xk-{price_gross}", price_gross=price_gross, shipping_cost=0)
    make_deal(db, offer_id, deal_score=deal_score, is_alltime_low=is_alltime_low, n_competing_offers=2, n_sources=1)
    db.commit()
    return product_id


def test_max_price_condition_fires_when_price_at_or_below_threshold(db: Session) -> None:
    product_id = _seed_deal(db, price_gross=50000, deal_score=10.0)
    add_watch(db, Watch(product_id=product_id, max_price=60000))
    db.commit()
    hits = evaluate_watches(db)
    assert len(hits) == 1
    assert hits[0].reason == "max_price"


def test_max_price_condition_does_not_fire_above_threshold(db: Session) -> None:
    product_id = _seed_deal(db, price_gross=70000, deal_score=10.0)
    add_watch(db, Watch(product_id=product_id, max_price=60000))
    db.commit()
    assert evaluate_watches(db) == []


def test_min_deal_score_condition_fires(db: Session) -> None:
    product_id = _seed_deal(db, price_gross=90000, deal_score=88.0)
    add_watch(db, Watch(product_id=product_id, min_deal_score=80.0))
    db.commit()
    hits = evaluate_watches(db)
    assert len(hits) == 1
    assert hits[0].reason == "min_deal_score"


def test_any_alltime_low_condition_fires_only_when_flag_is_set(db: Session) -> None:
    product_id = _seed_deal(db, price_gross=90000, deal_score=10.0, is_alltime_low=True)
    watch_id = add_watch(db, Watch(product_id=product_id), any_alltime_low=True)
    db.commit()
    hits = evaluate_watches(db)
    assert len(hits) == 1
    assert hits[0].reason == "any_alltime_low"
    assert hits[0].watch_id == watch_id


def test_alltime_low_does_not_leak_into_watches_that_never_asked_for_it(db: Session) -> None:
    """A watch with only max_price set must not also silently fire on an unrelated all-time-low."""
    product_id = _seed_deal(db, price_gross=90000, deal_score=10.0, is_alltime_low=True)
    add_watch(db, Watch(product_id=product_id, max_price=1))  # price way below threshold, no alltime_low opt-in
    db.commit()
    assert evaluate_watches(db) == []


def test_watch_with_no_condition_never_fires(db: Session) -> None:
    product_id = _seed_deal(db, price_gross=1, deal_score=100.0, is_alltime_low=True)
    add_watch(db, Watch(product_id=product_id))
    db.commit()
    assert evaluate_watches(db) == []


def test_inactive_watch_is_never_evaluated(db: Session) -> None:
    product_id = _seed_deal(db, price_gross=1, deal_score=100.0)
    watch_id = add_watch(db, Watch(product_id=product_id, max_price=999999))
    set_watch_active(db, watch_id, False)
    db.commit()
    assert evaluate_watches(db) == []


# --------------------------------------------------------------------------------------------------
# Antispam (notification_log)
# --------------------------------------------------------------------------------------------------


def test_should_notify_true_on_first_hit(db: Session) -> None:
    assert should_notify(db, watch_id=1, product_id=1, price_total=50000) is True


def test_same_hit_twice_in_a_row_sends_exactly_one_notification(db: Session) -> None:
    product_id = _seed_deal(db, price_gross=50000, deal_score=10.0)
    watch_id = add_watch(db, Watch(product_id=product_id, max_price=60000))
    db.commit()

    hit = WatchHit(
        watch_id=watch_id, product_id=product_id, channel="console", reason="max_price",
        price_total=50000, deal_score=10.0, is_alltime_low=False, offer_id=1,
    )
    first = notify_watch_hits(db, [hit])
    db.commit()
    assert first.sent == 1

    second = notify_watch_hits(db, [hit])  # identical hit again, e.g. next scheduled run
    db.commit()
    assert second.sent == 0
    assert second.suppressed_antispam == 1


def test_repeat_hit_at_a_meaningfully_lower_price_is_sent_again(db: Session) -> None:
    product_id = _seed_deal(db, price_gross=50000, deal_score=10.0)
    watch_id = add_watch(db, Watch(product_id=product_id, max_price=60000))
    db.commit()

    hit1 = WatchHit(watch_id=watch_id, product_id=product_id, channel="console", reason="max_price", price_total=50000, deal_score=10.0, is_alltime_low=False, offer_id=1)
    notify_watch_hits(db, [hit1])
    db.commit()

    # A ~1% drop must NOT re-trigger (still within the noise band the antispam rule exists to suppress).
    hit_small_drop = WatchHit(watch_id=watch_id, product_id=product_id, channel="console", reason="max_price", price_total=49700, deal_score=10.0, is_alltime_low=False, offer_id=1)
    report_small = notify_watch_hits(db, [hit_small_drop])
    db.commit()
    assert report_small.sent == 0

    # A >=3% drop must re-trigger.
    hit_big_drop = WatchHit(watch_id=watch_id, product_id=product_id, channel="console", reason="max_price", price_total=48000, deal_score=10.0, is_alltime_low=False, offer_id=1)
    report_big = notify_watch_hits(db, [hit_big_drop])
    db.commit()
    assert report_big.sent == 1


def test_repeat_hit_at_a_higher_price_is_not_sent(db: Session) -> None:
    product_id = _seed_deal(db, price_gross=50000, deal_score=10.0)
    watch_id = add_watch(db, Watch(product_id=product_id, max_price=60000))
    db.commit()
    hit_low = WatchHit(watch_id=watch_id, product_id=product_id, channel="console", reason="max_price", price_total=40000, deal_score=10.0, is_alltime_low=False, offer_id=1)
    notify_watch_hits(db, [hit_low])
    db.commit()
    hit_higher = WatchHit(watch_id=watch_id, product_id=product_id, channel="console", reason="max_price", price_total=45000, deal_score=10.0, is_alltime_low=False, offer_id=1)
    report = notify_watch_hits(db, [hit_higher])
    assert report.sent == 0


def test_two_different_watches_on_the_same_product_are_independent(db: Session) -> None:
    product_id = _seed_deal(db, price_gross=50000, deal_score=10.0)
    watch_a = add_watch(db, Watch(product_id=product_id, max_price=60000))
    watch_b = add_watch(db, Watch(product_id=product_id, max_price=60000))
    db.commit()
    hit_a = WatchHit(watch_id=watch_a, product_id=product_id, channel="console", reason="max_price", price_total=50000, deal_score=10.0, is_alltime_low=False, offer_id=1)
    hit_b = WatchHit(watch_id=watch_b, product_id=product_id, channel="console", reason="max_price", price_total=50000, deal_score=10.0, is_alltime_low=False, offer_id=1)
    report = notify_watch_hits(db, [hit_a, hit_b])
    assert report.sent == 2


# --------------------------------------------------------------------------------------------------
# Notifier plugins
# --------------------------------------------------------------------------------------------------


def test_console_notifier_is_always_enabled_and_sends(capsys: object) -> None:
    notifier = ConsoleNotifier()
    assert notifier.enabled is True
    assert notifier.send("subject", "body") is True


def test_file_notifier_writes_a_json_line(tmp_path: Path) -> None:
    path = tmp_path / "notifications.log"
    notifier = FileNotifier(path)
    assert notifier.enabled is True
    assert notifier.send("Subject A", "Body A") is True
    content = path.read_text(encoding="utf-8")
    assert "Subject A" in content
    assert "Body A" in content


def test_smtp_notifier_disables_itself_when_unconfigured(monkeypatch: object) -> None:
    import dealradar.reporting.watch as watch_module

    monkeypatch.setattr(watch_module, "get_secret", lambda name: None)  # type: ignore[attr-defined]
    notifier = watch_module.SmtpNotifier()
    assert notifier.enabled is False
    assert notifier.send("s", "b") is False  # never raises


def test_telegram_notifier_disables_itself_when_unconfigured(monkeypatch: object) -> None:
    import dealradar.reporting.watch as watch_module

    monkeypatch.setattr(watch_module, "get_secret", lambda name: None)  # type: ignore[attr-defined]
    notifier = watch_module.TelegramNotifier()
    assert notifier.enabled is False
    assert notifier.send("s", "b") is False  # never raises, never calls the network


def test_notify_watch_hits_counts_disabled_channels_without_raising(db: Session, monkeypatch: object) -> None:
    import dealradar.reporting.watch as watch_module

    monkeypatch.setattr(watch_module, "get_secret", lambda name: None)  # type: ignore[attr-defined]
    product_id = _seed_deal(db, price_gross=50000, deal_score=10.0)
    watch_id = add_watch(db, Watch(product_id=product_id, max_price=60000, channel="smtp"))
    db.commit()
    hit = WatchHit(watch_id=watch_id, product_id=product_id, channel="smtp", reason="max_price", price_total=50000, deal_score=10.0, is_alltime_low=False, offer_id=1)
    report = notify_watch_hits(db, [hit])
    assert report.sent == 0
    assert report.channel_disabled == 1
