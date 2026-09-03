"""Fee calculator tests. Every v2 expectation below is a bill a real transaction was actually charged
on a gas-model-v2 network, read back from its settlement: a failure here means the SDK disagrees
with the chain, not that a number was chosen badly. The same fixtures and expectations exist in
every SDK, so a divergence between SDKs shows up as a failure in one of them."""

from __future__ import annotations

import dataclasses

import pytest

from phantasma_py import (
    GasConfig,
    InfusedAsset,
    NativeFeeEstimate,
    NativeFeeKind,
    NativeFeeParams,
    envelope_bytes_for,
    estimate_native_fee,
    phantasma_canonical_rom_bytes,
    storage_quanta_for,
)
from phantasma_py.errors import BuilderError


def v1_config() -> GasConfig:
    """The live mainnet v1 configuration: multiplier 10000, shift 0, transfer 10 units, byte fee
    250000 kcal-base, minimum offer 10, escrow 2 atoms per row."""
    return GasConfig(
        version=0,
        max_name_length=32,
        max_token_symbol_length=10,
        fee_shift=0,
        fee_multiplier=10_000,
        gas_token_id=1,
        data_token_id=2,
        minimum_gas_offer=10,
        data_escrow_per_row=2,
        gas_fee_transfer=10,
        gas_fee_query=10,
        gas_fee_create_token_base=10_000_000_000,
        gas_fee_create_token_symbol=10_000_000_000,
        gas_fee_create_token_series=2_500_000_000,
        gas_fee_per_byte=250_000,
        gas_fee_register_name=10_000_000_000_000,
        gas_burn_ratio_mul=1,
    )


def v2_config() -> GasConfig:
    """The mainnet / testnet gas-model-v2 configuration (special resolutions #79 and #68)."""
    return dataclasses.replace(
        v1_config(),
        version=1,
        max_name_length=255,
        max_token_symbol_length=255,
        data_escrow_per_row=200_000,
        legacy_data_escrow_per_row=2,
        minimum_gas_bill=10_000_000,
        policy_fee_create_token_base=100_000_000_000_000,
        policy_fee_create_token_symbol=100_000_000_000_000,
        policy_fee_create_token_series=25_000_000_000_000,
        policy_fee_register_name=100_000_000_000_000_000,
    )


def localnet_config() -> GasConfig:
    """The localnet configuration of the 2026-08-28 live run: policy fees at the 10 KCAL scale,
    escrow 50,000 atoms per row, everything else as mainnet."""
    return dataclasses.replace(
        v2_config(),
        data_escrow_per_row=50_000,
        policy_fee_create_token_base=100_000_000_000,
        policy_fee_create_token_symbol=100_000_000_000,
        policy_fee_create_token_series=25_000_000_000,
        policy_fee_register_name=1_000_000_000_000,
    )


def estimate(kind: NativeFeeKind, config: GasConfig, **params: object) -> NativeFeeEstimate:
    return estimate_native_fee(kind, config, NativeFeeParams(**params))  # type: ignore[arg-type]


# A native SOUL or KCAL transfer is 170 signed bytes and escrows nothing, even into an address that
# has never held the token, because the gas and data token balance rows are free.
def test_bills_a_native_gas_or_data_token_transfer_for_its_envelope_alone() -> None:
    for token_id in (1, 2):
        got = estimate(NativeFeeKind.TRANSFER_FUNGIBLE, v2_config(), envelope_bytes=170, token_id=token_id)
        assert got.expected_gas_bill == 42_600_000
        assert got.max_gas == 42_600_000
        assert got.max_data == 0
        assert got.new_storage_quanta == 0


# The same envelope moving an ordinary token into a fresh holder creates one paid balance row, which
# the chain adds to the byte count and escrows at the row price.
def test_bills_and_escrows_the_fresh_balance_row_of_an_ordinary_token_transfer() -> None:
    fresh = estimate(NativeFeeKind.TRANSFER_FUNGIBLE, v2_config(), envelope_bytes=170, token_id=97)
    assert fresh.expected_gas_bill == 42_850_000
    assert fresh.max_data == 200_000
    held = estimate(
        NativeFeeKind.TRANSFER_FUNGIBLE, v2_config(), envelope_bytes=170, token_id=97, recipient_holds_token=True
    )
    assert held.expected_gas_bill == 42_600_000
    assert held.max_data == 0


