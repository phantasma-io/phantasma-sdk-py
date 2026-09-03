"""Native builders assemble the message only: the fee is planned afterwards from the message, the
form (plain or gas-payer) follows from whether a gas payer was named, and the limits are the
caller's. The build_*_tx_and_sign conveniences plan a fresh message before signing it."""

from __future__ import annotations

import pytest

from phantasma_py import (
    DEFAULT_TX_EXPIRY_MS,
    FeePlanOptions,
    GasConfig,
    PlanAndSignOptions,
    plan_and_sign_with_keys,
    plan_fees,
)
from phantasma_py.carbon import (
    Bytes32,
    IntX,
    PhantasmaNFTMintInfo,
    SignedTxMsg,
    TokenInfo,
    TxLimits,
    TxMsgBurnFungible,
    TxMsgBurnNonFungible,
    TxMsgBurnNonFungibleGasPayer,
    TxMsgMintFungible,
    TxMsgTransferFungible,
    TxMsgTransferFungibleGasPayer,
    TxMsgTransferNonFungibleMulti,
    TxMsgTransferNonFungibleMultiGasPayer,
    TxMsgTransferNonFungibleSingle,
    TxType,
    build_burn_fungible_tx,
    build_burn_non_fungible_tx,
    build_create_token_series_tx,
    build_create_token_series_tx_and_sign,
    build_create_token_tx,
    build_create_token_tx_and_sign,
    build_create_token_tx_and_sign_hex,
    build_mint_fungible_tx,
    build_mint_phantasma_non_fungible_single_tx,
    build_mint_phantasma_non_fungible_single_tx_and_sign,
    build_mint_phantasma_non_fungible_tx,
    build_mint_phantasma_non_fungible_tx_and_sign,
    build_phantasma_nft_rom,
    build_series_info,
    build_token_info,
    build_token_metadata,
    build_transfer_fungible_tx,
    build_transfer_non_fungible_tx,
    bytes32_from_public_key,
    default_expiry,
    deserialize,
    expiry_within,
    now_unix_millis,
    prepare_standard_token_schemas,
    sign_tx_msg,
)
from phantasma_py.crypto import PhantasmaKeys
from phantasma_py.errors import BuilderError

ICON = (
    "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR4nGMAAQAABQABDQottAAAAABJRU5ErkJggg=="
)
PAYER_KEYS = PhantasmaKeys.from_wif("KwPpBSByydVKqStGHAnZzQofCqhDmD2bfRgc9BmZqM3ZmsdWJw4d")
OWNER_KEYS = PhantasmaKeys.from_wif("KwVG94yjfVg1YKFyRxAGtug93wdRbmLnqqrFV6Yd2CiA9KZDAp4H")
PAYER = bytes32_from_public_key(PAYER_KEYS.public_key)
OWNER = bytes32_from_public_key(OWNER_KEYS.public_key)


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


def repeated(byte: int) -> Bytes32:
    return Bytes32(bytes([byte]) * 32)


def token_info(owner: Bytes32) -> TokenInfo:
    metadata = build_token_metadata(
        {"name": "Planned", "icon": ICON, "url": "https://example.com", "description": "a planned token"}
    )
    return build_token_info("PLANNED", IntX(0), is_nft=False, decimals=8, owner=owner, metadata=metadata)


def decode(signed: bytes) -> SignedTxMsg:
    decoded = deserialize(signed, SignedTxMsg)
    assert isinstance(decoded, SignedTxMsg)
    return decoded


def test_builds_a_plain_transfer_paid_by_the_sender_unplanned_until_fees_are_set() -> None:
    msg = build_transfer_fungible_tx(from_address=OWNER, to=PAYER, token_id=1, amount=5)
    assert msg.type == TxType.TRANSFER_FUNGIBLE
    assert msg.gas_from == OWNER
    assert (msg.max_gas, msg.max_data) == (0, 0)
    assert msg.expiry > now_unix_millis(), "the default expiry is in the future"
    assert isinstance(msg.msg, TxMsgTransferFungible)
    assert (msg.msg.to, msg.msg.amount) == (PAYER, 5)
    # A zero offer can never be admitted, so signing an unplanned message is refused.
    with pytest.raises(BuilderError, match="no gas offer"):
        sign_tx_msg(msg, OWNER_KEYS)
    planned = plan_fees(msg, config()).apply(msg)
    assert planned.max_gas == 42_600_000
    assert len(sign_tx_msg(planned, OWNER_KEYS).witnesses) == 1


