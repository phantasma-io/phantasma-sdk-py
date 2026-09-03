"""Planner tests: plan_fees reads every message fact out of the message, demands the facts it cannot
bound, and prices with the calculator the estimator tests pin to live bills."""

from __future__ import annotations

import dataclasses

import pytest

from phantasma_py import (
    FeePlan,
    FeePlanOptions,
    GasConfig,
    InfusedAsset,
    NativeFeeKind,
    NativeFeeParams,
    estimate_native_fee,
    plan_fees,
)
from phantasma_py.carbon import (
    Bytes32,
    GovernanceContractMethod,
    IntX,
    ModuleID,
    PhantasmaNFTMintInfo,
    RegisterNameArgs,
    SmallString,
    TxLimits,
    TxMsg,
    TxMsgBurnNonFungible,
    TxMsgCall,
    TxMsgCallMulti,
    TxMsgMintNonFungible,
    TxMsgTransferFungible,
    TxMsgTransferFungibleGasPayer,
    TxMsgTransferNonFungibleMulti,
    TxPayload,
    TxType,
    build_and_serialize_token_schemas,
    build_create_token_series_tx,
    build_create_token_tx,
    build_mint_phantasma_non_fungible_tx,
    build_series_info,
    build_token_info,
    build_token_metadata,
    bytes32_from_public_key,
    deserialize,
    envelope_bytes,
    get_nft_address,
    is_nft_address,
    serialize,
    unpack_nft_address,
)
from phantasma_py.crypto import PhantasmaKeys
from phantasma_py.errors import BuilderError

ICON = (
    "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR4nGMAAQAABQABDQottAAAAABJRU5ErkJggg=="
)
CREATOR_KEYS = PhantasmaKeys.from_wif("KwPpBSByydVKqStGHAnZzQofCqhDmD2bfRgc9BmZqM3ZmsdWJw4d")
RECEIVER_KEYS = PhantasmaKeys.from_wif("KwVG94yjfVg1YKFyRxAGtug93wdRbmLnqqrFV6Yd2CiA9KZDAp4H")
CREATOR = bytes32_from_public_key(CREATOR_KEYS.public_key)
RECEIVER = bytes32_from_public_key(RECEIVER_KEYS.public_key)
EXPIRY = 1_759_711_416_000


def config() -> GasConfig:
    return GasConfig(
        version=1,
        max_name_length=255,
        max_token_symbol_length=255,
        fee_multiplier=10_000,
        gas_token_id=1,
        data_token_id=2,
        minimum_gas_offer=10,
        data_escrow_per_row=200_000,
        legacy_data_escrow_per_row=2,
        minimum_gas_bill=10_000_000,
        gas_fee_transfer=10,
        gas_fee_query=10,
        gas_fee_create_token_base=10_000_000_000,
        gas_fee_create_token_symbol=10_000_000_000,
        gas_fee_create_token_series=2_500_000_000,
        gas_fee_per_byte=250_000,
        gas_fee_register_name=10_000_000_000_000,
        gas_burn_ratio_mul=1,
        policy_fee_create_token_base=100_000_000_000_000,
        policy_fee_create_token_symbol=100_000_000_000_000,
        policy_fee_create_token_series=25_000_000_000_000,
        policy_fee_register_name=100_000_000_000_000_000,
    )


def base_tx(tx_type: TxType, gas_from: Bytes32, msg: TxPayload) -> TxMsg:
    return TxMsg(tx_type, EXPIRY, 0, 0, gas_from, SmallString(""), msg)


def transfer(to: Bytes32, token_id: int) -> TxMsg:
    return base_tx(TxType.TRANSFER_FUNGIBLE, CREATOR, TxMsgTransferFungible(to, token_id, 100_000_000))


def create_token(symbol: str, is_nft: bool, extra: dict[str, str] | None = None) -> TxMsg:
    fields = {"name": "Planned", "icon": ICON, "url": "https://example.com", "description": "a planned token"}
    fields.update(extra or {})
    info = build_token_info(
        symbol,
        IntX(0),
        is_nft=is_nft,
        decimals=8,
        owner=CREATOR,
        metadata=build_token_metadata(fields),
        token_schemas=build_and_serialize_token_schemas() if is_nft else b"",
    )
    return build_create_token_tx(info, CREATOR, TxLimits(expiry=EXPIRY))