# Mainnet tx 6C1A6412... (block 9,075,820): a PoltergeistLite transfer of 178 bytes was billed
# 44,600,000 - the figure the node printed in its "gas fees" abort.
def test_reproduces_the_first_live_mainnet_bill() -> None:
    got = estimate(NativeFeeKind.TRANSFER_FUNGIBLE, v2_config(), envelope_bytes=178, token_id=1)
    assert got.expected_gas_bill == 44_600_000


# A native MintFungible of 171 bytes into a fresh holder of a token whose supply row is in place. The
# call returns the new balance, and a call result is block data exactly like the envelope. The
# result's size depends on the balance it reports: 9 bytes while it fits int64, up to 33 beyond - so
# stating big_fungible=False prices the 9-byte result exactly, and leaving it unstated prices the
# 33-byte maximum, 24 bytes of block data more.
def test_bills_a_fungible_mint_with_its_nine_byte_result_when_the_token_is_declared_small() -> None:
    exact = estimate(
        NativeFeeKind.MINT_FUNGIBLE,
        v2_config(),
        envelope_bytes=171,
        token_id=97,
        big_fungible=False,
        supply_row_exists=True,
    )
    assert exact.expected_gas_bill == 45_350_000
    assert exact.max_data == 200_000
    defaulted = estimate(
        NativeFeeKind.MINT_FUNGIBLE, v2_config(), envelope_bytes=171, token_id=97, supply_row_exists=True
    )
    assert defaulted.expected_gas_bill - exact.expected_gas_bill == 24 * 25 * 10_000


# An NFT transfer of 170 bytes into a fresh holder. The owner's lookup row is deleted and the
# recipient's created (net zero), the fresh balance row is the one net quantum; both new rows are
# escrowed, the deleted row's deposit comes back at its own price.
def test_bills_an_nft_transfer_for_its_net_rows_and_escrows_its_new_ones() -> None:
    got = estimate(NativeFeeKind.TRANSFER_NON_FUNGIBLE, v2_config(), envelope_bytes=170, token_id=7)
    assert got.expected_gas_bill == 42_850_000
    assert got.new_storage_quanta == 2
    assert got.deleted_storage_quanta == 1
    assert got.max_data == 400_000


# The same transfer into an NFT-derived address (an infusion) pays one query fee more for the owner
# check of the target; every mint kind pays the same lookup once per call.
def test_adds_the_query_fee_of_an_infusion_target() -> None:
    got = estimate(
        NativeFeeKind.TRANSFER_NON_FUNGIBLE, v2_config(), envelope_bytes=170, token_id=7, to_is_nft_address=True
    )
    assert got.expected_gas_bill == 42_950_000
    for kind in (
        NativeFeeKind.MINT_FUNGIBLE,
        NativeFeeKind.MINT_NON_FUNGIBLE,
        NativeFeeKind.MINT_PHANTASMA_NON_FUNGIBLE,
    ):
        plain = estimate(kind, v2_config(), envelope_bytes=200, token_id=7, rom_bytes=[10])
        infused = estimate(kind, v2_config(), envelope_bytes=200, token_id=7, rom_bytes=[10], to_is_nft_address=True)
        assert infused.expected_gas_bill - plain.expected_gas_bill == 100_000, kind


# A native NFT burn of 138 bytes; the mint's ten quanta are deleted and refunded, no net block data,
# the token had been burned before and its supply row was in place.
def test_bills_an_nft_burn_for_its_work_and_envelope_only() -> None:
    got = estimate(
        NativeFeeKind.BURN_NON_FUNGIBLE,
        v2_config(),
        envelope_bytes=138,
        token_id=7,
        rom_bytes=[3067 * 2 + 36],
        rom_has_meta_id=True,
        token_burned_before=True,
        supply_row_exists=True,
    )
    assert got.expected_gas_bill == 34_800_000
    assert got.deleted_storage_quanta == 10
    assert got.new_storage_quanta == 0
    assert got.max_data == 0


