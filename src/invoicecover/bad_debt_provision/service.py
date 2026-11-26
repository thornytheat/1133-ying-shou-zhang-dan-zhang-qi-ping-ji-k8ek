from .contracts import (
    DebtorProvision,
    EvidenceState,
    ProvisionLane,
    ProvisionSheet,
)

EXPOSURE_FIELDS = (
    ("单户核证（元）", "specific_cents"),
    ("组合评分（元）", "portfolio_cents"),
    ("待确认版本（元）", "pending_version_cents"),
    ("待入账回单（元）", "pending_receipt_cents"),
    ("待复核（元）", "unsupported_cents"),
)


def _yuan(cents):
    return round(cents / 100, 2)


def _exclusion_reason(row: DebtorProvision):
    reasons = []
    if row.pending_version_cents:
        reasons.append(EvidenceState.VERSION_PENDING.value)
    if row.pending_receipt_cents:
        reasons.append(EvidenceState.RECEIPT_PENDING.value)
    if row.unsupported_cents:
        reasons.append(EvidenceState.UNSCORED.value)
    return "、".join(reasons)


def rows(provision: ProvisionSheet):
    """逐户准备清单；五段敞口互斥，合计等于该户账面未结。"""
    panel = []
    for row in provision.debtors:
        record = {"经销商": row.debtor, "账面未结（元）": _yuan(row.book_cents)}
        for label, field in EXPOSURE_FIELDS:
            record[label] = _yuan(getattr(row, field))
        record["本期计提（元）"] = _yuan(row.provision_cents)
        record["准备依据"] = row.basis
        record["证据状态"] = "、".join(state.value for state in row.states)
        panel.append(record)
    return panel


def reconcile(provision: ProvisionSheet):
    """逐户用准备清单与账面未结对平，任何一户不平即拒绝出表。"""
    checks = []
    for row in provision.debtors:
        difference = row.book_cents - row.exposure_cents
        if difference:
            raise ValueError(f"{row.debtor} 准备清单与账面未结不平：{difference} 分")
        if row.provision_cents > row.specific_cents + row.portfolio_cents:
            raise ValueError(f"{row.debtor} 计提额超出单户核证与组合评分两侧")
        checks.append(
            {
                "经销商": row.debtor,
                "账面未结（分）": row.book_cents,
                "分段合计（分）": row.exposure_cents,
                "差额（分）": difference,
            }
        )
    if sum(row["账面未结（分）"] for row in checks) != provision.book_total_cents:
        raise ValueError("逐户账面未结与批次账面总额不等")
    return checks


def _lane_summary(provision):
    return [
        {
            "归口": lane.value,
            "笔数": sum(row.lane == lane for row in provision.lines),
            "敞口（分）": sum(
                row.exposure_cents for row in provision.lines if row.lane == lane
            ),
            "计提（分）": sum(
                row.provision_cents for row in provision.lines if row.lane == lane
            ),
        }
        for lane in ProvisionLane
    ]


def sheet(provision: ProvisionSheet):
    """本期坏账准备清单：逐户计提、汇总、逐户对账与暂缓明细。"""
    panel = rows(provision)
    return {
        "title": "本期坏账准备清单",
        "summary": (
            f"本期计提 {provision.provision_total_cents / 100:,.2f} 元，"
            f"逐户与账面未结对平，批次 {provision.batch_id}"
        ),
        "batch_id": provision.batch_id,
        "as_of": provision.as_of.isoformat(),
        "columns": {key: key for key in panel[0]} if panel else {},
        "records": panel,
        "totals": {
            "账面未结（分）": provision.book_total_cents,
            "本期计提（分）": provision.provision_total_cents,
            "单户核证敞口（分）": provision.specific_total_cents,
            "组合评分敞口（分）": provision.portfolio_total_cents,
            "待确认版本敞口（分）": provision.pending_version_total_cents,
            "待入账回单敞口（分）": provision.pending_receipt_total_cents,
            "待复核敞口（分）": provision.unsupported_total_cents,
            "未挂账回单（分）": provision.unattached_cents,
        },
        "lanes": _lane_summary(provision),
        "reconciliation": reconcile(provision),
        "deferred": [
            {
                "经销商": row.debtor,
                "原因": _exclusion_reason(row),
                "金额（分）": row.pending_version_cents
                + row.pending_receipt_cents
                + row.unsupported_cents,
                "证据状态": "、".join(state.value for state in row.states),
            }
            for row in provision.debtors
            if row.pending_version_cents
            or row.pending_receipt_cents
            or row.unsupported_cents
        ],
        "lines": [
            {
                "账单": row.invoice,
                "经销商": row.debtor,
                "归口": row.lane.value,
                "证据状态": row.state.value,
                "敞口（分）": row.exposure_cents,
                "计提率": None if row.rate is None else round(row.rate, 5),
                "计提（分）": row.provision_cents,
                "逾期天数": row.overdue_days,
            }
            for row in provision.lines
        ],
    }


def ledger_panel(ledger):
    """正式台账的累计视图；同一批次只登记一次。"""
    return {
        "已登记批次": ledger.batches,
        "重复投递": ledger.duplicates,
        "累计计提（分）": ledger.provision_total_cents,
        "逐户累计（分）": ledger.debtor_totals(),
        "按归口累计（分）": ledger.lane_totals(),
    }
