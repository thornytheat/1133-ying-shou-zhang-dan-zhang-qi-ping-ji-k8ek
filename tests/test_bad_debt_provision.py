from dataclasses import replace
from datetime import date
import pytest

from invoicecover.bad_debt_provision.contracts import (
    EvidenceState,
    ProvisionLane,
    ProvisionPolicy,
)
from invoicecover.bad_debt_provision.engine import (
    ProvisionLedger,
    _provision,
    allocate,
)
from invoicecover.bad_debt_provision.service import ledger_panel, reconcile, rows
from invoicecover.invoice_cutoff.contracts import Bill, DocumentFlag, EntryDecision
from invoicecover.repayment_clock.contracts import (
    CashDisposition,
    CashEntry,
    Settlement,
)
from invoicecover.term_scorecard.contracts import DebtorScore

NOW = date(2026, 9, 1)


def bill(number, debtor, cents, due):
    issued = date(2025, 11, 1) if due < date(2026, 5, 1) else date(2026, 5, 1)
    return Bill(number, debtor, "代理", issued, due, issued, 1, cents)


def settlement(bill, outstanding):
    paid = bill.cents - outstanding
    entries = (
        (CashEntry("RC-1", bill.number, paid, 0, CashDisposition.POSTED, None),)
        if paid
        else ()
    )
    return Settlement(bill.number, outstanding, paid, None, entries)


def score(debtor, probability):
    return DebtorScore(debtor, 0.5, probability, 0.0, "可评分", ())


def run_case(bills, outstanding, decisions=(), custody=(), scores=None, policy=None):
    statements = [settlement(item, outstanding[item.number]) for item in bills]
    if scores is None:
        scores = [score(item, 1.0) for item in {bill.debtor for bill in bills}]
    return allocate(
        bills, statements, custody, decisions, scores, NOW, policy
    )


def lane_cents(sheet, debtor, lane):
    row = next(item for item in sheet.debtors if item.debtor == debtor)
    return getattr(row, f"{_FIELD[lane]}_cents")


_FIELD = {
    ProvisionLane.SPECIFIC: "specific",
    ProvisionLane.PORTFOLIO: "portfolio",
    ProvisionLane.PENDING_VERSION: "pending_version",
    ProvisionLane.PENDING_RECEIPT: "pending_receipt",
    ProvisionLane.UNSUPPORTED: "unsupported",
}


def test_specific_and_portfolio_never_double_count_the_same_cents():
    bills = [
        bill("A", "甲", 10000, date(2026, 6, 1)),  # 逾期 92 天 → 单户核证
        bill("B", "甲", 5000, date(2026, 8, 25)),  # 逾期 7 天 → 组合评分
    ]
    sheet = run_case(
        bills,
        {"A": 10000, "B": 5000},
        scores=[score("甲", 0.4)],
    )
    debtor = sheet.debtors[0]
    assert lane_cents(sheet, "甲", ProvisionLane.SPECIFIC) == 10000
    assert lane_cents(sheet, "甲", ProvisionLane.PORTFOLIO) == 5000
    assert debtor.provision_cents == 10000 + 2000
    assert debtor.exposure_cents == debtor.book_cents == 15000
    assert reconcile(sheet)[0]["差额（分）"] == 0


def test_one_side_is_always_taken_and_the_other_side_never_adds_on_top():
    bills = [bill("A", "甲", 10000, date(2026, 6, 1))]
    sheet = run_case(bills, {"A": 10000}, scores=[score("甲", 0.9)])
    debtor = sheet.debtors[0]
    assert debtor.portfolio_cents == 0
    assert debtor.provision_cents == 10000  # 不是 10000 + 9000


def test_unsupported_debtor_is_listed_but_never_estimated():
    bills = [bill("A", "东汇", 23_000_000, date(2025, 12, 1))]
    sheet = run_case(bills, {"A": 23_000_000}, scores=[score("东汇", None)])
    debtor = sheet.debtors[0]
    assert debtor.unsupported_cents == 23_000_000
    assert debtor.provision_cents == 0
    assert debtor.basis == ProvisionLane.UNSUPPORTED.value
    assert debtor.states == (EvidenceState.UNSCORED,)


def test_pending_bill_version_is_excluded_from_provision():
    target = bill("A", "甲", 10000, date(2026, 6, 1))
    decision = EntryDecision("A", DocumentFlag.FUTURE, 2, 9000)
    sheet = run_case([target], {"A": 10000}, decisions=[decision])
    debtor = sheet.debtors[0]
    assert debtor.pending_version_cents == 10000
    assert debtor.provision_cents == 0
    assert EvidenceState.VERSION_PENDING in debtor.states


def test_pending_receipt_holds_only_the_receipt_amount():
    target = bill("A", "甲", 10000, date(2026, 8, 20))  # 12 天 → 组合评分
    late = CashEntry("RC-LATE", "A", 0, 3000, CashDisposition.LATE_POST, NOW)
    sheet = run_case(
        [target], {"A": 10000}, custody=[late], scores=[score("甲", 0.5)]
    )
    debtor = sheet.debtors[0]
    assert debtor.pending_receipt_cents == 3000
    assert debtor.portfolio_cents == 7000
    assert debtor.provision_cents == 3500
    assert debtor.exposure_cents == debtor.book_cents == 10000