# Burning an NFT returns whatever its own address holds, and the chain charges for each returned
# asset as the transfers it performs: a transfer fee plus the owner lookup of the NFT-address source
# per fungible token; an instance query, a transfer per instance and that lookup per NFT token. The
# returned rows never add block data - a burn refunds more than the returns create - so the bill
# moves by the work alone, while the escrow ceiling covers a balance row the burner lacks and the
# moved lookup rows. Measured live 2026-09-02: +200,000 for one infused KCAL atom, +700,000 for
# KCAL, a custom token and an NFT together.
def test_prices_the_assets_a_burned_nft_returns() -> None:
    def burn(infusions: list[InfusedAsset]) -> NativeFeeEstimate:
        return estimate(
            NativeFeeKind.BURN_NON_FUNGIBLE,
            v2_config(),
            envelope_bytes=138,
            token_id=7,
            rom_bytes=[3067 * 2 + 36],
            token_burned_before=True,
            supply_row_exists=True,
            infusions=infusions,
        )

    empty = burn([])
    assert empty.expected_gas_bill == 34_800_000

    # One fungible token: a transfer and a query. The gas token's rows are free, so nothing else.
    kcal = burn([InfusedAsset(token_id=1)])
    assert kcal.expected_gas_bill - empty.expected_gas_bill == 20 * 10_000
    assert kcal.new_storage_quanta == empty.new_storage_quanta
    assert kcal.deleted_storage_quanta == empty.deleted_storage_quanta

    # A custom token the burner does not hold: the same work, plus the balance row the return
    # creates - and the NFT address's own row, which the return deletes.
    custom = burn([InfusedAsset(token_id=97)])
    assert custom.expected_gas_bill - empty.expected_gas_bill == 20 * 10_000
    assert custom.new_storage_quanta == empty.new_storage_quanta + 1
    assert custom.deleted_storage_quanta == empty.deleted_storage_quanta + 1
    assert custom.max_data == empty.max_data + v2_config().data_escrow_per_row
    held = burn([InfusedAsset(token_id=97, burner_holds_token=True)])
    assert held.new_storage_quanta == empty.new_storage_quanta
    # An id the reader could not resolve is priced as a paid row: over-covering, never short.
    assert burn([InfusedAsset()]).new_storage_quanta == empty.new_storage_quanta + 1

    # Two instances of an NFT token: the instance query, two transfers, the lookup; a balance row
    # plus two moved lookup rows created, the NFT address's balance row and two lookups deleted.
    nft = burn([InfusedAsset(token_id=9, non_fungible=True, instance_count=2)])
    assert nft.expected_gas_bill - empty.expected_gas_bill == 40 * 10_000
    assert nft.new_storage_quanta == empty.new_storage_quanta + 3
    assert nft.deleted_storage_quanta == empty.deleted_storage_quanta + 3

    # The live combination: KCAL, a held custom token and one NFT - seventy units.
    everything = burn(
        [
            InfusedAsset(token_id=1),
            InfusedAsset(token_id=97, burner_holds_token=True),
            InfusedAsset(token_id=9, non_fungible=True, instance_count=1, burner_holds_token=True),
        ]
    )
    assert everything.expected_gas_bill - empty.expected_gas_bill == 70 * 10_000

    with pytest.raises(BuilderError, match="instance_count"):
        estimate(
            NativeFeeKind.BURN_NON_FUNGIBLE,
            v2_config(),
            envelope_bytes=138,
            token_id=7,
            infusions=[InfusedAsset(token_id=9, non_fungible=True, instance_count=0)],
        )


# Two settled CreateToken bills with 7-character symbols. The fungible one is 374 signed bytes and
# writes the symbol, token-info and null-balance rows and returns the u64 token id; the NFT-capable
# one (466 bytes) adds the series counter. Policy fee 10 KCAL + 10 KCAL >> 6.
def test_reproduces_the_two_localnet_create_token_bills_to_the_atom() -> None:
    # The Call arguments ARE the serialized TokenInfo, so their length is the envelope minus the
    # 100-byte transaction header and the 58 bytes the Call header and witness array add.
    fungible = estimate(
        NativeFeeKind.CREATE_TOKEN,
        localnet_config(),
        envelope_bytes=374,
        symbol_length=7,
        token_info_bytes=374 - 100 - 58,
    )
    assert fungible.expected_gas_bill == 101_658_750_000
    assert fungible.new_storage_quanta == 3
    assert fungible.max_data == 150_000

    nft = estimate(
        NativeFeeKind.CREATE_TOKEN,
        localnet_config(),
        envelope_bytes=466,
        symbol_length=7,
        token_info_bytes=466 - 100 - 58,
        non_fungible=True,
    )
    assert nft.expected_gas_bill == 101_682_000_000
    assert nft.new_storage_quanta == 4
    assert nft.max_data == 200_000


