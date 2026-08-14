"""Unit tests for the regex-based numeric attribute extractor that feeds gate G2, and its
reconciliation with A2's `offer.evidence["attrs"]` (see attrs.py's module docstring)."""

from __future__ import annotations

from dealradar.matching.attrs import attrs_for_offer, extract_numeric_attrs, parse_evidence_attrs


def test_capacity_gb_from_gb() -> None:
    assert extract_numeric_attrs("dysk ssd 512gb m.2")["capacity_gb"] == 512.0


def test_capacity_gb_from_tb_converts() -> None:
    assert extract_numeric_attrs("dysk ssd 1tb m.2")["capacity_gb"] == 1024.0


def test_capacity_gb_absent_when_no_match() -> None:
    assert "capacity_gb" not in extract_numeric_attrs("mysz bezprzewodowa logitech")


def test_capacity_wh_and_mah() -> None:
    attrs = extract_numeric_attrs("powerbank 20000mah 74wh")
    assert attrs["capacity_mah"] == 20000.0
    assert attrs["capacity_wh"] == 74.0


def test_refresh_hz_extracted_for_plausible_monitor_values() -> None:
    assert extract_numeric_attrs("monitor lg 27gp850 165hz")["refresh_hz"] == 165.0


def test_refresh_hz_not_extracted_for_mains_frequency() -> None:
    # 50/60Hz almost always means mains frequency (a charger), not a monitor refresh rate.
    attrs = extract_numeric_attrs("zasilacz 65w 50hz 60hz")
    assert "refresh_hz" not in attrs


def test_resolution_extracted() -> None:
    attrs = extract_numeric_attrs("monitor 2560x1440 165hz")
    assert attrs["resolution_w"] == 2560.0
    assert attrs["resolution_h"] == 1440.0


def test_diagonal_in_extracted() -> None:
    assert extract_numeric_attrs('monitor 27" ips')["diagonal_in"] == 27.0


def test_voltage_extracted_in_plausible_range() -> None:
    attrs = extract_numeric_attrs("zasilacz dell 65w eu wtyczka 230v")
    assert attrs["voltage_v"] == 230.0


def test_quantity_from_pak_word() -> None:
    assert extract_numeric_attrs("baterie duracell aa 2-pak")["quantity"] == 2.0


def test_quantity_from_dwupak_word() -> None:
    assert extract_numeric_attrs("baterie duracell aaa dwupak")["quantity"] == 2.0


def test_model_number_distinguishes_iphone_generations() -> None:
    a = extract_numeric_attrs("apple iphone 15 128gb black")
    b = extract_numeric_attrs("apple iphone 13 128gb")
    assert a["model_number"] == 15.0
    assert b["model_number"] == 13.0


def test_model_number_matches_across_sources_for_same_generation() -> None:
    a = extract_numeric_attrs("apple iphone 15 128gb black")
    b = extract_numeric_attrs("iphone 15 128gb czarny apple")
    assert a["model_number"] == b["model_number"] == 15.0


def test_model_number_not_extracted_when_no_known_line_present() -> None:
    assert "model_number" not in extract_numeric_attrs("dysk ssd samsung 990 pro 512gb m.2 nvme")


def test_battery_size_distinguishes_aa_from_aaa() -> None:
    a = extract_numeric_attrs("baterie duracell aa")
    b = extract_numeric_attrs("baterie duracell aaa")
    assert a["battery_size"] != b["battery_size"]


def test_battery_size_not_extracted_for_unrelated_titles() -> None:
    assert "battery_size" not in extract_numeric_attrs("mysz logitech mx master 3s graphite")


def test_none_title_norm_returns_empty() -> None:
    assert extract_numeric_attrs(None) == {}


def test_empty_title_norm_returns_empty() -> None:
    assert extract_numeric_attrs("") == {}


# --------------------------------------------------------------------------------------------------
# A2 integration: offer.evidence["attrs"] (see the A3 final report's reconciliation note)
# --------------------------------------------------------------------------------------------------


def test_parse_evidence_attrs_reads_a2_shape() -> None:
    evidence = {"attrs": [{"key": "capacity_gb", "value_num": 512.0, "unit": "GB", "extracted_from": "feed_field"}]}
    assert parse_evidence_attrs(evidence) == {"capacity_gb": 512.0}


def test_parse_evidence_attrs_skips_null_value_num() -> None:
    evidence = {"attrs": [{"key": "capacity_gb", "value_num": None, "unit": None, "extracted_from": None}]}
    assert parse_evidence_attrs(evidence) == {}


def test_parse_evidence_attrs_handles_missing_or_malformed_evidence() -> None:
    assert parse_evidence_attrs(None) == {}
    assert parse_evidence_attrs({}) == {}
    assert parse_evidence_attrs({"attrs": "not-a-list"}) == {}
    assert parse_evidence_attrs({"attrs": ["not-a-dict"]}) == {}


def test_attrs_for_offer_a2_evidence_wins_on_overlapping_key() -> None:
    # title_norm regex would say 512 (a plausible extraction), but A2 saw the authoritative
    # feed_field value of 1024 -- A2 must win.
    evidence = {"attrs": [{"key": "capacity_gb", "value_num": 1024.0, "unit": "GB", "extracted_from": "feed_field"}]}
    merged = attrs_for_offer("dysk ssd 512gb m.2", evidence)
    assert merged["capacity_gb"] == 1024.0


def test_attrs_for_offer_keeps_own_extraction_for_keys_a2_does_not_provide() -> None:
    # voltage_v is outside A2's documented extraction set (A2-normalizator.md section 6) -- this
    # module's own regex is the only source for it.
    evidence = {"attrs": [{"key": "capacity_gb", "value_num": 1024.0, "unit": "GB", "extracted_from": "title"}]}
    merged = attrs_for_offer("zasilacz dell 65w eu wtyczka 230v", evidence)
    assert merged["voltage_v"] == 230.0
    assert merged["power_w"] == 65.0
