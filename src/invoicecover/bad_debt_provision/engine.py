from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from hashlib import sha256
from threading import RLock
import json

from ..invoice_cutoff.contracts import DocumentFlag
from ..repayment_clock.contracts import CashDisposition
from .contracts import (
    Allocation,
    DebtorProvision,
    EvidenceState,
    ProvisionLane,
    ProvisionPolicy,
    ProvisionSheet,
)


def _provision(cents, rate):
    """逐笔按分四舍五入；单笔计提额不超过本笔敞口。"""
    exact = Decimal(cents) * Decimal(str(rate))
    rounded = int(exact.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return min(max(rounded, 0), cents)


def _hold_for_late_receipt(outstanding, late_receipts):
    """待入账回单只暂扣与本回单等额的敞口，其余仍可计提。"""
    hold = 0
    for suspense in late_receipts:
        hold += min(outstanding - hold, suspense)
    return hold


def _batch_identity(as_of, bills, settlements, custody, decisions, scores, policy):
    payload = {
        "as_of": as_of.isoformat(),
        "policy": [policy.specific_days, repr(policy.specific_rate)],
        "bills": sorted(
            (bill.number, bill.debtor, bill.cents, bill.due.isoformat())
            for bill in bills
        ),
        "settlements": sorted(
            (
                row.invoice,
                row.outstanding,
                row.paid,
                row.paid_on.isoformat() if row.paid_on else None,
            )
            for row in settlements
        ),
        "custody": sorted(
            (row.receipt, row.invoice, str(row.flag), row.suspense) for row in custody
        ),
        "decisions": sorted(
            (row.number, str(row.flag), row.revision, row.cents) for row in decisions
        ),
        "scores": sorted(
            (row.debtor, None if row.probability is None else repr(row.probability))
            for row in scores
        ),
    }
    content = json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False)
    return sha256(content.encode()).hexdigest()[:12]


def allocate(
    bills,
    settlements,
    custody,
    decisions,
    scores,
    as_of,
    policy=None,
) -> ProvisionSheet:
    """把账面未结拆成互不重叠的四段，逐户给出计提额。

    归口顺序（先命中先生效）：
    1. 账单存在截点后新版本 → 待确认版本，金额不计提；
    2. 该账单有已到账但截点后入账的回单 → 待入账回单，仅暂扣回单金额；
    3. 该户超出评分支持 → 待复核，不作估算；
    4. 已逾期超过单户核证天数的进入单户核证，按核证比例计提；
    5. 其余未逾期或逾期尚短的进入组合评分，按该户逾期概率计提。
    """
    policy = policy or ProvisionPolicy()
    settlement_by_invoice = {}
    for row in settlements:
        if row.invoice in settlement_by_invoice:
            raise ValueError("回款账单重复")
        settlement_by_invoice[row.invoice] = row
    bill_numbers = {bill.number for bill in bills}
    if len(bill_numbers) != len(bills):
        raise ValueError("账单编号重复")
    if set(settlement_by_invoice) != bill_numbers:
        raise ValueError("账单与回款记录无法逐户对账")
    score_by_debtor = {row.debtor: row for row in scores}
    if len(score_by_debtor) != len(scores) or not {
        bill.debtor for bill in bills
    } <= set(score_by_debtor):
        raise ValueError("评分册缺少账单欠款方")

    version_pending = {
        row.number for row in decisions if row.flag is DocumentFlag.FUTURE
    }
    late_receipts = {}
    for row in custody:
        if row.flag is CashDisposition.LATE_POST and row.invoice in bill_numbers:
            late_receipts.setdefault(row.invoice, []).append(row.suspense)
    for receipt_amounts in late_receipts.values():
        receipt_amounts.sort()

    lines = []
    book_by_debtor = {}
    for bill in sorted(bills, key=lambda row: (row.debtor, row.number)):
        settlement = settlement_by_invoice[bill.number]
        outstanding = settlement.outstanding
        book_by_debtor[bill.debtor] = book_by_debtor.get(bill.debtor, 0) + outstanding
        if outstanding == 0:
            continue
        overdue_days = (as_of - bill.due).days
        probability = score_by_debtor[bill.debtor].probability

        def line(lane, state, cents, rate=None):
            provision = 0 if rate is None else _provision(cents, rate)
            lines.append(
                Allocation(
                    bill.number,
                    bill.debtor,
                    lane,
                    state,
                    cents,
                    rate,
                    provision,
                    overdue_days,
                )
            )

        if bill.number in version_pending:
            line(
                ProvisionLane.PENDING_VERSION,
                EvidenceState.VERSION_PENDING,
                outstanding,
            )
            continue
        if bill.number in late_receipts:
            held = _hold_for_late_receipt(outstanding, late_receipts[bill.number])
            if held:
                line(
                    ProvisionLane.PENDING_RECEIPT,
                    EvidenceState.RECEIPT_PENDING,
                    held,
                )
            outstanding -= held
            if outstanding == 0:
                continue
        if probability is None:
            line(ProvisionLane.UNSUPPORTED, EvidenceState.UNSCORED, outstanding)
            continue
        if overdue_days > policy.specific_days:
            line(
                ProvisionLane.SPECIFIC,
                EvidenceState.CONFIRMED,
                outstanding,
                policy.specific_rate,
            )
        else:
            line(
                ProvisionLane.PORTFOLIO,
                EvidenceState.CONFIRMED,
                outstanding,
                probability,
            )

    debtors = []
    for debtor in sorted(book_by_debtor):
        own = [row for row in lines if row.debtor == debtor]
        totals = {lane: 0 for lane in ProvisionLane}
        for row in own:
            totals[row.lane] += row.exposure_cents
        provision = sum(row.provision_cents for row in own)
        states = []
        for row in own:
            if row.state not in states:
                states.append(row.state)
        if not states:
            states.append(EvidenceState.SETTLED)
        debtors.append(
            DebtorProvision(
                debtor,
                book_by_debtor[debtor],
                totals[ProvisionLane.SPECIFIC],
                totals[ProvisionLane.PORTFOLIO],
                totals[ProvisionLane.PENDING_VERSION],
                totals[ProvisionLane.PENDING_RECEIPT],
                totals[ProvisionLane.UNSUPPORTED],
                provision,
                tuple(states),
            )
        )
    book_total = sum(book_by_debtor.values())
    if sum(row.exposure_cents for row in lines) != book_total:
        raise RuntimeError("敞口分段未与账面未结守恒")
    unattached = sum(
        row.suspense
        for row in custody
        if row.suspense and row.invoice not in bill_numbers
    )
    return ProvisionSheet(
        as_of,
        _batch_identity(as_of, bills, settlements, custody, decisions, scores, policy),
        tuple(debtors),
        tuple(lines),
        unattached,
        book_total,
    )


