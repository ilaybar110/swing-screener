"""Metric -> ordered fallback XBRL tag list.

Each metric resolves against a list of `TagSpec`s in priority order: the parser
(src/data/fundamentals.py) tries each tag in turn and uses the first one that has
any data in a given companyfacts payload. `is_proxy=True` tags are not the exact
concept requested but a reasonable stand-in (e.g. pre-tax income from continuing
operations as a proxy for operating income when the operating-income line itself
isn't tagged). Callers should record `is_proxy` on the resulting row so downstream
consumers (guardrail, valuation) can discount proxy-derived values if they choose.

`total_debt` is not a single tag lookup -- it is assembled from several
instantaneous balance-sheet tags (see `LONG_TERM_DEBT_TAGS` etc.) by
`src/data/fundamentals.py`, because no single XBRL concept reliably captures
"total debt" across filers.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TagSpec:
    taxonomy: str  # "us-gaap" or "dei"
    tag: str
    is_proxy: bool = False


# --- Duration (flow) metrics ------------------------------------------------

REVENUE_TAGS: list[TagSpec] = [
    TagSpec("us-gaap", "Revenues"),
    TagSpec("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax"),
    TagSpec("us-gaap", "RevenueFromContractWithCustomerIncludingAssessedTax"),
    TagSpec("us-gaap", "SalesRevenueNet"),
    # Common older/segment variants seen in the wild.
    TagSpec("us-gaap", "SalesRevenueGoodsNet"),
    TagSpec("us-gaap", "SalesRevenueServicesNet"),
    TagSpec("us-gaap", "InterestAndDividendIncomeOperating", is_proxy=True),  # banks/REITs
    TagSpec("us-gaap", "RegulatedAndUnregulatedOperatingRevenue"),  # utilities
]

OPERATING_INCOME_TAGS: list[TagSpec] = [
    TagSpec("us-gaap", "OperatingIncomeLoss"),
    TagSpec(
        "us-gaap",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        is_proxy=True,
    ),
    TagSpec(
        "us-gaap",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
        is_proxy=True,
    ),
]

OPERATING_CASH_FLOW_TAGS: list[TagSpec] = [
    TagSpec("us-gaap", "NetCashProvidedByUsedInOperatingActivities"),
    TagSpec("us-gaap", "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"),
]

CAPEX_TAGS: list[TagSpec] = [
    TagSpec("us-gaap", "PaymentsToAcquirePropertyPlantAndEquipment"),
    TagSpec("us-gaap", "PaymentsToAcquireProductiveAssets"),
    TagSpec("us-gaap", "PaymentsForCapitalImprovements", is_proxy=True),
]

# --- Instant (point-in-time) metrics ----------------------------------------

SHARES_OUTSTANDING_TAGS: list[TagSpec] = [
    TagSpec("dei", "EntityCommonStockSharesOutstanding"),
    TagSpec("us-gaap", "CommonStockSharesOutstanding"),
    TagSpec("us-gaap", "WeightedAverageNumberOfDilutedSharesOutstanding", is_proxy=True),
    TagSpec("us-gaap", "WeightedAverageNumberOfSharesOutstandingBasic", is_proxy=True),
]

CASH_TAGS: list[TagSpec] = [
    TagSpec("us-gaap", "CashAndCashEquivalentsAtCarryingValue"),
    TagSpec(
        "us-gaap",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
        is_proxy=True,
    ),
    TagSpec("us-gaap", "Cash", is_proxy=True),
    TagSpec(
        "us-gaap",
        "CashAndCashEquivalentsAtCarryingValueIncludingDiscontinuedOperations",
        is_proxy=True,
    ),
]

# Sub-components used to assemble total_debt (see src/data/fundamentals.py).
LONG_TERM_DEBT_TAGS: list[TagSpec] = [
    TagSpec("us-gaap", "LongTermDebt"),
]
LONG_TERM_DEBT_NONCURRENT_TAGS: list[TagSpec] = [
    TagSpec("us-gaap", "LongTermDebtNoncurrent"),
]
LONG_TERM_DEBT_CURRENT_TAGS: list[TagSpec] = [
    TagSpec("us-gaap", "LongTermDebtCurrent"),
]
LONG_TERM_DEBT_AND_CAPITAL_LEASE_TAGS: list[TagSpec] = [
    TagSpec("us-gaap", "LongTermDebtAndCapitalLeaseObligations", is_proxy=True),
]
SHORT_TERM_BORROWINGS_TAGS: list[TagSpec] = [
    TagSpec("us-gaap", "ShortTermBorrowings"),
]
DEBT_CURRENT_TAGS: list[TagSpec] = [
    TagSpec("us-gaap", "DebtCurrent"),
]

# Metrics resolved by a straightforward tag-fallback lookup (see
# fundamentals.resolve_tag). "total_debt" is intentionally absent: it is a
# composite handled by fundamentals._resolve_total_debt.
METRIC_TAGS: dict[str, list[TagSpec]] = {
    "revenue": REVENUE_TAGS,
    "operating_income": OPERATING_INCOME_TAGS,
    "operating_cash_flow": OPERATING_CASH_FLOW_TAGS,
    "capex": CAPEX_TAGS,
    "shares_outstanding": SHARES_OUTSTANDING_TAGS,
    "cash": CASH_TAGS,
}

# Metrics whose facts are duration (flow, need quarterization) vs instant.
DURATION_METRICS = {"revenue", "operating_income", "operating_cash_flow", "capex"}
INSTANT_METRICS = {"shares_outstanding", "cash", "total_debt"}