# Validating token metadata that names a staking organisation looks the organisation up, and
# metadata that names a reward token reads that token: one query fee each, on top of the policy fee
# and the rows.
def test_charges_the_lookups_that_staking_metadata_costs_a_token_creation() -> None:
    def create(org: bool, reward: bool) -> NativeFeeEstimate:
        return estimate(
            NativeFeeKind.CREATE_TOKEN,
            localnet_config(),
            envelope_bytes=374,
            symbol_length=7,
            token_info_bytes=374 - 100 - 58,
            has_staking_organisation=org,
            has_staking_reward_token=reward,
        )

    plain = create(False, False)
    assert create(True, False).expected_gas_bill - plain.expected_gas_bill == 100_000
    assert create(False, True).expected_gas_bill - plain.expected_gas_bill == 100_000
    both = create(True, True)
    assert both.expected_gas_bill - plain.expected_gas_bill == 200_000
    assert both.new_storage_quanta == plain.new_storage_quanta


# A settled CreateTokenSeries of 246 bytes with a `_i` in its metadata writes the series info, its
# supply and the meta-id lookup and returns the u32 series id.
def test_reproduces_the_localnet_create_token_series_bill() -> None:
    got = estimate(
        NativeFeeKind.CREATE_TOKEN_SERIES,
        localnet_config(),
        envelope_bytes=246,
        # As above, less the 8-byte token id that precedes the SeriesInfo in the Call arguments.
        series_info_bytes=246 - 100 - 58 - 8,
        series_has_meta_id=True,
    )
    assert got.expected_gas_bill == 25_063_250_000
    assert got.new_storage_quanta == 3


# Two settled deterministic Phantasma mints. The 182-byte public ROM was the token's first mint to
# that owner (fresh balance row, 5 quanta); the 3,083-byte one found the balance row in place and its
# canonical ROM alone took seven quanta (10 in total). Each instance pays the mint plus two query fees
# and returns 40 bytes after the 4-byte count.
#
# Both were minted into a UNIQUE series of a token whose supply row was in place, and every
# settled-bill case says both explicitly: the series mode and the supply row are chain state the
# message does not carry, so the calculator assumes the costlier reading of each when nobody tells
# it. Leaving either out here would compare the chain's receipt against a deliberate over-estimate.
def test_reproduces_the_two_localnet_phantasma_nft_mint_bills() -> None:
    small = estimate(
        NativeFeeKind.MINT_PHANTASMA_NON_FUNGIBLE,
        localnet_config(),
        envelope_bytes=413,
        token_id=9,
        rom_bytes=[182],
        duplicated_series=False,
        supply_row_exists=True,
    )
    assert small.expected_gas_bill == 115_800_000
    assert small.new_storage_quanta == 5
    assert small.max_data == 250_000

    large = estimate(
        NativeFeeKind.MINT_PHANTASMA_NON_FUNGIBLE,
        localnet_config(),
        envelope_bytes=3314,
        token_id=9,
        rom_bytes=[3083],
        recipient_holds_token=True,
        duplicated_series=False,
        supply_row_exists=True,
    )
    assert large.expected_gas_bill == 842_300_000
    assert large.new_storage_quanta == 10
    assert large.max_data == 500_000


# The same model at a different network's prices, including two 166-byte mints of the same size where
# only the first paid for the owner's balance row.
def test_reproduces_the_four_testnet_phantasma_nft_mint_bills() -> None:
    for envelope, rom, held, bill, quanta in [
        (398, 167, False, 112_050_000, 5),
        (3298, 3067, True, 838_300_000, 10),
        (397, 166, False, 111_800_000, 5),
        (397, 166, True, 111_550_000, 4),
    ]:
        got = estimate(
            NativeFeeKind.MINT_PHANTASMA_NON_FUNGIBLE,
            v2_config(),
            envelope_bytes=envelope,
            token_id=9,
            rom_bytes=[rom],
            recipient_holds_token=held,
            duplicated_series=False,
            supply_row_exists=True,
        )
        assert got.expected_gas_bill == bill
        assert got.new_storage_quanta == quanta