def test_switches_to_the_gas_payer_form_when_another_account_pays() -> None:
    msg = build_transfer_fungible_tx(
        from_address=OWNER,
        to=repeated(0x33),
        token_id=1,
        amount=5,
        gas_payer=PAYER,
        limits=TxLimits(max_gas=60_000_000),
    )
    assert msg.type == TxType.TRANSFER_FUNGIBLE_GAS_PAYER
    assert msg.gas_from == PAYER
    assert isinstance(msg.msg, TxMsgTransferFungibleGasPayer)
    assert msg.msg.from_address == OWNER
    # Both sign: the payer first, the owner second, as the node reads them.
    signed = sign_tx_msg(msg, OWNER_KEYS, PAYER_KEYS)
    assert [w.address for w in signed.witnesses] == [PAYER, OWNER]


def test_picks_the_single_or_multi_instance_type_by_the_instance_count() -> None:
    owner, payer, receiver = repeated(0x11), repeated(0x22), repeated(0x33)

    def build(gas_payer: Bytes32 | None, instance_ids: list[int]):  # type: ignore[no-untyped-def]
        return build_transfer_non_fungible_tx(
            from_address=owner, to=receiver, token_id=7, instance_ids=instance_ids, gas_payer=gas_payer
        )

    single = build(None, [42])
    assert single.type == TxType.TRANSFER_NON_FUNGIBLE_SINGLE
    assert isinstance(single.msg, TxMsgTransferNonFungibleSingle)
    assert single.msg.instance_id == 42
    multi = build(None, [42, 43])
    assert multi.type == TxType.TRANSFER_NON_FUNGIBLE_MULTI
    assert isinstance(multi.msg, TxMsgTransferNonFungibleMulti)
    assert multi.msg.instance_ids == [42, 43]
    paid_single = build(payer, [42])
    assert paid_single.type == TxType.TRANSFER_NON_FUNGIBLE_SINGLE_GAS_PAYER
    assert paid_single.gas_from == payer
    paid_multi = build(payer, [42, 43])
    assert paid_multi.type == TxType.TRANSFER_NON_FUNGIBLE_MULTI_GAS_PAYER
    assert isinstance(paid_multi.msg, TxMsgTransferNonFungibleMultiGasPayer)
    assert paid_multi.msg.from_address == owner
    with pytest.raises(BuilderError, match="instance_ids"):
        build(None, [])


def test_builds_mints_and_burns_with_the_limits_given() -> None:
    owner, payer, receiver = repeated(0x11), repeated(0x22), repeated(0x33)
    limits = TxLimits(max_gas=1, max_data=2, expiry=3)
    mint = build_mint_fungible_tx(owner=owner, to=receiver, token_id=9, amount=IntX(100), limits=limits)
    assert mint.type == TxType.MINT_FUNGIBLE
    assert mint.gas_from == owner
    assert (mint.max_gas, mint.max_data, mint.expiry) == (1, 2, 3)
    assert isinstance(mint.msg, TxMsgMintFungible)
    assert mint.msg.amount == IntX(100)

    burn = build_burn_fungible_tx(from_address=owner, token_id=9, amount=IntX(1))
    assert burn.type == TxType.BURN_FUNGIBLE
    assert burn.gas_from == owner
    assert isinstance(burn.msg, TxMsgBurnFungible)
    paid_burn = build_burn_fungible_tx(from_address=owner, token_id=9, amount=IntX(1), gas_payer=payer)
    assert paid_burn.type == TxType.BURN_FUNGIBLE_GAS_PAYER
    assert paid_burn.gas_from == payer

    nft_burn = build_burn_non_fungible_tx(from_address=owner, token_id=7, instance_id=42)
    assert nft_burn.type == TxType.BURN_NON_FUNGIBLE
    assert isinstance(nft_burn.msg, TxMsgBurnNonFungible)
    assert nft_burn.msg.instance_id == 42
    paid_nft_burn = build_burn_non_fungible_tx(from_address=owner, token_id=7, instance_id=42, gas_payer=payer)
    assert paid_nft_burn.type == TxType.BURN_NON_FUNGIBLE_GAS_PAYER
    assert isinstance(paid_nft_burn.msg, TxMsgBurnNonFungibleGasPayer)
    assert paid_nft_burn.msg.from_address == owner


def test_stamps_the_default_lifetime_from_now_unless_an_expiry_is_given() -> None:
    before = now_unix_millis()
    expiry = default_expiry()
    after = now_unix_millis()
    assert before + DEFAULT_TX_EXPIRY_MS <= expiry <= after + DEFAULT_TX_EXPIRY_MS
    msg = build_transfer_fungible_tx(from_address=OWNER, to=PAYER, token_id=1, amount=1)
    assert msg.expiry >= before + DEFAULT_TX_EXPIRY_MS
    explicit = build_transfer_fungible_tx(
        from_address=OWNER, to=PAYER, token_id=1, amount=1, limits=TxLimits(max_gas=1, max_data=2, expiry=3)
    )
    assert (explicit.max_gas, explicit.max_data, explicit.expiry) == (1, 2, 3)


