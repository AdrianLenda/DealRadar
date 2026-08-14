"""Tests for dealradar.normalize.attrs -- regex attribute extraction, including the ambiguity traps (A2 spec, sect. 6)."""

from __future__ import annotations

from dealradar.normalize.attrs import extract_attrs


def _by_key(attrs: list, key: str):  # type: ignore[no-untyped-def]
    return next((a for a in attrs if a.key == key), None)


# --------------------------------------------------------------------------------------------------
# Storage capacity.
# --------------------------------------------------------------------------------------------------


def test_capacity_gb_direct() -> None:
    attrs = extract_attrs(title="Dysk SSD Kingston NV2 512GB M.2 NVMe")
    cap = _by_key(attrs, "capacity_gb")
    assert cap is not None and cap.value_num == 512.0 and cap.unit == "GB" and cap.extracted_from == "title"


def test_capacity_tb_converted_to_gb() -> None:
    attrs = extract_attrs(title="Dysk SSD Samsung 990 PRO 1TB M.2 NVMe")
    cap = _by_key(attrs, "capacity_gb")
    assert cap is not None and cap.value_num == 1000.0


def test_capacity_1tb_with_space() -> None:
    attrs = extract_attrs(title="Dysk SSD WD Blue 1 TB SATA")
    cap = _by_key(attrs, "capacity_gb")
    assert cap is not None and cap.value_num == 1000.0


def test_capacity_2000gb() -> None:
    attrs = extract_attrs(title="Dysk SSD Crucial 2000GB M.2")
    cap = _by_key(attrs, "capacity_gb")
    assert cap is not None and cap.value_num == 2000.0


def test_ram_storage_shorthand_extracts_both_correctly() -> None:
    """"8/256GB" is RAM/storage phone shorthand: ram_gb=8, capacity_gb=256 -- never confuse the two (F2 risk)."""
    attrs = extract_attrs(title="Xiaomi Redmi Note 13 8/256GB")
    ram = _by_key(attrs, "ram_gb")
    storage = _by_key(attrs, "capacity_gb")
    assert ram is not None and ram.value_num == 8.0
    assert storage is not None and storage.value_num == 256.0


def test_ambiguous_slash_gb_pattern_outside_ram_range_extracts_nothing() -> None:
    """"256/512GB" (two storage *options*, not RAM/storage) is ambiguous -- extract neither rather than guess."""
    attrs = extract_attrs(title="Smartfon XYZ 256/512GB dostępne warianty")
    assert _by_key(attrs, "capacity_gb") is None
    assert _by_key(attrs, "ram_gb") is None


# --------------------------------------------------------------------------------------------------
# Energy.
# --------------------------------------------------------------------------------------------------


def test_capacity_wh_direct() -> None:
    attrs = extract_attrs(title="Laptop Dell Latitude bateria 74Wh")
    cap = _by_key(attrs, "capacity_wh")
    assert cap is not None and cap.value_num == 74.0


def test_mah_without_voltage_kept_as_mah_not_guessed_wh() -> None:
    attrs = extract_attrs(title="Powerbank Anker PowerCore 20000mAh")
    assert _by_key(attrs, "capacity_wh") is None
    mah = _by_key(attrs, "capacity_mah")
    assert mah is not None and mah.value_num == 20000.0


def test_mah_with_voltage_computes_wh() -> None:
    attrs = extract_attrs(title="Bateria 20000mAh 3.7V do laptopa")
    wh = _by_key(attrs, "capacity_wh")
    assert wh is not None and wh.value_num == 74.0  # 20000 * 3.7 / 1000


# --------------------------------------------------------------------------------------------------
# Screen.
# --------------------------------------------------------------------------------------------------


def test_screen_diagonal_refresh_and_resolution() -> None:
    attrs = extract_attrs(title='Monitor LG UltraGear 27GP850 27" 165Hz 2560x1440')
    diagonal = _by_key(attrs, "diagonal_in")
    refresh = _by_key(attrs, "refresh_hz")
    px_w = _by_key(attrs, "resolution_px_w")
    px_h = _by_key(attrs, "resolution_px_h")
    assert diagonal is not None and diagonal.value_num == 27.0
    assert refresh is not None and refresh.value_num == 165.0
    assert px_w is not None and px_w.value_num == 2560.0
    assert px_h is not None and px_h.value_num == 1440.0


def test_screen_diagonal_in_cali_form() -> None:
    attrs = extract_attrs(title="Monitor Dell 27 cali Full HD")
    diagonal = _by_key(attrs, "diagonal_in")
    assert diagonal is not None and diagonal.value_num == 27.0


# --------------------------------------------------------------------------------------------------
# Mass / length / power, and the "5G network" vs. "5 g mass" trap.
# --------------------------------------------------------------------------------------------------


def test_mass_kg_lowercase_unit() -> None:
    attrs = extract_attrs(title="Laptop Lenovo IdeaPad Slim 5, waga 1.4 kg")
    mass = _by_key(attrs, "mass_kg")
    assert mass is not None and abs(mass.value_num - 1.4) < 1e-9


def test_5g_network_generation_is_not_mistaken_for_5_grams() -> None:
    """"Smartfon 5G" must never be read as a 5-gram mass -- classic unit-token collision (A2 spec, section 6)."""
    attrs = extract_attrs(title="Smartfon Xiaomi 5G 8/256GB")
    assert _by_key(attrs, "mass_kg") is None


def test_length_m_with_comma_decimal() -> None:
    attrs = extract_attrs(title="Kabel USB-C 1,5 m 65W")
    length = _by_key(attrs, "length_m")
    assert length is not None and abs(length.value_num - 1.5) < 1e-9


def test_power_w() -> None:
    attrs = extract_attrs(title="Ładowarka sieciowa 65W USB-C")
    power = _by_key(attrs, "power_w")
    assert power is not None and power.value_num == 65.0


def test_power_w_does_not_collide_with_energy_wh() -> None:
    attrs = extract_attrs(title="Powerbank 20000mAh 74Wh 100W szybkie ładowanie")
    power = _by_key(attrs, "power_w")
    energy = _by_key(attrs, "capacity_wh")
    assert power is not None and power.value_num == 100.0
    assert energy is not None and energy.value_num == 74.0


# --------------------------------------------------------------------------------------------------
# extracted_from precedence.
# --------------------------------------------------------------------------------------------------


def test_extracted_from_prefers_feed_field_over_title() -> None:
    attrs = extract_attrs(title="Dysk SSD uniwersalny", feed_fields={"capacity": "1TB"})
    cap = _by_key(attrs, "capacity_gb")
    assert cap is not None and cap.extracted_from == "feed_field" and cap.value_num == 1000.0


def test_no_attrs_found_returns_empty_list() -> None:
    assert extract_attrs(title="Uniwersalny kabel bez specyfikacji") == []
