from backend.inventory import InventoryItem
from backend.scaling import (
    aggregate_reagents,
    build_prep_table,
    convert_volume,
    find_inventory_group,
    humanize_volume_text,
    protocol_ingredients,
    scale_reagents,
)
from backend.state import Protocol, Step


def protocol_with_steps(*steps: Step) -> Protocol:
    return Protocol(id="p", name="Protocol", steps=list(steps))


def test_convert_volume_between_volume_units():
    assert convert_volume(1000, "uL", "mL") == 1
    assert convert_volume(1.5, "mL", "uL") == 1500
    assert convert_volume(0.002, "L", "uL") == 2000
    assert convert_volume(1000, "microliters", "mL") == 1


def test_convert_volume_rejects_non_volume_units():
    assert convert_volume(5, "g", "uL") is None
    assert convert_volume(5, "uL", "g") is None
    assert convert_volume(5, "", "uL") is None
    assert convert_volume(5, "units", "mL") is None


def test_humanize_volume_text_inflated_ul_to_ml():
    assert humanize_volume_text("Add 50000 uL buffer") == "Add 50 mL buffer"
    assert humanize_volume_text("Add 2000 uL ethanol") == "Add 2 mL ethanol"
    assert humanize_volume_text("Add 1500.0 uL of TRIzol") == "Add 1.5 mL of TRIzol"


def test_humanize_volume_text_leaves_sub_ml_untouched():
    assert humanize_volume_text("Add 950 uL water") == "Add 950 uL water"
    assert humanize_volume_text("Add 200 uL lysis buffer") == "Add 200 uL lysis buffer"


def test_humanize_volume_text_hits_liter_threshold():
    assert humanize_volume_text("Add 1000000 uL water") == "Add 1 L water"
    # exactly 1000 uL is the mL boundary (matches _display_volume's >= rule)
    assert humanize_volume_text("Add 1000 uL water") == "Add 1 mL water"


def test_humanize_volume_text_does_not_touch_non_volume_numbers():
    assert humanize_volume_text("Incubate at 65 degrees C") == "Incubate at 65 degrees C"
    assert humanize_volume_text("Centrifuge at 13,000 g") == "Centrifuge at 13,000 g"
    assert humanize_volume_text("Run 30 cycles") == "Run 30 cycles"
    assert humanize_volume_text("Add 100 mL buffer") == "Add 100 mL buffer"


def test_humanize_volume_text_idempotent_and_empty_safe():
    once = humanize_volume_text("Add 50000 uL buffer and 950 uL water")
    assert once == "Add 50 mL buffer and 950 uL water"
    assert humanize_volume_text(once) == once
    assert humanize_volume_text("Mix gently") == "Mix gently"
    assert humanize_volume_text("") == ""


def test_aggregate_reagents_sums_repeated_reagent():
    proto = protocol_with_steps(
        Step(id=1, text="add buffer", parameters={"reagent": "buffer", "volume_ul": 200}),
        Step(id=2, text="spin", parameters={"speed_g": 13000}),
        Step(id=3, text="add buffer again", parameters={"reagent": "buffer", "volume_ul": 50}),
        Step(id=4, text="add water", parameters={"reagent": "water", "volume_ul": 10.5}),
    )

    assert aggregate_reagents(proto) == {"buffer": 250.0, "water": 10.5}


def test_protocol_ingredients_lists_name_and_display_amount():
    proto = protocol_with_steps(
        Step(id=1, text="add buffer", parameters={"reagent": "buffer", "volume_ul": 200}),
        Step(id=2, text="add buffer again", parameters={"reagent": "buffer", "volume_ul": 50}),
        Step(id=3, text="spin", parameters={"speed_g": 13000}),
        Step(id=4, text="add ethanol", parameters={"reagent": "ethanol", "volume_ul": 2000}),
    )
    assert protocol_ingredients(proto) == [
        {"reagent": "buffer", "volume_ul": 250, "display": "250 uL"},
        {"reagent": "ethanol", "volume_ul": 2000, "display": "2 mL"},
    ]


def test_protocol_ingredients_empty_when_no_volumes():
    proto = protocol_with_steps(Step(id=1, text="mix", parameters={}))
    assert protocol_ingredients(proto) == []


def test_aggregate_reagents_ignores_missing_or_non_numeric_values():
    proto = protocol_with_steps(
        Step(id=1, text="missing reagent", parameters={"volume_ul": 200}),
        Step(id=2, text="missing volume", parameters={"reagent": "buffer"}),
        Step(id=3, text="string volume", parameters={"reagent": "buffer", "volume_ul": "200"}),
        Step(id=4, text="boolean volume", parameters={"reagent": "buffer", "volume_ul": True}),
    )

    assert aggregate_reagents(proto) == {}


def test_scale_reagents_for_dna_extraction_numbers():
    proto = protocol_with_steps(
        Step(id=1, text="lysis", parameters={"reagent": "lysis buffer", "volume_ul": 200}),
        Step(id=2, text="ethanol", parameters={"reagent": "ethanol", "volume_ul": 200}),
        Step(id=3, text="water", parameters={"reagent": "nuclease-free water", "volume_ul": 50}),
    )

    rows = scale_reagents(proto, n_samples=12, overage_pct=10)

    by_name = {row["reagent"]: row for row in rows}
    assert by_name["lysis buffer"]["total_ul"] == 2640
    assert by_name["lysis buffer"]["total_display"] == "2.64 mL"
    assert by_name["ethanol"]["total_ul"] == 2640
    assert by_name["nuclease-free water"]["total_ul"] == 660
    assert by_name["nuclease-free water"]["total_display"] == "660 uL"