def phantasma_mint(series_ids: list[int], rom_bytes: int, to: Bytes32) -> TxMsg:
    tokens = [PhantasmaNFTMintInfo(IntX(series), bytes([7]) * rom_bytes, b"") for series in series_ids]
    return build_mint_phantasma_non_fungible_tx(9, CREATOR, to, tokens, TxLimits(expiry=EXPIRY))


def block_data(plan: FeePlan) -> int:
    return (plan.envelope_bytes + plan.new_storage_quanta) * 25 * 10_000


ONE_WITNESS = FeePlanOptions(witness_count=1)


# A native transfer is planned from the message alone: its signed size with one signature, the
# recipient read from the message, the bill the calculator gives for those facts.
def test_plans_a_native_transfer_from_the_message() -> None:
    msg = transfer(RECEIVER, 1)
    plan = plan_fees(msg, config())
    assert plan.kind is NativeFeeKind.TRANSFER_FUNGIBLE
    assert plan.envelope_bytes == envelope_bytes(msg) == 170
    assert plan.expected_gas_bill == 42_600_000
    assert plan.max_gas == 42_600_000
    assert plan.max_data == 0

    applied = plan.apply(msg)
    assert (applied.max_gas, applied.max_data) == (plan.max_gas, plan.max_data)
    assert msg.max_gas == 0, "apply leaves the input untouched"
    assert plan.quote().expected_gas_bill == plan.expected_gas_bill


# The plan equals the calculator's answer for the facts the message states, so a change in either
# shows up here as a disagreement rather than as a silently different number.
def test_agrees_with_the_calculator_for_the_facts_it_reads() -> None:
    msg = transfer(RECEIVER, 97)
    plan = plan_fees(msg, config(), FeePlanOptions(recipient_holds_token=True))
    direct = estimate_native_fee(
        NativeFeeKind.TRANSFER_FUNGIBLE,
        config(),
        NativeFeeParams(envelope_bytes=170, token_id=97, recipient_holds_token=True),
    )
    assert plan.quote() == direct.quote()
    assert plan.new_storage_quanta == direct.new_storage_quanta


# A gas-payer transfer fixes its own two witnesses: the plan sizes both signatures without being
# told, and a stated count that disagrees is refused rather than priced.
def test_sizes_a_gas_payer_transfer_for_both_of_its_signatures() -> None:
    msg = base_tx(TxType.TRANSFER_FUNGIBLE_GAS_PAYER, CREATOR, TxMsgTransferFungibleGasPayer(RECEIVER, RECEIVER, 1, 5))
    plan = plan_fees(msg, config())
    assert plan.envelope_bytes == 170 + 32 + 64
    assert plan_fees(msg, config(), FeePlanOptions(witness_count=2)) == plan
    with pytest.raises(BuilderError, match="witness"):
        plan_fees(msg, config(), FeePlanOptions(witness_count=1))


# A Call chooses its own witnesses, so the count is demanded; each witness is 96 billed bytes.
def test_demands_the_witness_count_of_a_call_and_bills_each_witness() -> None:
    msg = create_token("PLAN", False)
    with pytest.raises(BuilderError, match="witness_count"):
        plan_fees(msg, config())
    one = plan_fees(msg, config(), FeePlanOptions(witness_count=1))
    two = plan_fees(msg, config(), FeePlanOptions(witness_count=2))
    assert two.envelope_bytes - one.envelope_bytes == 96
    assert two.expected_gas_bill - one.expected_gas_bill == 96 * 25 * 10_000


# CreateToken: the symbol length, the NFT flag, the token-info row (the Call arguments as submitted)
# and the metadata keys that add rows or lookups are all read from the call.
def test_reads_a_token_creation_out_of_its_call() -> None:
    fungible = create_token("PLANNED", False)
    assert isinstance(fungible.msg, TxMsgCall)
    plan = plan_fees(fungible, config(), ONE_WITNESS)
    assert plan.kind is NativeFeeKind.CREATE_TOKEN
    direct = estimate_native_fee(
        NativeFeeKind.CREATE_TOKEN,
        config(),
        NativeFeeParams(envelope_bytes=plan.envelope_bytes, symbol_length=7, token_info_bytes=len(fungible.msg.args)),
    )
    assert plan.quote() == direct.quote()
    assert plan.new_storage_quanta == 3

    nft = plan_fees(create_token("PLANNED", True), config(), ONE_WITNESS)
    assert nft.new_storage_quanta == 4, "an NFT token adds its series counter row"

    # The staking keys cost a lookup each; the pre-burn and inflation keys write a row each. The
    # metadata grows by the key it carries, which the token-info row and the envelope both carry;
    # take that growth out to see the lookup alone.
    plain = plan_fees(create_token("PLANNED", False), config(), ONE_WITNESS)
    org = plan_fees(create_token("PLANNED", False, {"_soi": "1"}), config(), ONE_WITNESS)
    reward = plan_fees(create_token("PLANNED", False, {"_srt": "1"}), config(), ONE_WITNESS)
    assert (org.expected_gas_bill - block_data(org)) - (plain.expected_gas_bill - block_data(plain)) == 100_000
    assert (reward.expected_gas_bill - block_data(reward)) - (plain.expected_gas_bill - block_data(plain)) == 100_000
    pre_burn = plan_fees(create_token("PLANNED", False, {"_brn": "1"}), config(), ONE_WITNESS)
    assert pre_burn.new_storage_quanta == plain.new_storage_quanta + 1
    inflation = plan_fees(create_token("PLANNED", False, {"_ip": "1"}), config(), ONE_WITNESS)
    assert inflation.new_storage_quanta == plain.new_storage_quanta + 1