@dataclass(frozen=True)
class LedgerEntry:
    batch_id: str
    as_of: date
    debtors: tuple[DebtorProvision, ...]
    lines: tuple[Allocation, ...]
    provision_cents: int


class ProvisionLedger:
    """正式准备台账，按批次号登记；同一批次重投只记一次。"""

    def __init__(self):
        self._entries = {}
        self._duplicates = 0
        self._lock = RLock()

    def post(self, sheet: ProvisionSheet) -> bool:
        """返回本次是否新增了批次；重复投递同一批次返回 False。"""
        with self._lock:
            existing = self._entries.get(sheet.batch_id)
            if existing is not None:
                if (
                    existing.debtors != sheet.debtors
                    or existing.as_of != sheet.as_of
                    or existing.provision_cents != sheet.provision_total_cents
                ):
                    raise ValueError("同批次编号对应不同的准备清单")
                self._duplicates += 1
                return False
            self._entries[sheet.batch_id] = LedgerEntry(
                sheet.batch_id,
                sheet.as_of,
                sheet.debtors,
                sheet.lines,
                sheet.provision_total_cents,
            )
            return True

    @property
    def batches(self):
        with self._lock:
            return len(self._entries)

    @property
    def duplicates(self):
        with self._lock:
            return self._duplicates

    @property
    def provision_total_cents(self):
        with self._lock:
            return sum(entry.provision_cents for entry in self._entries.values())

    def debtor_totals(self):
        with self._lock:
            totals = {}
            for entry in self._entries.values():
                for row in entry.debtors:
                    totals[row.debtor] = totals.get(row.debtor, 0) + row.provision_cents
            return dict(sorted(totals.items()))

    def lane_totals(self):
        with self._lock:
            rows = {}
            lines = [
                line for entry in self._entries.values() for line in entry.lines
            ]
            for lane in ProvisionLane:
                rows[lane.value] = {
                    "敞口（分）": sum(
                        line.exposure_cents for line in lines if line.lane == lane
                    ),
                    "计提（分）": sum(
                        line.provision_cents for line in lines if line.lane == lane
                    ),
                }
            return rows