# A sweep of settled mints across the quantum boundary (public ROM bytes -> quanta, the first mint
# also paying for the balance row): 268 -> 5, 968 -> 5, 1168 -> 6, 3068 -> 10, 5068 -> 13. 968 and
# 1168 straddle the 1024-byte boundary, which is where a wrong ROM model shows up.
def test_reproduces_the_rom_size_sweep_quanta() -> None:
    for rom, held, quanta in [(268, False, 5), (968, True, 5), (1168, True, 6), (3068, True, 10), (5068, True, 13)]:
        got = estimate(
            NativeFeeKind.MINT_PHANTASMA_NON_FUNGIBLE,
            v2_config(),
            envelope_bytes=1000,
            rom_bytes=[rom],
            recipient_holds_token=held,
            supply_row_exists=True,
        )
        assert got.new_storage_quanta == quanta, rom


# Several instances in one transaction: the work units, the per-instance query fees, the call result
# bytes and the per-instance storage all scale with the count, while the recipient's balance row is
# paid for exactly once. A settled three-instance Phantasma mint of 75-byte public ROMs, 490 signed
# bytes: 13 quanta are one balance row plus, per instance, one ROM row and the three fixed rows a
# deterministic mint writes.
def test_scales_a_multi_instance_phantasma_mint_by_its_instance_count() -> None:
    three = estimate(
        NativeFeeKind.MINT_PHANTASMA_NON_FUNGIBLE,
        localnet_config(),
        envelope_bytes=490,
        count=3,
        rom_bytes=[75],
        duplicated_series=False,
        supply_row_exists=True,
    )
    assert three.expected_gas_bill == 157_650_000
    assert three.new_storage_quanta == 13
    assert three.max_data == 650_000
    single = estimate(
        NativeFeeKind.MINT_PHANTASMA_NON_FUNGIBLE,
        localnet_config(),
        envelope_bytes=490,
        count=1,
        rom_bytes=[75],
        duplicated_series=False,
        supply_row_exists=True,
    )
    assert single.new_storage_quanta == 5


# A duplicated series is read twice per instance rather than once - the mode check and the shared
# ROM each read the token info - and its supply is read once per series for the whole transaction,
# because the chain reuses the number it already read. The counts therefore scale differently and
# the model has to keep them apart.
def test_charges_a_duplicated_series_three_queries_an_instance_and_one_for_the_series() -> None:
    def mint(**extra: object) -> NativeFeeEstimate:
        return estimate(
            NativeFeeKind.MINT_PHANTASMA_NON_FUNGIBLE,
            localnet_config(),
            envelope_bytes=490,
            count=3,
            rom_bytes=[75],
            **extra,
        )

    one_series = mint(duplicated_series=True, distinct_series_count=1)
    unique = mint(duplicated_series=False)
    # Three extra instance queries and one supply query over the unique-series bill.
    assert one_series.expected_gas_bill - unique.expected_gas_bill == 40 * 10_000
    three_series = mint(duplicated_series=True, distinct_series_count=3)
    assert three_series.expected_gas_bill - one_series.expected_gas_bill == 20 * 10_000
    # A series count nobody could have minted is a bookkeeping error, not a price.
    with pytest.raises(BuilderError, match="distinct_series_count"):
        mint(duplicated_series=True, distinct_series_count=4)


# A settled two-instance NFT transfer of 182 signed bytes: each instance deletes the owner's lookup
# row and creates the recipient's, so only the recipient's new balance row is billed, while the
# escrow ceiling still covers all three rows the transaction may create.
def test_scales_a_multi_instance_nft_transfer_by_its_instance_count() -> None:
    got = estimate(NativeFeeKind.TRANSFER_NON_FUNGIBLE, localnet_config(), envelope_bytes=182, token_id=181, count=2)
    assert got.expected_gas_bill == 45_950_000
    assert got.new_storage_quanta == 3
    assert got.deleted_storage_quanta == 2
    assert got.max_data == 150_000


