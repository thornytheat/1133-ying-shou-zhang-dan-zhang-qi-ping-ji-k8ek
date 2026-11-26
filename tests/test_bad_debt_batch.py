from invoicecover.pipeline import AS_OF, build
from invoicecover.bad_debt_provision.contracts import ProvisionLane
from invoicecover.bad_debt_provision.engine import ProvisionLedger


def test_batch_sheet_reconciles_every_debtor_to_book_outstanding():
    result, sheet = build()
    document = result["provision"]
    assert document["records"]
    reconciled = document["reconciliation"]
    assert {row["经销商"] for row in reconciled} == {row["经销商"] for row in document["records"]}
    assert all(row["差额（分）"] == 0 for row in reconciled)
    assert sum(row["账面未结（分）"] for row in reconciled) == document["totals"]["账面未结（分）"]
    assert document["totals"]["账面未结（分）"] == result["stages"][-1]["cash"]["cents_open"]
    assert document["as_of"] == AS_OF.isoformat()


def test_provision_comes_only_from_specific_and_portfolio_lanes():
    result, sheet = build()
    totals = result["provision"]["totals"]
    lanes = {row["归口"]: row for row in result["provision"]["lanes"]}
    assert totals["本期计提（分）"] == (
        lanes["单户核证"]["计提（分）"] + lanes["组合评分"]["计提（分）"]
    )
    assert totals["待确认版本敞口（分）"] == 0
    assert totals["待入账回单敞口（分）"] > 0
    assert totals["待复核敞口（分）"] > 0
    for row in result["provision"]["records"]:
        assert row["本期计提（元）"] <= row["单户核证（元）"] + row["组合评分（元）"]


def test_out_of_support_debtor_is_listed_waiting_not_estimated():
    result, sheet = build()
    waiting = [row for row in result["provision"]["records"] if row["待复核（元）"]]
    assert waiting
    assert all(row["本期计提（元）"] == 0 for row in waiting)
    assert all(row["准备依据"] == ProvisionLane.UNSUPPORTED.value for row in waiting)


def test_pending_receipt_and_pending_version_are_held_out_of_provision():
    result, sheet = build()
    deferred = result["provision"]["deferred"]
    assert deferred
    receipt_rows = [row for row in deferred if "回单待入账" in row["原因"]]
    assert receipt_rows and all(row["金额（分）"] > 0 for row in receipt_rows)
    totals = result["provision"]["totals"]
    assert totals["待入账回单敞口（分）"] > 0
    assert totals["未挂账回单（分）"] > 0


def test_batch_lane_exposure_is_exhaustive_and_matches_book():
    result, sheet = build()
    lanes = result["provision"]["lanes"]
    assert sum(row["敞口（分）"] for row in lanes) == sheet.book_total_cents
    assert sum(row["计提（分）"] for row in lanes) == sheet.provision_total_cents


def test_rerunning_the_same_batch_adds_nothing_to_the_ledger():
    first_result, first_sheet = build()
    ledger = ProvisionLedger()
    assert ledger.post(first_sheet) is True
    total = ledger.provision_total_cents
    per_debtor = ledger.debtor_totals()
    for _ in range(3):
        rerun_result, rerun_sheet = build()
        assert rerun_result["provision"]["batch_id"] == first_result["provision"]["batch_id"]
        assert ledger.post(rerun_sheet) is False
        assert ledger.provision_total_cents == total
        assert ledger.debtor_totals() == per_debtor
    assert ledger.batches == 1
    assert ledger.provision_total_cents == first_result["provision"]["totals"]["本期计提（分）"]


def test_capacity_change_does_not_move_the_provision_sheet():
    low, _ = build(1)
    high, _ = build(9)
    assert low["provision"] == high["provision"]


def test_input_order_is_irrelevant_to_deviation_and_total():
    result, sheet = build()
    assert sheet.batch_id == build()[0]["provision"]["batch_id"]
    assert sum(row.provision_cents for row in sheet.debtors) == sheet.provision_total_cents
