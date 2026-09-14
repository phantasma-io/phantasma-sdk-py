"""The client-owned fee planner: one cached gas config per client, read again on request, on
invalidation and on expiry; burns planned for what the NFT holds."""

from __future__ import annotations

import time

from _canned_node import OWNER, PAYER, CannedNode, burn_tx, transfer_tx

from phantasma_py import (
    ChainFeeParams,
    FeePlanOptions,
    InfusedAsset,
    NativeFeeKind,
    PlanRequestOptions,
)
from phantasma_py.carbon import (
    BurnNonFungibleArgs,
    ModuleID,
    SmallString,
    TokenContractMethod,
    TxMsg,
    TxMsgCall,
    TxMsgCallMulti,
    TxType,
    get_nft_address,
    serialize,
)


def test_reads_the_gas_config_once_and_prices_messages_with_it() -> None:
    node = CannedNode()
    client = node.client()
    msg = transfer_tx(OWNER, PAYER)
    first = client.fees.plan(msg)
    second = client.fees.plan(msg)
    assert first.expected_gas_bill == 42_600_000
    assert second.max_gas == 42_600_000
    assert node.gas_config_reads == 1
    assert client.fees.chain_params() == ChainFeeParams(3_600_000, 2_000, 2)
    assert node.gas_config_reads == 1


def test_reads_the_config_again_when_asked_when_invalidated_and_when_the_cache_expires() -> None:
    node = CannedNode()
    client = node.client(fee_config_ttl=0.05)
    msg = transfer_tx(OWNER, PAYER)
    client.fees.config()
    client.fees.config()
    assert node.gas_config_reads == 1
    client.fees.plan(msg, PlanRequestOptions(refresh_config=True))
    assert node.gas_config_reads == 2
    client.fees.invalidate()
    client.fees.config()
    assert node.gas_config_reads == 3
    time.sleep(0.1)
    client.fees.config()
    assert node.gas_config_reads == 4


def test_plans_against_a_config_the_caller_holds_without_touching_the_node() -> None:
    node = CannedNode()
    client = node.client()
    config = client.fees.config()
    plan = client.fees.plan_with(config, transfer_tx(OWNER, PAYER))
    assert plan.expected_gas_bill == 42_600_000
    assert node.gas_config_reads == 1


# A burn is planned for what the NFT holds: read from the node unless the caller stated it.
def test_reads_what_a_burned_nft_holds_unless_the_caller_states_it() -> None:
    node = CannedNode()
    node.tokens["KCAL"] = {"symbol": "KCAL", "carbonId": "1"}
    node.tokens["GPX"] = {"symbol": "GPX", "carbonId": "97"}
    node.fungible[get_nft_address(7, 42).hex()] = [
        {"chain": "main", "symbol": "KCAL", "amount": "1", "decimals": 10},
        {"chain": "main", "symbol": "GPX", "amount": "5", "decimals": 8},
    ]
    client = node.client()
    burn = burn_tx(OWNER, 7, 42)
    holdings = [InfusedAsset(token_id=1), InfusedAsset(token_id=97)]

    read = client.fees.plan(burn)
    assert node.infusion_reads == 1
    stated = client.fees.plan_with(client.fees.config(), burn, FeePlanOptions(infusions=holdings))
    assert stated.expected_gas_bill == read.expected_gas_bill

    empty = client.fees.plan(burn, PlanRequestOptions(facts=FeePlanOptions(infusions=[])))
    assert node.infusion_reads == 1
    assert read.expected_gas_bill - empty.expected_gas_bill == 40 * 10_000


def test_the_planner_is_owned_by_the_client_one_per_client() -> None:
    node = CannedNode()
    client = node.client()
    client.fees.config()
    assert client.fees is client.fees
    other = node.client()
    other.fees.config()
    assert node.gas_config_reads == 2


def test_reads_one_address_per_burned_instance_of_a_batch() -> None:
    """A batch of burns is planned for what EVERY burned NFT holds. The planner reads one address
    per instance, because the fee follows each returned asset separately, and hands the union to the
    pure planner."""
    node = CannedNode()
    node.tokens["KCAL"] = {"symbol": "KCAL", "carbonId": "1"}
    for token_id, instance_id in ((9, 1), (9, 2), (5, 3)):
        node.fungible[get_nft_address(token_id, instance_id).hex()] = [
            {"chain": "main", "symbol": "KCAL", "amount": "1", "decimals": 10}
        ]
    client = node.client()

    def burn_call(token_id: int, instance_ids: list[int]) -> TxMsgCall:
        args = BurnNonFungibleArgs(token_id, OWNER, instance_ids)
        return TxMsgCall(ModuleID.TOKEN, TokenContractMethod.BURN_NON_FUNGIBLE, serialize(args))

    batch = TxMsg(
        TxType.CALL_MULTI,
        1_759_711_416_000,
        0,
        0,
        OWNER,
        SmallString(""),
        TxMsgCallMulti([burn_call(9, [1, 2]), burn_call(5, [3])]),
    )
    options = PlanRequestOptions(facts=FeePlanOptions(witness_count=1))
    plan = client.fees.plan(batch, options)
    assert node.infusion_reads == 3
    assert plan.kinds == (NativeFeeKind.BURN_NON_FUNGIBLE, NativeFeeKind.BURN_NON_FUNGIBLE)

    # The three returned assets are priced once for the batch, on its first burn: one transfer plus
    # one owner-lookup query each, which is 20 work units apiece.
    empty = client.fees.plan_with(client.fees.config(), batch, FeePlanOptions(witness_count=1, infusions=[]))
    assert plan.expected_gas_bill - empty.expected_gas_bill == 3 * 20 * 10_000