# A settled RegisterName of a 10-character name, 213 signed bytes. Governance rows are free data, so
# the bill is the policy fee (10,000,000 KCAL >> 9) plus the envelope and nothing else.
def test_reproduces_the_testnet_register_name_bill() -> None:
    got = estimate(NativeFeeKind.REGISTER_NAME, v2_config(), envelope_bytes=213, name_length=10)
    assert got.expected_gas_bill == 195_312_553_250_000
    assert got.max_data == 0


# A tiny v2 tx can never bill below the consensus floor; the offer must also respect the admission
# check max_gas >= minimum_gas_bill.
def test_applies_the_minimum_bill_floor() -> None:
    config = dataclasses.replace(v2_config(), minimum_gas_bill=10_000_000_000)
    got = estimate(NativeFeeKind.TRANSFER_FUNGIBLE, config, envelope_bytes=170, token_id=1)
    assert got.expected_gas_bill == 10_000_000_000
    assert got.max_gas == 10_000_000_000


# Facts about chain state that the message cannot carry are defaulted to the case that COSTS MORE,
# because the offer is spent against the real bill and a short one aborts the transaction while an
# over-offer is refunded. These pin that direction; each case fails if a default flips back.
def test_assumes_a_phantasma_series_is_duplicated_until_told_otherwise() -> None:
    def mint(**extra: object) -> NativeFeeEstimate:
        return estimate(
            NativeFeeKind.MINT_PHANTASMA_NON_FUNGIBLE,
            localnet_config(),
            envelope_bytes=490,
            count=3,
            rom_bytes=[75],
            **extra,
        )

    assumed = mint()
    duplicated = mint(duplicated_series=True)
    unique = mint(duplicated_series=False)
    # Three extra instance queries plus one supply query: 40 units at this config's multiplier.
    assert duplicated.expected_gas_bill - unique.expected_gas_bill == 40 * 10_000
    assert assumed.expected_gas_bill == duplicated.expected_gas_bill


def test_assumes_a_minted_rom_carries_a_meta_id_which_is_a_row_it_must_escrow_for() -> None:
    def mint(**extra: object) -> NativeFeeEstimate:
        return estimate(
            NativeFeeKind.MINT_NON_FUNGIBLE, v2_config(), envelope_bytes=300, token_id=97, rom_bytes=[64], **extra
        )

    assumed = mint()
    with_meta_id = mint(rom_has_meta_id=True)
    without = mint(rom_has_meta_id=False)
    assert with_meta_id.new_storage_quanta == without.new_storage_quanta + 1
    assert assumed.new_storage_quanta == with_meta_id.new_storage_quanta
    # max_data is the one that has to be right: the chain aborts a transaction whose escrow exceeds
    # it, so a row assumed away is not an under-offer but a failed transaction.
    assert assumed.max_data == with_meta_id.max_data
    assert assumed.max_data - without.max_data == v2_config().data_escrow_per_row


# Same flag, same default on a burn - it keeps the deleted rows mirroring what the mint wrote - but
# there it moves a reported number and nothing else. Deleted rows are refunded, max_data covers only
# the rows an operation CREATES, and a burn always deletes more than it creates, so the block-data
# term floors at zero whichever way the flag goes. This pins both halves: the count follows the
# flag, the price does not.
def test_counts_a_burned_meta_id_row_but_does_not_price_on_it() -> None:
    def burn(**extra: object) -> NativeFeeEstimate:
        return estimate(
            NativeFeeKind.BURN_NON_FUNGIBLE, v2_config(), envelope_bytes=300, token_id=97, rom_bytes=[64], **extra
        )

    assumed = burn()
    without = burn(rom_has_meta_id=False)
    assert assumed.deleted_storage_quanta == without.deleted_storage_quanta + 1
    assert assumed.quote() == without.quote()


def test_assumes_a_created_series_carries_a_meta_id_which_is_one_more_row() -> None:
    assumed = estimate(NativeFeeKind.CREATE_TOKEN_SERIES, v2_config(), envelope_bytes=300, series_info_bytes=100)
    without = estimate(
        NativeFeeKind.CREATE_TOKEN_SERIES,
        v2_config(),
        envelope_bytes=300,
        series_info_bytes=100,
        series_has_meta_id=False,
    )
    assert assumed.new_storage_quanta == without.new_storage_quanta + 1