# CreateTokenSeries: the row is the SeriesInfo after the 8-byte token id; the meta-id flag is chain
# state passed through.
def test_reads_a_series_creation_out_of_its_call() -> None:
    info = build_series_info(5, 0, 0, CREATOR)
    msg = build_create_token_series_tx(9, info, CREATOR, TxLimits(expiry=EXPIRY))
    assert isinstance(msg.msg, TxMsgCall)
    plan = plan_fees(msg, config(), FeePlanOptions(witness_count=1, series_has_meta_id=True))
    assert plan.kind is NativeFeeKind.CREATE_TOKEN_SERIES
    direct = estimate_native_fee(
        NativeFeeKind.CREATE_TOKEN_SERIES,
        config(),
        NativeFeeParams(
            envelope_bytes=plan.envelope_bytes, series_info_bytes=len(msg.msg.args) - 8, series_has_meta_id=True
        ),
    )
    assert plan.quote() == direct.quote()
    assert plan.new_storage_quanta == 3


# A Phantasma mint: the count, every instance's ROM size, the distinct series the instances name and
# the NFT-address recipient are read from the call; the series mode is passed through.
def test_reads_a_phantasma_mint_out_of_its_call() -> None:
    options = FeePlanOptions(witness_count=1, duplicated_series=True, supply_row_exists=True)
    one_series = plan_fees(phantasma_mint([5, 5, 5], 75, RECEIVER), config(), options)
    assert one_series.kind is NativeFeeKind.MINT_PHANTASMA_NON_FUNGIBLE
    three_series = plan_fees(phantasma_mint([5, 6, 7], 75, RECEIVER), config(), options)
    assert three_series.envelope_bytes == one_series.envelope_bytes
    assert three_series.expected_gas_bill - one_series.expected_gas_bill == 2 * 10 * 10_000, (
        "one supply read per series"
    )
    direct = estimate_native_fee(
        NativeFeeKind.MINT_PHANTASMA_NON_FUNGIBLE,
        config(),
        NativeFeeParams(
            envelope_bytes=one_series.envelope_bytes,
            token_id=9,
            count=3,
            rom_bytes=[75, 75, 75],
            ram_bytes=[0, 0, 0],
            duplicated_series=True,
            distinct_series_count=1,
            supply_row_exists=True,
        ),
    )
    assert one_series.quote() == direct.quote()

    infused = plan_fees(phantasma_mint([5, 5, 5], 75, get_nft_address(9, 1)), config(), options)
    assert infused.expected_gas_bill - one_series.expected_gas_bill == 100_000, (
        "an NFT-address recipient costs one lookup"
    )


# A native NFT mint carries its ROM and RAM; both are rows, both are read from the message.
def test_reads_a_native_nft_mint_out_of_the_message() -> None:
    msg = base_tx(
        TxType.MINT_NON_FUNGIBLE, CREATOR, TxMsgMintNonFungible(7, RECEIVER, 1, bytes([1]) * 1100, bytes([2]) * 30)
    )
    plan = plan_fees(msg, config())
    assert plan.kind is NativeFeeKind.MINT_NON_FUNGIBLE
    direct = estimate_native_fee(
        NativeFeeKind.MINT_NON_FUNGIBLE,
        config(),
        NativeFeeParams(envelope_bytes=plan.envelope_bytes, token_id=7, rom_bytes=[1100], ram_bytes=[30]),
    )
    assert plan.quote() == direct.quote()
    assert plan.new_storage_quanta == direct.new_storage_quanta


