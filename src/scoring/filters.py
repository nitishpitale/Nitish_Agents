"""
Liquidity and quality filters applied before scoring.

All logic is DETERMINISTIC — filters are pure functions of the contract data.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import List, NamedTuple

from ..ingest.models import OptionContract

logger = logging.getLogger(__name__)


class FilterConfig(NamedTuple):
    max_bid_ask_spread_pct: float = 0.10
    min_open_interest: int = 200
    min_days_to_expiry: int = 3
    max_days_to_expiry: int = 180


class FilterResult(NamedTuple):
    passed: bool
    reason: str  # empty string if passed


def _days_to_expiry(contract: OptionContract, as_of: date) -> int:
    return (contract.expiry - as_of).days


def filter_contract(
    contract: OptionContract,
    as_of: date,
    cfg: FilterConfig,
) -> FilterResult:
    """
    Apply all filters to a single contract.

    Returns a FilterResult indicating pass/fail and the failure reason.
    """
    # --- Bid/ask completeness ---
    if contract.bid is None or contract.ask is None:
        return FilterResult(False, "missing bid/ask")

    mid = contract.mid
    if mid is None or mid <= 0:
        return FilterResult(False, "zero or negative mid price")

    # --- Bid-ask spread ---
    spread_pct = contract.bid_ask_spread_pct
    if spread_pct is None:
        return FilterResult(False, "cannot compute bid-ask spread pct")
    if spread_pct > cfg.max_bid_ask_spread_pct:
        return FilterResult(
            False,
            f"bid-ask spread {spread_pct:.2%} > max {cfg.max_bid_ask_spread_pct:.2%}",
        )

    # --- Open interest ---
    if contract.open_interest < cfg.min_open_interest:
        return FilterResult(
            False,
            f"OI {contract.open_interest} < min {cfg.min_open_interest}",
        )

    # --- Days to expiry ---
    dte = _days_to_expiry(contract, as_of)
    if dte < cfg.min_days_to_expiry:
        return FilterResult(False, f"DTE {dte} < min {cfg.min_days_to_expiry}")
    if dte > cfg.max_days_to_expiry:
        return FilterResult(False, f"DTE {dte} > max {cfg.max_days_to_expiry}")

    return FilterResult(True, "")


def apply_filters(
    contracts: List[OptionContract],
    as_of: date,
    cfg: FilterConfig,
) -> tuple[List[OptionContract], dict[str, int]]:
    """
    Apply all filters and return (passed_contracts, filter_counts).

    filter_counts maps rejection reason → count for monitoring.
    """
    passed: List[OptionContract] = []
    rejection_counts: dict[str, int] = {}

    for contract in contracts:
        result = filter_contract(contract, as_of, cfg)
        if result.passed:
            passed.append(contract)
        else:
            rejection_counts[result.reason] = rejection_counts.get(result.reason, 0) + 1
            logger.debug("Filtered out %s: %s", contract.contract_id, result.reason)

    logger.info(
        "Filtering: %d/%d contracts passed (%s rejected)",
        len(passed),
        len(contracts),
        dict(rejection_counts),
    )
    return passed, rejection_counts