# The supply row disappears when its balance reaches exactly zero - a limited token fully in
# circulation, an unlimited one with nothing outstanding - and the next mint or burn recreates it.
# Unstated, every mint and burn prices that recreation: one quantum in the bill and the escrow
# ceiling. Transfers never touch the row, and the chain's own gas and data tokens are free rows, so
# neither moves with the flag.
def test_assumes_the_supply_row_must_be_recreated_on_mints_and_burns() -> None:
    for kind in (
        NativeFeeKind.MINT_FUNGIBLE,
        NativeFeeKind.BURN_FUNGIBLE,
        NativeFeeKind.MINT_NON_FUNGIBLE,
        NativeFeeKind.MINT_PHANTASMA_NON_FUNGIBLE,
        NativeFeeKind.BURN_NON_FUNGIBLE,
    ):
        assumed = estimate(kind, v2_config(), envelope_bytes=300, token_id=97, rom_bytes=[64])
        in_place = estimate(kind, v2_config(), envelope_bytes=300, token_id=97, rom_bytes=[64], supply_row_exists=True)
        assert assumed.new_storage_quanta == in_place.new_storage_quanta + 1, kind
        assert assumed.max_data - in_place.max_data == v2_config().data_escrow_per_row, kind
        # An NFT burn deletes more quanta than it creates, so its block-data term floors at zero
        # either way and only the escrow ceiling moves; everywhere else the bill moves too.
        want = 0 if kind is NativeFeeKind.BURN_NON_FUNGIBLE else 25 * 10_000
        assert assumed.expected_gas_bill - in_place.expected_gas_bill == want, kind

    assert estimate(NativeFeeKind.TRANSFER_FUNGIBLE, v2_config(), envelope_bytes=300, token_id=97) == estimate(
        NativeFeeKind.TRANSFER_FUNGIBLE, v2_config(), envelope_bytes=300, token_id=97, supply_row_exists=True
    )
    assert estimate(NativeFeeKind.BURN_FUNGIBLE, v2_config(), envelope_bytes=300, token_id=1) == estimate(
        NativeFeeKind.BURN_FUNGIBLE, v2_config(), envelope_bytes=300, token_id=1, supply_row_exists=True
    )


def test_requires_the_envelope_size_under_gas_model_v2() -> None:
    with pytest.raises(BuilderError, match="envelope_bytes is required"):
        estimate_native_fee(NativeFeeKind.TRANSFER_FUNGIBLE, v2_config())


# v1 transfer with an existing recipient row: bill is the pure work term 10 * 10000.
def test_v1_bills_work_only_for_a_transfer_to_an_existing_recipient() -> None:
    got = estimate(NativeFeeKind.TRANSFER_FUNGIBLE, v1_config(), recipient_holds_token=True)
    assert got.expected_gas_bill == 100_000
    # stdFee shape: 2x min offer + work + flat 1 KiB byte allowance.
    assert got.max_gas == 10 * 2 + 100_000 + 1024 * 250_000
    assert got.max_data == 0


# v1 transfer default (worst case: 1 fresh row): the row quantum joins the byte fee and the escrow
# shows up in max_data at the v1 price.
def test_v1_includes_one_fresh_row_by_default() -> None:
    got = estimate_native_fee(NativeFeeKind.TRANSFER_FUNGIBLE, v1_config())
    assert got.expected_gas_bill == 100_000 + 250_000
    assert got.max_data == 2


# CreateToken under v1 charges unit-priced product fees through the multiplier; the 8-byte result
# and the rows are block data at the v1 byte price.
def test_v1_prices_token_creation_through_the_multiplier() -> None:
    got = estimate(NativeFeeKind.CREATE_TOKEN, v1_config(), symbol_length=4, token_info_bytes=200)
    work = (10_000_000_000 + 1_250_000_000) * 10_000
    assert got.expected_gas_bill == work + (8 + 3) * 250_000


# RegisterName halves the price per character after the first, under both models.
def test_halves_the_name_price_per_character_under_both_models() -> None:
    v1 = estimate(NativeFeeKind.REGISTER_NAME, v1_config(), name_length=8)
    v2 = estimate(NativeFeeKind.REGISTER_NAME, v2_config(), name_length=8, envelope_bytes=300)
    assert v1.expected_gas_bill == (10_000_000_000_000 >> 7) * 10_000
    assert v2.expected_gas_bill == (100_000_000_000_000_000 >> 7) + 300 * 25 * 10_000