# A multi-instance transfer counts its instances from the message; an empty list is refused.
def test_counts_the_instances_of_a_multi_transfer() -> None:
    def multi(instance_ids: list[int]) -> TxMsg:
        return base_tx(
            TxType.TRANSFER_NON_FUNGIBLE_MULTI, CREATOR, TxMsgTransferNonFungibleMulti(RECEIVER, 7, instance_ids)
        )

    plan = plan_fees(multi([1, 2]), config())
    assert plan.kind is NativeFeeKind.TRANSFER_NON_FUNGIBLE
    assert plan.deleted_storage_quanta == 2
    assert plan.new_storage_quanta == 3
    with pytest.raises(BuilderError, match="at least one instance"):
        plan_fees(multi([]), config())


# A burn returns whatever the NFT holds, which no default can bound: the plan demands the list, an
# empty list states that the address holds nothing, and each asset is priced as its return.
def test_demands_what_a_burned_nft_holds() -> None:
    msg = base_tx(TxType.BURN_NON_FUNGIBLE, CREATOR, TxMsgBurnNonFungible(7, 42))
    with pytest.raises(BuilderError, match="infusions"):
        plan_fees(msg, config())
    empty = plan_fees(msg, config(), FeePlanOptions(infusions=[]))
    assert empty.kind is NativeFeeKind.BURN_NON_FUNGIBLE
    infused = plan_fees(msg, config(), FeePlanOptions(infusions=[InfusedAsset(token_id=1)]))
    assert infused.expected_gas_bill - empty.expected_gas_bill == 20 * 10_000


# Transfers into an NFT-derived address pay the owner lookup of the target; the address form is read
# from the message, never stated by the caller.
def test_prices_the_owner_lookup_of_an_nft_address_recipient() -> None:
    plain = plan_fees(transfer(RECEIVER, 1), config())
    infused = plan_fees(transfer(get_nft_address(9, 1), 1), config())
    assert infused.expected_gas_bill - plain.expected_gas_bill == 100_000


# Calls the model does not price are budgeted as scripts; a raw Phantasma transaction cannot be
# planned at all.
def test_budgets_unmodelled_calls_as_scripts_and_refuses_raw_transactions() -> None:
    unknown = base_tx(TxType.CALL, CREATOR, TxMsgCall(ModuleID.TOKEN, 999, bytes(40)))
    options = FeePlanOptions(witness_count=1, script_storage_quanta=0)
    plan = plan_fees(unknown, config(), options)
    assert plan.kind is NativeFeeKind.SCRIPT
    assert plan.expected_gas_bill == (5000 + (plan.envelope_bytes + 512) * 25) * 10_000
    multi = base_tx(TxType.CALL_MULTI, CREATOR, TxMsgCallMulti([]))
    assert plan_fees(multi, config(), options).kind is NativeFeeKind.SCRIPT


# RegisterName is a governance call whose arguments the plan reads for the name length.
def test_reads_a_name_registration_out_of_its_call() -> None:
    args = RegisterNameArgs(CREATOR, SmallString("planned-name"))
    encoded = serialize(args)
    assert deserialize(encoded, RegisterNameArgs) == args
    msg = base_tx(TxType.CALL, CREATOR, TxMsgCall(ModuleID.GOVERNANCE, GovernanceContractMethod.REGISTER_NAME, encoded))
    plan = plan_fees(msg, config(), FeePlanOptions(witness_count=2))
    assert plan.kind is NativeFeeKind.REGISTER_NAME
    direct = estimate_native_fee(
        NativeFeeKind.REGISTER_NAME, config(), NativeFeeParams(envelope_bytes=plan.envelope_bytes, name_length=12)
    )
    assert plan.quote() == direct.quote()
    assert plan.max_data == 0


def test_recognises_nft_addresses_by_their_form() -> None:
    address = get_nft_address(9, 42)
    assert is_nft_address(address)
    assert unpack_nft_address(address) == (9, 42)
    assert not is_nft_address(get_nft_address(0, 42))
    assert not is_nft_address(get_nft_address(9, 0))
    assert not is_nft_address(CREATOR)
    assert not is_nft_address(Bytes32())


def test_apply_keeps_every_other_field() -> None:
    msg = transfer(RECEIVER, 1)
    applied = plan_fees(msg, config()).apply(msg)
    assert dataclasses.replace(applied, max_gas=0, max_data=0) == msg