def test_rounding_is_per_line_and_sum_stays_within_exposure():
    bills = [
        bill("A", "甲", 333, date(2026, 8, 20)),
        bill("B", "甲", 333, date(2026, 8, 20)),
        bill("C", "甲", 333, date(2026, 8, 20)),
    ]
    sheet = run_case(
        bills, {"A": 333, "B": 333, "C": 333}, scores=[score("甲", 0.5)]
    )
    debtor = sheet.debtors[0]
    assert debtor.provision_cents == 3 * 167  # 逐笔四舍五入，而非对总额取整
    assert debtor.provision_cents <= debtor.portfolio_cents


def test_specific_rate_caps_provision_at_exposure():
    assert _provision(100, 1.0) == 100
    assert _provision(100, 0.5) == 50
    assert _provision(1, 0.5) == 1  # 0.5 分向上取整且不超过敞口


def test_repeated_batch_posting_does_not_increase_totals():
    bills = [bill("A", "甲", 10000, date(2026, 6, 1)), bill("B", "乙", 5000, date(2026, 8, 25))]
    sheet = run_case(bills, {"A": 10000, "B": 5000})
    ledger = ProvisionLedger()
    assert ledger.post(sheet) is True
    first_total = ledger.provision_total_cents
    assert ledger.post(sheet) is False
    assert ledger.post(sheet) is False
    assert ledger.batches == 1
    assert ledger.duplicates == 2
    assert ledger.provision_total_cents == first_total
    assert sum(ledger.debtor_totals().values()) == first_total


def test_same_batch_id_with_different_content_is_rejected():
    bills = [bill("A", "甲", 10000, date(2026, 6, 1))]
    sheet = run_case(bills, {"A": 10000})
    altered = run_case(bills, {"A": 10000}, policy=ProvisionPolicy(specific_rate=0.5))
    assert altered.batch_id != sheet.batch_id  # 策略不同即不同批次
    ledger = ProvisionLedger()
    ledger.post(sheet)
    with pytest.raises(ValueError, match="同批次编号"):
        ledger.post(replace(altered, batch_id=sheet.batch_id))


def test_every_debtor_row_reconciles_to_book_outstanding():
    bills = [
        bill("A", "甲", 10000, date(2026, 6, 1)),
        bill("B", "乙", 7000, date(2026, 8, 25)),
        bill("C", "丙", 4000, date(2025, 12, 1)),
    ]
    sheet = run_case(
        bills,
        {"A": 10000, "B": 7000, "C": 4000},
        scores=[score("甲", 0.6), score("乙", 0.3), score("丙", None)],
    )
    checks = reconcile(sheet)
    assert len(checks) == 3
    assert all(row["差额（分）"] == 0 for row in checks)
    assert sum(row["账面未结（分）"] for row in checks) == sheet.book_total_cents
    panel = rows(sheet)
    assert sum(row["账面未结（元）"] for row in panel) == sheet.book_total_cents / 100
    assert sum(row["本期计提（元）"] for row in panel) == sheet.provision_total_cents / 100


def test_fully_settled_debtor_still_reconciles_to_zero():
    target = bill("A", "甲", 10000, date(2026, 6, 1))
    sheet = run_case([target], {"A": 0})
    debtor = sheet.debtors[0]
    assert debtor.book_cents == 0 and debtor.provision_cents == 0
    assert debtor.states == (EvidenceState.SETTLED,)


def test_unmatched_score_or_settlement_rejects_whole_sheet():
    bills = [bill("A", "甲", 10000, date(2026, 6, 1))]
    with pytest.raises(ValueError, match="评分册"):
        run_case(bills, {"A": 10000}, scores=[score("乙", 0.5)])
    with pytest.raises(ValueError, match="逐户对账"):
        allocate(
            bills,
            [],
            [],
            [],
            [score("甲", 0.5)],
            NOW,
        )


def test_input_order_does_not_change_the_sheet():
    bills = [
        bill("A", "甲", 10000, date(2026, 6, 1)),
        bill("B", "乙", 7000, date(2026, 8, 25)),
    ]
    outstanding = {"A": 10000, "B": 7000}
    forward = run_case(bills, outstanding)
    backward = run_case(bills[::-1], outstanding)
    assert forward.batch_id == backward.batch_id
    assert rows(forward) == rows(backward)


def test_ledger_panel_separates_exposure_from_provision_per_lane():
    bills = [bill("A", "甲", 10000, date(2026, 8, 25))]
    sheet = run_case(bills, {"A": 10000}, scores=[score("甲", 0.5)])
    ledger = ProvisionLedger()
    ledger.post(sheet)
    panel = ledger_panel(ledger)
    assert panel["已登记批次"] == 1
    assert panel["累计计提（分）"] == 5000
    assert panel["按归口累计（分）"][ProvisionLane.PORTFOLIO.value] == {
        "敞口（分）": 10000,
        "计提（分）": 5000,
    }
    assert panel["按归口累计（分）"][ProvisionLane.UNSUPPORTED.value] == {
        "敞口（分）": 0,
        "计提（分）": 0,
    }


def test_exposure_split_is_exhaustive_and_mutually_exclusive():
    bills = [
        bill("A", "甲", 10000, date(2026, 6, 1)),
        bill("B", "甲", 5000, date(2026, 8, 25)),
        bill("C", "乙", 4000, date(2025, 12, 1)),
    ]
    sheet = run_case(
        bills,
        {"A": 10000, "B": 5000, "C": 4000},
        scores=[score("甲", 0.4), score("乙", None)],
    )
    assert sum(item.exposure_cents for item in sheet.lines) == sheet.book_total_cents
    for row in sheet.debtors:
        assert (
            row.specific_cents
            + row.portfolio_cents
            + row.pending_version_cents
            + row.pending_receipt_cents
            + row.unsupported_cents
        ) == row.book_cents