# fee_shift semantics: the chain clamps shifts >= 64 to a zero work delta; the estimator must match
# rather than undercharge/overcharge.
def test_zeroes_scaled_terms_on_an_oversized_fee_shift() -> None:
    config = dataclasses.replace(v1_config(), fee_shift=64)
    got = estimate(NativeFeeKind.TRANSFER_FUNGIBLE, config, recipient_holds_token=True)
    assert got.expected_gas_bill == 0


# The SCRIPT kind budgets a generous VM unit allowance (default 5000 exceeds every script in mainnet
# history), event bytes and storage rows instead of pretending opcode costs are closed-form.
def test_budgets_a_vm_allowance_for_scripts() -> None:
    got = estimate(NativeFeeKind.SCRIPT, v2_config(), envelope_bytes=568, script_storage_quanta=0)
    # (5000 vm units + (568 + 512 events) * 25) * 10000
    assert got.expected_gas_bill == (5000 + 1080 * 25) * 10_000
    defaulted = estimate(NativeFeeKind.SCRIPT, v2_config(), envelope_bytes=568)
    assert defaulted.new_storage_quanta == 4


# Envelope arithmetic mirrors SignedTxMsg: native kinds append bare 64-byte signatures, call/script
# kinds append a length-prefixed 96-byte witness array. The witness count is always stated: a fee
# kind cannot tell a two-signature gas-payer transfer from its one-signature form.
def test_envelope_bytes_for_follows_the_witness_layout() -> None:
    assert envelope_bytes_for(NativeFeeKind.TRANSFER_FUNGIBLE, 150, 1) == 150 + 64
    assert envelope_bytes_for(NativeFeeKind.TRANSFER_FUNGIBLE, 150, 2) == 150 + 128
    assert envelope_bytes_for(NativeFeeKind.CREATE_TOKEN, 900, 1) == 900 + 4 + 96
    assert envelope_bytes_for(NativeFeeKind.SCRIPT, 500, 2) == 500 + 4 + 192


def test_measures_rows_in_quanta_and_the_canonical_rom_at_twice_the_public_one() -> None:
    assert storage_quanta_for(0) == 0
    assert storage_quanta_for(1024) == 1
    assert storage_quanta_for(1025) == 2
    assert phantasma_canonical_rom_bytes(182) == 400


# The chain's length-halved fee is defined up to 64 characters, and the calculator refuses to price
# anything longer. A 64-character name is the longest priceable one and must still price.
def test_refuses_to_price_a_name_or_symbol_past_the_length_the_chain_can_shift() -> None:
    longest = estimate(NativeFeeKind.REGISTER_NAME, v2_config(), envelope_bytes=300, name_length=64)
    assert longest.expected_gas_bill > 0
    with pytest.raises(BuilderError, match="cannot be priced offline"):
        estimate(NativeFeeKind.REGISTER_NAME, v2_config(), envelope_bytes=300, name_length=65)
    with pytest.raises(BuilderError, match="cannot be priced offline"):
        estimate(NativeFeeKind.CREATE_TOKEN, v2_config(), envelope_bytes=300, symbol_length=65, token_info_bytes=100)


# Impossible inputs are rejected instead of quoting fees for txs the chain would never admit.
def test_rejects_invalid_inputs() -> None:
    with pytest.raises(BuilderError):
        estimate(NativeFeeKind.TRANSFER_FUNGIBLE, v1_config(), count=0)
    with pytest.raises(BuilderError):
        estimate_native_fee(NativeFeeKind.REGISTER_NAME, v1_config())
    # max_token_symbol_length is 10 on the v1 config.
    with pytest.raises(BuilderError):
        estimate(NativeFeeKind.CREATE_TOKEN, v1_config(), symbol_length=11)
    with pytest.raises(BuilderError, match="one entry per instance"):
        estimate(NativeFeeKind.MINT_NON_FUNGIBLE, v2_config(), envelope_bytes=300, count=2, rom_bytes=[10, 20, 30])