def test_expiry_within_uses_the_chains_window_less_a_margin() -> None:
    before = now_unix_millis()
    expiry = expiry_within(3_600_000)
    lifetime = 3_600_000 - 5_000
    assert before + lifetime <= expiry <= now_unix_millis() + lifetime
    custom = expiry_within(60_000, 10_000)
    assert 50_000 <= custom - before <= 51_000
    with pytest.raises(BuilderError, match="leaves nothing"):
        expiry_within(10_000, 10_000)
    with pytest.raises(BuilderError, match="must be positive"):
        expiry_within(0)
    with pytest.raises(BuilderError, match="must not be negative"):
        expiry_within(60_000, -1)


def test_create_token_and_sign_plans_the_message_before_signing() -> None:
    info = token_info(PAYER)
    expected = plan_fees(build_create_token_tx(info, PAYER), config(), FeePlanOptions(witness_count=1))
    signed = build_create_token_tx_and_sign(info, PAYER_KEYS, config())
    assert len(signed) == expected.envelope_bytes
    decoded = decode(signed)
    assert (decoded.msg.max_gas, decoded.msg.max_data) == (expected.max_gas, expected.max_data)
    as_hex = build_create_token_tx_and_sign_hex(
        info, PAYER_KEYS, config(), PlanAndSignOptions(limits=TxLimits(expiry=decoded.msg.expiry))
    )
    assert as_hex == signed.hex()


def test_keeps_an_offer_the_caller_fixed_and_needs_no_config_for_it() -> None:
    info = token_info(PAYER)
    signed = build_create_token_tx_and_sign(
        info, PAYER_KEYS, None, PlanAndSignOptions(limits=TxLimits(max_gas=55_000_000, max_data=7))
    )
    decoded = decode(signed)
    assert (decoded.msg.max_gas, decoded.msg.max_data) == (55_000_000, 7)
    with pytest.raises(BuilderError, match="gas config"):
        build_create_token_tx_and_sign(info, PAYER_KEYS)


def test_series_and_phantasma_mint_conveniences_plan_their_calls() -> None:
    series = build_series_info(7, 0, 0, PAYER)
    series_signed = build_create_token_series_tx_and_sign(9, series, PAYER_KEYS, config())
    series_plan = plan_fees(build_create_token_series_tx(9, series, PAYER), config(), FeePlanOptions(witness_count=1))
    assert decode(series_signed).msg.max_gas == series_plan.max_gas

    schemas = prepare_standard_token_schemas(False)
    rom = build_phantasma_nft_rom(
        schemas.rom,
        [
            ("name", "Planned"),
            ("description", "planned"),
            ("imageURL", "https://example.com/i.png"),
            ("infoURL", "https://example.com"),
            ("royalties", 10_000_000),
        ],
    )
    facts = PlanAndSignOptions(facts=FeePlanOptions(duplicated_series=False, supply_row_exists=True))
    mint_signed = build_mint_phantasma_non_fungible_single_tx_and_sign(
        9, 7, PAYER_KEYS, OWNER, rom, b"", config(), facts
    )
    unsigned = build_mint_phantasma_non_fungible_single_tx(9, 7, PAYER, OWNER, rom, b"")
    expected = plan_fees(
        unsigned, config(), FeePlanOptions(witness_count=1, duplicated_series=False, supply_row_exists=True)
    )
    assert decode(mint_signed).msg.max_gas == expected.max_gas
    assert len(mint_signed) == expected.envelope_bytes
    multi_signed = build_mint_phantasma_non_fungible_tx_and_sign(
        9, PAYER_KEYS, OWNER, [PhantasmaNFTMintInfo(IntX(7), rom, b"")], config(), facts
    )
    assert len(multi_signed) == len(mint_signed)
    with pytest.raises(BuilderError, match="at least one instance"):
        build_mint_phantasma_non_fungible_tx(9, PAYER, OWNER, [])


def test_plan_and_sign_sizes_the_witnesses_from_the_message_or_the_keys() -> None:
    msg = build_transfer_fungible_tx(from_address=OWNER, to=repeated(0x33), token_id=1, amount=5, gas_payer=PAYER)
    signed = plan_and_sign_with_keys(msg, [OWNER_KEYS, PAYER_KEYS], config())
    assert len(signed) == 170 + 32 + 64

    call = build_create_token_tx(token_info(PAYER), PAYER)
    two = plan_and_sign_with_keys(call, [PAYER_KEYS, OWNER_KEYS], config())
    one = plan_and_sign_with_keys(call, [PAYER_KEYS], config())
    assert len(two) - len(one) == 96, "two keys size two witnesses"
