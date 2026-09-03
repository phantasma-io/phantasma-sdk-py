"""A fee plan rendered in the units a person reads."""

from __future__ import annotations

from phantasma_py import FeePlan, FeePlanSummary, NativeFeeKind, summarize_fee_plan, summarize_fee_plan_with_decimals

PLAN = FeePlan(
    kind=NativeFeeKind.TRANSFER_FUNGIBLE,
    envelope_bytes=170,
    max_gas=42_850_000,
    max_data=200_000,
    expected_gas_bill=42_850_000,
    new_storage_quanta=1,
    deleted_storage_quanta=0,
)


def test_renders_the_plan_in_kcal_and_soul() -> None:
    assert summarize_fee_plan(PLAN) == FeePlanSummary("0.004285", "0.004285", "0.002")


def test_takes_the_decimals_of_a_chain_whose_tokens_differ() -> None:
    assert summarize_fee_plan_with_decimals(PLAN, 8, 10) == FeePlanSummary("0.4285", "0.4285", "0.00002")