def test_scale_reagents_rejects_bad_inputs():
    proto = protocol_with_steps(
        Step(id=1, text="lysis", parameters={"reagent": "lysis buffer", "volume_ul": 200})
    )

    try:
        scale_reagents(proto, n_samples=0, overage_pct=10)
    except ValueError as exc:
        assert "sample count" in str(exc).lower()
    else:
        raise AssertionError("sample_count=0 should fail")

    try:
        scale_reagents(proto, n_samples=1, overage_pct=-1)
    except ValueError as exc:
        assert "overage" in str(exc).lower()
    else:
        raise AssertionError("negative overage should fail")


def test_build_prep_table_inventory_verdicts():
    proto = protocol_with_steps(
        Step(id=1, text="ethanol", parameters={"reagent": "ethanol", "volume_ul": 200}),
        Step(id=2, text="water", parameters={"reagent": "water", "volume_ul": 50}),
        Step(id=3, text="proteinase", parameters={"reagent": "Proteinase K", "volume_ul": 200}),
        Step(id=4, text="agarose", parameters={"reagent": "agarose", "volume_ul": 100}),
        Step(id=5, text="lysis", parameters={"reagent": "lysis buffer", "volume_ul": 200}),
        Step(id=6, text="soc", parameters={"reagent": "SOC medium", "volume_ul": 100}),
    )
    inventory = [
        InventoryItem(name="Ethanol 70%", amount="1000", unit="mL", location="Cabinet", quantity_approx="", status="ok"),
        InventoryItem(name="Water", amount="500", unit="uL", location="Fridge", quantity_approx="", status="ok"),
        InventoryItem(name="Proteinase K", amount="2", unit="uL", location="Freezer", quantity_approx="", status="low"),
        InventoryItem(name="Agarose", amount="500", unit="g", location="Cabinet", quantity_approx="", status="ok"),
        InventoryItem(name="SOC medium", amount="100", unit="mL", location="Fridge", quantity_approx="", status="critical"),
    ]

    rows = build_prep_table(proto, n_samples=2, overage_pct=0, inventory=inventory)
    by_name = {row["reagent"]: row for row in rows}

    assert by_name["ethanol"]["verdict"] == "in_stock"
    assert by_name["ethanol"]["match_name"] == "Ethanol 70%"
    assert by_name["ethanol"]["location"] == "Cabinet"

    assert by_name["water"]["verdict"] == "in_stock"

    assert by_name["Proteinase K"]["verdict"] == "insufficient"
    assert by_name["Proteinase K"]["shortage_ul"] == 398
    assert by_name["Proteinase K"]["status"] == "low"
    assert by_name["Proteinase K"]["location"] == "Freezer"

    assert by_name["agarose"]["verdict"] == "unknown_unit"
    assert by_name["agarose"]["available_unit"] == "g"

    assert by_name["lysis buffer"]["verdict"] == "missing"
    assert by_name["lysis buffer"]["match_name"] is None
    # No inventory match -> no location to report.
    assert by_name["lysis buffer"]["location"] is None

    assert by_name["SOC medium"]["verdict"] == "critical"


def test_build_prep_table_pools_multiple_bottles_of_same_reagent():
    # No single EDTA bottle covers the need, but the three pooled together do.
    proto = protocol_with_steps(
        Step(id=1, text="edta", parameters={"reagent": "EDTA", "volume_ul": 30000}),
    )
    inventory = [
        InventoryItem(name="EDTA", amount="20", unit="mL", location="A", quantity_approx="", status="ok"),
        InventoryItem(name="EDTA 0.5 M", amount="10", unit="mL", location="B", quantity_approx="", status="low"),
        InventoryItem(name="EDTA (stock)", amount="5", unit="mL", location="C", quantity_approx="", status="ok"),
    ]

    rows = build_prep_table(proto, n_samples=1, overage_pct=0, inventory=inventory)
    row = rows[0]

    assert row["verdict"] == "low"  # 35 mL pooled covers 30 mL; worst status wins
    assert row["available"] == 35000  # 20 + 10 + 5 mL, in uL
    assert row["match_count"] == 3
    assert set(row["sources"]) == {"EDTA", "EDTA 0.5 M", "EDTA (stock)"}


def test_build_prep_table_pooled_still_insufficient_reports_shortage():
    proto = protocol_with_steps(
        Step(id=1, text="edta", parameters={"reagent": "EDTA", "volume_ul": 50000}),
    )
    inventory = [
        InventoryItem(name="EDTA", amount="20", unit="mL", location="A", quantity_approx="", status="ok"),
        InventoryItem(name="EDTA 0.5 M", amount="10", unit="mL", location="B", quantity_approx="", status="ok"),
    ]

    row = build_prep_table(proto, n_samples=1, overage_pct=0, inventory=inventory)[0]

    assert row["verdict"] == "insufficient"
    assert row["shortage_ul"] == 20000  # need 50 mL, have 30 mL pooled


def test_find_inventory_group_uses_see_also_alias():
    # The shipped EDTA row carries "see also: Ethylenediaminetetraacetic acid".
    inventory = [
        InventoryItem(name="EDTA", amount="20", unit="mL", location="A", quantity_approx="",
                      notes="see also: Ethylenediaminetetraacetic acid", status="ok"),
        InventoryItem(name="Ethylenediaminetetraacetic acid", amount="10", unit="mL",
                      location="B", quantity_approx="", status="ok"),
        InventoryItem(name="Ethanol 70%", amount="1000", unit="mL", location="C",
                      quantity_approx="", status="ok"),
    ]

    anchor, group = find_inventory_group("EDTA", inventory, cutoff=0.75)

    assert anchor.name == "EDTA"
    assert {item.name for item in group} == {"EDTA", "Ethylenediaminetetraacetic acid"}
