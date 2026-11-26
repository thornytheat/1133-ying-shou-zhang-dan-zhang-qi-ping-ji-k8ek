from dataclasses import dataclass
from datetime import date
from enum import StrEnum


class ProvisionLane(StrEnum):
    """每笔敞口只能归属于一个计提侧，四类证据侧不参与准备。"""

    SPECIFIC = "单户核证"
    PORTFOLIO = "组合评分"
    PENDING_VERSION = "待确认版本"
    PENDING_RECEIPT = "待入账回单"
    UNSUPPORTED = "待复核"


PROVISION_LANES = (ProvisionLane.SPECIFIC, ProvisionLane.PORTFOLIO)


class EvidenceState(StrEnum):
    CONFIRMED = "证据齐备"
    VERSION_PENDING = "账单版本未确认"
    RECEIPT_PENDING = "回单待入账"
    UNSCORED = "超出评分支持"
    SETTLED = "账面已结清"


@dataclass(frozen=True)
class ProvisionPolicy:
    """单户核证门槛按已逾期天数；组合评分沿用该户逾期概率。"""

    specific_days: int = 60
    specific_rate: float = 1.0

    def __post_init__(self):
        if type(self.specific_days) is not int or self.specific_days < 0:
            raise ValueError("单户核证门槛必须为非负整数天")
        if not 0 < self.specific_rate <= 1:
            raise ValueError("单户核证计提比例须介于 0 与 1 之间")


@dataclass(frozen=True)
class Allocation:
    """单张账单未结金额在计提侧上的一段。"""

    invoice: str
    debtor: str
    lane: ProvisionLane
    state: EvidenceState
    exposure_cents: int
    rate: float | None
    provision_cents: int
    overdue_days: int

    def __post_init__(self):
        if self.exposure_cents < 0 or self.provision_cents < 0:
            raise ValueError("敞口与准备不能为负")
        if self.exposure_cents and self.lane not in PROVISION_LANES:
            if self.provision_cents:
                raise ValueError("未决或待复核敞口不得计提")
        if self.provision_cents > self.exposure_cents:
            raise ValueError("计提额不能超过敞口")


@dataclass(frozen=True)
class DebtorProvision:
    debtor: str
    book_cents: int
    specific_cents: int
    portfolio_cents: int
    pending_version_cents: int
    pending_receipt_cents: int
    unsupported_cents: int
    provision_cents: int
    states: tuple[EvidenceState, ...]

    @property
    def exposure_cents(self):
        return (
            self.specific_cents
            + self.portfolio_cents
            + self.pending_version_cents
            + self.pending_receipt_cents
            + self.unsupported_cents
        )

    @property
    def basis(self):
        parts = []
        if self.specific_cents:
            parts.append(ProvisionLane.SPECIFIC.value)
        if self.portfolio_cents:
            parts.append(ProvisionLane.PORTFOLIO.value)
        if parts:
            return "+".join(parts)
        if self.unsupported_cents:
            return ProvisionLane.UNSUPPORTED.value
        if self.pending_version_cents:
            return ProvisionLane.PENDING_VERSION.value
        if self.pending_receipt_cents:
            return ProvisionLane.PENDING_RECEIPT.value
        return "已结清"

    def __post_init__(self):
        if self.book_cents < 0 or min(
            self.specific_cents,
            self.portfolio_cents,
            self.pending_version_cents,
            self.pending_receipt_cents,
            self.unsupported_cents,
        ) < 0:
            raise ValueError("准备清单金额不能为负")
        if self.exposure_cents != self.book_cents:
            raise ValueError("逐户敞口未与账面未结守恒")
        if self.provision_cents > self.specific_cents + self.portfolio_cents:
            raise ValueError("准备只能来自单户核证与组合评分两侧")


@dataclass(frozen=True)
class ProvisionSheet:
    as_of: date
    batch_id: str
    debtors: tuple[DebtorProvision, ...]
    lines: tuple[Allocation, ...]
    unattached_cents: int
    book_total_cents: int

    def __post_init__(self):
        if self.unattached_cents < 0:
            raise ValueError("未挂账回单不能为负")
        if sum(row.provision_cents for row in self.debtors) != self.provision_total_cents:
            raise ValueError("逐户准备与总额不等")
        if sum(row.book_cents for row in self.debtors) != self.book_total_cents:
            raise ValueError("逐户账面与总额不等")
        if sum(row.exposure_cents for row in self.lines) != self.book_total_cents:
            raise ValueError("计提段落与账面未结不等")

    @property
    def provision_total_cents(self):
        return sum(row.provision_cents for row in self.debtors)

    @property
    def specific_total_cents(self):
        return sum(row.specific_cents for row in self.debtors)

    @property
    def portfolio_total_cents(self):
        return sum(row.portfolio_cents for row in self.debtors)

    @property
    def pending_version_total_cents(self):
        return sum(row.pending_version_cents for row in self.debtors)

    @property
    def pending_receipt_total_cents(self):
        return sum(row.pending_receipt_cents for row in self.debtors)

    @property
    def unsupported_total_cents(self):
        return sum(row.unsupported_cents for row in self.debtors)
