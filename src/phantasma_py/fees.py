"""Fee calculation under both gas models: the exact bill and storage escrow of a native operation,
reproducing the chain's own settlement arithmetic and the gas each contract path charges."""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import TypeVar

from .carbon import (
    STANDARD_META_TOKEN_INFLATION_PERIOD,
    STANDARD_META_TOKEN_PRE_BURN,
    STANDARD_META_TOKEN_STAKING_ORG_ID,
    STANDARD_META_TOKEN_STAKING_REWARD_TOKEN,
    BurnFungibleArgs,
    BurnNonFungibleArgs,
    CarbonSerializable,
    GasConfig,
    GovernanceContractMethod,
    MintFungibleArgs,
    MintPhantasmaNonFungibleArgs,
    ModuleID,
    RegisterNameArgs,
    TokenContractMethod,
    TokenFlags,
    TokenInfo,
    TransferFungibleArgs,
    TransferNonFungibleArgs,
    TxLimits,
    TxMsg,
    TxMsgBurnFungible,
    TxMsgBurnFungibleGasPayer,
    TxMsgBurnNonFungible,
    TxMsgBurnNonFungibleGasPayer,
    TxMsgCall,
    TxMsgCallMulti,
    TxMsgMintFungible,
    TxMsgMintNonFungible,
    TxMsgPhantasma,
    TxMsgPhantasmaRaw,
    TxMsgTrade,
    TxMsgTransferFungible,
    TxMsgTransferFungibleGasPayer,
    TxMsgTransferNonFungibleMulti,
    TxMsgTransferNonFungibleMultiGasPayer,
    TxMsgTransferNonFungibleSingle,
    TxMsgTransferNonFungibleSingleGasPayer,
    VMDynamicStruct,
    deserialize,
    envelope_bytes,
    is_nft_address,
    required_witnesses,
    sign_and_serialize_tx_msg,
)
from .crypto import PhantasmaKeys
from .errors import BuilderError


class NativeFeeKind(Enum):
    """Native operations the fee calculator models exactly, plus SCRIPT, the budget for everything else."""

    #: Fungible token transfer (TransferFungible and its gas-payer form).
    TRANSFER_FUNGIBLE = "transfer_fungible"
    #: NFT transfer of `count` instances (the single and multi forms and their gas-payer variants).
    TRANSFER_NON_FUNGIBLE = "transfer_non_fungible"
    #: Fungible mint (MintFungible).
    MINT_FUNGIBLE = "mint_fungible"
    #: NFT mint of `count` instances with caller-supplied ROM (MintNonFungible). The ROM is stored
    #: exactly as submitted, unlike a deterministic Phantasma mint.
    MINT_NON_FUNGIBLE = "mint_non_fungible"
    #: Deterministic Phantasma NFT mint of `count` instances (Token.MintPhantasmaNonFungible). The
    #: chain stores a canonical ROM: the public ROM plus the derived Phantasma NFT id plus a copy of
    #: the public ROM, so storage grows at twice the ROM size.
    MINT_PHANTASMA_NON_FUNGIBLE = "mint_phantasma_non_fungible"
    #: Fungible burn (BurnFungible and its gas-payer form).
    BURN_FUNGIBLE = "burn_fungible"
    #: NFT burn of `count` instances (BurnNonFungible and its gas-payer form).
    BURN_NON_FUNGIBLE = "burn_non_fungible"
    #: Token.CreateToken call; set `symbol_length` when a symbol is used.
    CREATE_TOKEN = "create_token"
    #: Token.CreateTokenSeries call.
    CREATE_TOKEN_SERIES = "create_token_series"
    #: Governance.RegisterName call; `name_length` is required.
    REGISTER_NAME = "register_name"
    #: Generic Phantasma VM script transaction (AllowGas/SpendGas pattern: stake, marketplace,
    #: custom contract calls). Script opcode costs depend on chain state and are not closed-form;
    #: the estimate budgets `script_units_allowance` VM work units and `script_event_bytes` of
    #: events on top of the byte fee. For an exact script bill use the node-side estimator
    #: (estimateTransaction).
    SCRIPT = "script"


_BARE_SIGNATURE_KINDS = frozenset(
    {
        NativeFeeKind.TRANSFER_FUNGIBLE,
        NativeFeeKind.TRANSFER_NON_FUNGIBLE,
        NativeFeeKind.MINT_FUNGIBLE,
        NativeFeeKind.MINT_NON_FUNGIBLE,
        NativeFeeKind.BURN_FUNGIBLE,
        NativeFeeKind.BURN_NON_FUNGIBLE,
    }
)

#: Gas model v2 price of block-carried bytes, in gas units per byte. A versioned consensus constant
#: of the v2 gas model, deliberately not part of the on-chain config: it changes only with a new gas
#: model version, so a client can hold it as a constant rather than read it per block.
GAS_MODEL_V2_UNITS_PER_BLOCK_DATA_BYTE = 25

#: Storage is escrowed per 1024-byte quantum of a row's key plus its value.
STORAGE_QUANTUM_BYTES = 1024

#: Serialized size of one witness-array entry (32-byte address + 64-byte signature).
WITNESS_ARRAY_ENTRY_BYTES = 96

#: Serialized size of one bare signature (native TxTypes carry no witness array).
NATIVE_SIGNATURE_BYTES = 64

_U64_MAX = (1 << 64) - 1

# Sizes of the token-module rows a native operation writes, in bytes of key plus value. A row costs
# ceil((key + value) / 1024) quanta; the small fixed rows never leave the first quantum, while the
# ROM-bearing rows are computed from the ROM the caller submits.
_NFT_INSTANCE_ROW_OVERHEAD = 17 + 32 + 8 + 1 + 4  # key + originator + created + flags + ROM length prefix
_NFT_RAM_ROW_OVERHEAD = 17  # key; the RAM is stored bare
_TOKEN_INFO_KEY_BYTES = 9
_SERIES_INFO_KEY_BYTES = 13
# The canonical ROM of a deterministic Phantasma mint: the public ROM fields, plus `_i` (int256, 32
# bytes), plus a `rom` field holding the public ROM again with a 4-byte length prefix.
_PHANTASMA_CANONICAL_ROM_OVERHEAD = 32 + 4
# Fungible mint / burn calls return the resulting balance as an IntX: 1 header + 8 bytes for int64
# balances, up to 1 + 32 for int256 (big-fungible) balances.
_INTX_SMALL_RESULT_BYTES = 9
_INTX_BIG_RESULT_BYTES = 33
# The longest name or symbol this calculator will price. The chain's length-halved policy fee is
# defined up to here; past it no offline price exists, so the calculator refuses rather than quote a
# number the chain may not agree with.
_MAX_PRICEABLE_LENGTH = 64
# Budgets of the SCRIPT kind when the caller states none: a VM work allowance that exceeds every
# script seen in mainnet history (max 3392 units) with margin, the event bytes a script may emit
# (Notify payloads count as block data) and the storage rows it may create.
_DEFAULT_SCRIPT_UNITS_ALLOWANCE = 5000
_DEFAULT_SCRIPT_EVENT_BYTES = 512
_DEFAULT_SCRIPT_STORAGE_QUANTA = 4


@dataclass(slots=True)
class InfusedAsset:
    """An asset held at a burned NFT's own address, which the burn returns to the burner. See
    NativeFeeParams.infusions."""

    #: The token's id. Rows of the chain's gas and data tokens are free, which only the id can tell;
    #: None prices the rows as paid, which can only over-cover the escrow ceiling.
    token_id: int | None = None
    #: An NFT token: the burn returns every instance the address holds (`instance_count`).
    non_fungible: bool = False
    #: Instances of an NFT token the address holds; each is a transfer and a moved lookup row.
    #: None = 1.
    instance_count: int | None = None
    #: The burner already holds this token, so no balance row is created when it comes back. False
    #: (the default) is the costlier reading, which moves the escrow ceiling and never the bill: a
    #: burn refunds more rows than the return creates.
    burner_holds_token: bool = False


@dataclass(slots=True)
class NativeFeeParams:
    """Inputs of estimate_native_fee. Under gas model v2 every byte the transaction puts in the block
    puts in the block and escrows every new storage row. The inputs are therefore the sizes the
    chain will see: the signed envelope, the serialized structures the operation stores, and the
    facts about existing state that decide whether a row is new.

    The inputs are of two kinds, and they are defaulted differently.

    - Facts the CALLER CANNOT KNOW without reading chain state. Examples: whether the recipient
      already holds the token, whether a ROM carries an `_i` id, which mode a series mints in. Each
      defaults to the case that costs MORE. An estimate built from the defaults is then an upper
      bound, and the settlement can only come out below it. The facts whose costlier reading is True
      are `bool | None`, where None means unstated. The others are plain bools whose False is the
      costlier reading.
    - Facts carried by the MESSAGE ITSELF. Examples: the instance count, the serialized sizes,
      whether the token being created is non-fungible or carries `pre_burn`. These are not guesses,
      so they have no safe default. Pass them. plan_fees reads every one of them out of the message.
    """

    #: Full signed transaction size in bytes. This is the envelope the block carries. Required under
    #: gas model v2 (see envelope_bytes and envelope_bytes_for). Ignored under v1, which billed the
    #: payload note alone.
    envelope_bytes: int = 0
    #: Instance count for NFT kinds (transferred, minted or burned instances). None is 1.
    count: int | None = None
    #: Token moved by a transfer, mint or burn. Balance rows of the chain's gas and data tokens are
    #: free, so with the token id known the estimate escrows nothing for them. None prices the rows
    #: as paid.
    token_id: int | None = None
    #: The recipient already holds this token, so its balance row exists and costs nothing. False,
    #: the default, prices a fresh row.
    recipient_holds_token: bool = False
    #: The recipient is an NFT-derived address, which means an infusion. The chain reads that NFT's
    #: owner, and that costs one extra query fee. Transfers and every mint kind pay it. A burn has no
    #: recipient.
    #:
    #: This is a fact of the recipient's address form. It says nothing about chain state. plan_fees
    #: derives it from the message's own recipient with is_nft_address, so only direct callers of
    #: this calculator pass it.
    to_is_nft_address: bool = False
    #: The token's balances can exceed int64. Such a token is called big-fungible.
    #:
    #: A fungible mint or burn answers with the RESULTING balance as a variable-length integer. That
    #: is 9 bytes while the balance fits int64, and up to 33 bytes for an int256 balance. The
    #: resulting balance is chain state, so None prices the 33-byte maximum. The offer then covers
    #: the bill and the difference is refunded. Pass False for an ordinary int64 token and the
    #: estimate is exact.
    big_fungible: bool | None = None
    #: The token has been burned before, so its burnt counter row exists. False, the default, prices
    #: the row the first burn creates.
    token_burned_before: bool = False
    #: The token's supply-tracking row exists. The chain drops that row when its balance reaches
    #: exactly zero, so the row can be absent in two cases. A limited-supply token has its entire
    #: supply in circulation, and the next burn recreates the row. An unlimited token has nothing
    #: outstanding, and the next mint recreates it.
    #:
    #: While this is False, every mint and burn prices the recreation. That is one more storage
    #: quantum in the bill and in the escrow ceiling, and it covers both cases. Pass True for the
    #: exact quote whenever the token is in neither of them. Rows of the chain's gas and data tokens
    #: are free either way.
    supply_row_exists: bool = False
    #: What the burned NFTs hold at their own addresses (BURN_NON_FUNGIBLE). There is one entry per
    #: asset per burned instance.
    #:
    #: The burn returns every one of them to the burner, and the chain charges for each. A fungible
    #: token costs a transfer fee plus the owner-lookup query of the NFT-address source. An NFT token
    #: costs an instance query, a transfer per instance, and that same lookup. A returned token the
    #: burner does not hold also costs the burner's new balance row.
    #:
    #: This is chain state that the message does not carry, and an NFT can hold any number of assets,
    #: so there is no costlier bound to assume. None prices an empty address, because a direct caller
    #: of this calculator states what it knows. plan_fees demands the list, and the RPC-side planner
    #: reads it from the chain.
    infusions: list[InfusedAsset] | None = None
    #: Token symbol length in characters (CREATE_TOKEN). 0 is no symbol.
    symbol_length: int = 0
    #: Serialized TokenInfo length (CREATE_TOKEN). These are the Call arguments, and they become the
    #: token-info row.
    token_info_bytes: int = 0
    #: The token being created is non-fungible (CREATE_TOKEN). It costs one more row, the series
    #: counter.
    non_fungible: bool = False
    #: The token metadata carries `pre_burn` (CREATE_TOKEN). The burnt counter row is then created at
    #: once.
    has_pre_burn: bool = False
    #: The token metadata carries an inflation schedule (CREATE_TOKEN). The next-inflation row is
    #: then created.
    has_inflation_schedule: bool = False
    #: The token metadata names a staking organisation (CREATE_TOKEN). The creation looks that
    #: organisation up, which costs one query fee.
    has_staking_organisation: bool = False
    #: The token metadata names a staking reward token (CREATE_TOKEN). The creation reads that
    #: token's info, which costs one query fee.
    has_staking_reward_token: bool = False
    #: Serialized SeriesInfo length (CREATE_TOKEN_SERIES). These are the Call arguments after the
    #: token id.
    series_info_bytes: int = 0
    #: The series metadata carries a `_i` id (CREATE_TOKEN_SERIES). The meta-id lookup row is then
    #: created. The metadata is schema-encoded like the ROM, so a caller holding only the bytes
    #: cannot tell. None is the reading that pays for the row.
    series_has_meta_id: bool | None = None
    #: Registered name length in characters (REGISTER_NAME). Required for that kind.
    name_length: int = 0
    #: ROM bytes per minted or burned instance (MINT_NON_FUNGIBLE and BURN_NON_FUNGIBLE: as stored;
    #: MINT_PHANTASMA_NON_FUNGIBLE: the public ROM). It takes one entry per instance, or a single
    #: entry that applies to every instance. Empty is 0 bytes.
    rom_bytes: list[int] = field(default_factory=list)
    #: RAM bytes per instance, in the same shape as rom_bytes. Empty is no RAM row.
    ram_bytes: list[int] = field(default_factory=list)
    #: The raw ROM carries a `_i` id, and the chain indexes it in one more row (MINT_NON_FUNGIBLE and
    #: BURN_NON_FUNGIBLE). The ROM is schema-encoded, so a caller holding only the bytes cannot tell.
    #:
    #: None assumes the id. On a mint that is the reading which escrows for the row. On a burn it is
    #: the reading that mirrors what the mint created. A burn never prices on this fact either way
    #: (see NativeFeeEstimate.deleted_storage_quanta). A Phantasma mint always has such an id and
    #: ignores this input.
    rom_has_meta_id: bool | None = None
    #: The series mints duplicated NFTs (MINT_PHANTASMA_NON_FUNGIBLE). A duplicated series costs one
    #: more query fee per instance than a unique one, plus one per distinct series (see
    #: distinct_series_count). A call whose instances mix duplicated and unique series is priced as
    #: if every instance were duplicated.
    #:
    #: None prices the duplicated mode. A series' mode is chain state that the message does not
    #: carry, so only the costlier reading is safe. A duplicated mint priced as unique is short by
    #: exactly those query fees, and the planner offers the bill with no headroom, so the transaction
    #: aborts. Pass False only when the series is known to be unique. The saving is a few query fees.
    duplicated_series: bool | None = None
    #: How many distinct series a duplicated Phantasma mint writes into
    #: (MINT_PHANTASMA_NON_FUNGIBLE with duplicated_series). The chain reads each series' supply once
    #: per transaction, not once per instance, so this is the count of distinct series ids in the
    #: call. It is never more than count. None is 1. Ignored for a unique series, which does not read
    #: the supply at all.
    distinct_series_count: int | None = None
    #: User payload bytes attached to the transaction. Billed under gas model v1 only.
    payload_bytes: int = 0
    #: VM work-unit allowance for the SCRIPT kind. None is 5000, which exceeds every script seen in
    #: mainnet history (max 3392 units) with margin.
    #:
    #: In a CALL_MULTI the allowance counts once per unmodelled call, because each of them can do
    #: that much work. A batch of calls the model does not price therefore offers several times what
    #: it will spend. The difference is refunded, and a caller who knows the calls can bring the
    #: offer down with this field.
    script_units_allowance: int | None = None
    #: Event bytes allowance for the SCRIPT kind (Notify payloads count as block data). None is 512,
    #: per unmodelled call like script_units_allowance.
    script_event_bytes: int | None = None
    #: New storage quanta allowance for the SCRIPT kind. None is 4, per unmodelled call like
    #: script_units_allowance.
    script_storage_quanta: int | None = None


@dataclass(slots=True, frozen=True)
class FeeQuote:
    """A fee quote for one transaction, from the offline calculator or from the node's own estimator.

    Gas values are kcal-base (1 KCAL = 1e10 kcal-base); escrow is in data-token atoms (1 SOUL = 1e8
    atoms).
    """

    #: The gas offer that covers the bill (TxMsg.max_gas). From the offline calculator it is the
    #: bill itself, floored at the chain's minimum offer; unused gas is refunded, so callers wanting
    #: headroom add it on top.
    max_gas: int
    #: The storage-escrow ceiling (TxMsg.max_data): every new row priced at the current row price.
    max_data: int
    #: The bill the chain formula yields for exactly the provided inputs. It is exact for every native
    #: operation when the inputs describe the transaction and the state facts are right. For the
    #: SCRIPT kind it is the budgeted allowance, not a prediction.
    #:
    #: One caveat on "exact": the chain scales each charge as it is made and adds the results, while
    #: this calculator scales their sum. The two agree while fee_shift is zero, which is the case on
    #: every network running the v2 model today; under a non-zero shift the rounding differs, always
    #: in the direction of this calculator quoting a few units MORE than the chain settles, so the
    #: offer stays covering.
    expected_gas_bill: int


@dataclass(slots=True, frozen=True)
class NativeFeeEstimate:
    """Result of an offline fee estimate: the quote plus the storage rows it was computed from."""

    #: See FeeQuote.max_gas.
    max_gas: int
    #: See FeeQuote.max_data.
    max_data: int
    #: See FeeQuote.expected_gas_bill.
    expected_gas_bill: int
    #: Storage quanta the operation creates (1024-byte units per new paid row).
    new_storage_quanta: int
    #: Storage quanta the operation deletes; their escrow is refunded at each row's own price.
    #:
    #: Informational. It does not enter the bill: max_data covers the rows an operation CREATES, and
    #: the block-data term uses the net growth, which an operation that deletes more than it creates
    #: floors at zero either way. A burn's figure is a lower bound, because the stored ROM is chain
    #: state that the message does not carry. Nothing depends on tightening it.
    deleted_storage_quanta: int

    def quote(self) -> FeeQuote:
        """The quote alone, in the shape the node's estimator answers with too."""
        return FeeQuote(self.max_gas, self.max_data, self.expected_gas_bill)


@dataclass(slots=True)
class _OperationModel:
    """Work units, policy fee, result bytes and row changes of one operation."""

    work_units: int = 0
    policy_fee: int = 0
    #: Bytes the Call returns; they are block data like the envelope.
    result_bytes: int = 0
    new_quanta: int = 0
    deleted_quanta: int = 0


@dataclass(slots=True)
class NativeFeePart:
    """One operation of a batched message: the kind it is priced as and the inputs it is priced
    from. See estimate_native_fee_batch."""

    kind: NativeFeeKind
    params: NativeFeeParams = field(default_factory=lambda: NativeFeeParams())


@dataclass(slots=True, frozen=True)
class NativeFeeTransactionParams:
    """The estimate inputs that belong to the transaction itself. No operation inside it owns them,
    because the block carries one envelope however many operations the message performs."""

    #: Full signed transaction size. Required under gas model v2.
    envelope_bytes: int = 0
    #: User payload attached to the transaction. Billed under gas model v1 only.
    payload_bytes: int = 0


def estimate_native_fee(
    kind: NativeFeeKind, config: GasConfig, params: NativeFeeParams | None = None
) -> NativeFeeEstimate:
    """The offline fee calculator: the exact gas bill and storage escrow of a native operation under
    both gas models (selected by GasConfig.version), reproducing the chain's own settlement
    arithmetic and the gas each contract path charges. Any change to those formulas ships as a new
    gas-model version, never silently, which is what makes an offline calculation safe."""
    params = params if params is not None else NativeFeeParams()
    transaction = NativeFeeTransactionParams(envelope_bytes=params.envelope_bytes, payload_bytes=params.payload_bytes)
    return estimate_native_fee_batch([NativeFeePart(kind, params)], config, transaction)


def estimate_native_fee_batch(
    parts: list[NativeFeePart],
    config: GasConfig,
    transaction: NativeFeeTransactionParams | None = None,
) -> NativeFeeEstimate:
    """The fee of a message that performs SEVERAL operations in one transaction. That message is a
    CALL_MULTI. The chain runs its calls in a plain loop, with no per-call surcharge and no batch
    dispatch cost. It accumulates one gas bill, one result buffer and one change set over the whole
    transaction, and it bills the envelope once. A batch therefore costs the sum of its parts over
    work, policy fee, result bytes and rows, settled once.

    Rows are counted per part. Two parts that create the SAME row count it twice. Two burns of one
    token both count its burnt counter, and two transfers into one fresh address both count its
    balance row.

    Carrying "an earlier part already created it" forward is unsound in the direction that matters.
    A burn that empties a supply to exactly zero deletes the supply row again, so a later mint would
    be priced short, abort and be billed. Counting twice raises the escrow ceiling alone, and that is
    refunded."""
    transaction = transaction if transaction is not None else NativeFeeTransactionParams()
    models = [_part_model(part, config) for part in parts]
    return _settle(models, config, transaction)


def _part_model(part: NativeFeePart, config: GasConfig) -> _OperationModel:
    # The model of one operation, with the input check that belongs to every kind. Split out so the
    # single-operation and the batch entry points build their parts the same way.
    count = 1 if part.params.count is None else part.params.count
    if count < 1:
        raise BuilderError("estimate_native_fee: count must be a positive integer")
    return _operation_model(part.kind, config, part.params, count)


def _settle(
    models: list[_OperationModel], config: GasConfig, transaction: NativeFeeTransactionParams
) -> NativeFeeEstimate:
    # Turns the operations a transaction performs into its bill, offer and escrow ceiling. One
    # transaction is settled once. The work, policy fees, result bytes and rows add up. The envelope
    # is counted once. The fee scaling and the minimum-bill floor apply to the total. The chain does
    # the same with the counters it accumulates while the transaction runs.
    total = _OperationModel()
    for model in models:
        total.work_units += model.work_units
        total.policy_fee += model.policy_fee
        total.result_bytes += model.result_bytes
        total.new_quanta += model.new_quanta
        total.deleted_quanta += model.deleted_quanta
    # Only the net growth of paid storage is block data; deleted rows are refunded, not billed.
    net_quanta = max(total.new_quanta - total.deleted_quanta, 0)

    if config.has_gas_model_v2:
        if transaction.envelope_bytes <= 0:
            raise BuilderError("estimate_native_fee: envelope_bytes is required under gas model v2")
        # v2: bill = mul_shift(work + block_data * 25, mult, shift) + policy_fee, floored at
        # minimum_gas_bill, where block_data = envelope + net storage quanta + Call result bytes.
        block_data = transaction.envelope_bytes + net_quanta + total.result_bytes
        byte_units = block_data * GAS_MODEL_V2_UNITS_PER_BLOCK_DATA_BYTE
        bill = _mul_shift(total.work_units + byte_units, config.fee_multiplier, config.fee_shift) + total.policy_fee
        expected = max(min(bill, _U64_MAX), config.minimum_gas_bill)
        max_gas = max(expected, config.minimum_gas_offer)
    else:
        # v1: bill = (work * mult >> shift) + block_data * gas_fee_per_byte, where block_data =
        # payload + Call result bytes + net storage quanta; no envelope term, no floor. The v1
        # product prices ride the work term (see _operation_model).
        work = _mul_shift(total.work_units, config.fee_multiplier, config.fee_shift)
        block_data = transaction.payload_bytes + total.result_bytes + net_quanta
        expected = min(work + block_data * config.gas_fee_per_byte, _U64_MAX)
        # Offer shape mirrors the node's own test agent: a 2x minimum-offer pad plus a flat 1 KiB
        # block-data allowance on top of the work term.
        byte_allowance = max(block_data, 1024)
        max_gas = min(config.minimum_gas_offer * 2 + work + byte_allowance * config.gas_fee_per_byte, _U64_MAX)

    return NativeFeeEstimate(
        max_gas=max_gas,
        max_data=min(total.new_quanta * config.data_escrow_per_row, _U64_MAX),
        expected_gas_bill=expected,
        new_storage_quanta=total.new_quanta,
        deleted_storage_quanta=total.deleted_quanta,
    )


def envelope_bytes_for(kind: NativeFeeKind, serialized_message_length: int, witness_count: int) -> int:
    """Envelope size (signed tx bytes as carried in the block) from an already serialized unsigned
    message length and the number of signers, for callers that hold bytes rather than a message.

    With the message in hand prefer envelope_bytes, which reads the witness count out of the message
    instead of taking it on trust. Witness layout: the native transaction types append bare 64-byte
    signatures, while the call, trade and script types append a length-prefixed array of 32-byte
    address plus 64-byte signature entries.

    witness_count has no default on purpose. A fee kind does not distinguish a gas-payer message,
    which carries two signatures, from the plain form that carries one - both are the same kind - so
    a default of one would silently size a two-signature envelope as a one-signature envelope and
    under-offer the transaction by 64 bytes.
    """
    if serialized_message_length < 0:
        raise BuilderError("serialized_message_length must not be negative")
    if witness_count < 0:
        raise BuilderError("witness_count must not be negative")
    if kind in _BARE_SIGNATURE_KINDS:
        return serialized_message_length + NATIVE_SIGNATURE_BYTES * witness_count
    # CreateToken / CreateTokenSeries / MintPhantasmaNonFungible / RegisterName ride TxType.CALL;
    # SCRIPT rides TxType.PHANTASMA - both carry the witness array form.
    return serialized_message_length + 4 + WITNESS_ARRAY_ENTRY_BYTES * witness_count


def storage_quanta_for(row_bytes: int) -> int:
    """Storage quanta of one row: ceil((key + value) / 1024)."""
    return -(-row_bytes // STORAGE_QUANTUM_BYTES)


def phantasma_canonical_rom_bytes(public_rom_bytes: int) -> int:
    """Bytes of the NFT ROM the chain stores for a deterministic Phantasma mint, from the public ROM
    the caller submits: the chain builds a canonical ROM out of the public fields, the derived
    Phantasma NFT id and a second copy of the public ROM, so storage grows at roughly twice the
    submitted size."""
    return public_rom_bytes * 2 + _PHANTASMA_CANONICAL_ROM_OVERHEAD


def _quantum(value: bool) -> int:
    return 1 if value else 0


def _operation_model(kind: NativeFeeKind, config: GasConfig, params: NativeFeeParams, count: int) -> _OperationModel:
    # Work units, policy fee, result bytes and row changes of each operation. Query fees
    # (gas_fee_query) are charged by the state lookups a contract path makes internally, so they
    # are part of the bill even though nothing in the message mentions them.
    v2 = config.has_gas_model_v2
    free_balance_rows = params.token_id is not None and params.token_id in (config.gas_token_id, config.data_token_id)
    recipient_row = _quantum(not params.recipient_holds_token and not free_balance_rows)
    burnt_row = _quantum(not params.token_burned_before and not free_balance_rows)
    # The supply-tracking row a mint or burn may have to recreate (see NativeFeeParams). Transfers
    # never touch it, and a creation writes it unconditionally, so only mints and burns price it.
    supply_row = _quantum(not params.supply_row_exists and not free_balance_rows)
    big_fungible = True if params.big_fungible is None else params.big_fungible
    balance_result_bytes = _INTX_BIG_RESULT_BYTES if big_fungible else _INTX_SMALL_RESULT_BYTES
    infusion_query = config.gas_fee_query if params.to_is_nft_address else 0

    if kind is NativeFeeKind.TRANSFER_FUNGIBLE:
        return _OperationModel(work_units=config.gas_fee_transfer + infusion_query, new_quanta=recipient_row)
    if kind is NativeFeeKind.TRANSFER_NON_FUNGIBLE:
        # Per instance the owner's lookup row is deleted and the recipient's created; the
        # recipient's balance row may be new as well.
        return _OperationModel(
            work_units=config.gas_fee_transfer * count + infusion_query,
            new_quanta=count + recipient_row,
            deleted_quanta=count,
        )
    if kind is NativeFeeKind.MINT_FUNGIBLE:
        return _OperationModel(
            work_units=config.gas_fee_transfer + infusion_query,
            result_bytes=balance_result_bytes,
            new_quanta=recipient_row + supply_row,
        )
    if kind is NativeFeeKind.BURN_FUNGIBLE:
        return _OperationModel(
            work_units=config.gas_fee_transfer,
            result_bytes=balance_result_bytes,
            new_quanta=burnt_row + supply_row,
        )
    if kind is NativeFeeKind.MINT_NON_FUNGIBLE:
        roms = _per_instance(params.rom_bytes, count, "rom_bytes")
        rams = _per_instance(params.ram_bytes, count, "ram_bytes")
        # Per instance: the instance row (ROM), the owner row, the lookup row, the RAM row when RAM
        # is given, the meta-id row when the ROM carries `_i`; plus the recipient's balance row and
        # the supply row when it must be recreated.
        rom_has_meta_id = True if params.rom_has_meta_id is None else params.rom_has_meta_id
        quanta = recipient_row + supply_row
        for rom, ram in zip(roms, rams, strict=True):
            quanta += storage_quanta_for(_NFT_INSTANCE_ROW_OVERHEAD + rom) + 2
            if ram > 0:
                quanta += storage_quanta_for(_NFT_RAM_ROW_OVERHEAD + ram)
            if rom_has_meta_id:
                quanta += 1
        return _OperationModel(
            work_units=config.gas_fee_transfer * count + infusion_query,
            result_bytes=4 + 8 * count,  # instance count + one u64 instance id each
            new_quanta=quanta,
        )
    if kind is NativeFeeKind.MINT_PHANTASMA_NON_FUNGIBLE:
        roms = _per_instance(params.rom_bytes, count, "rom_bytes")
        rams = _per_instance(params.ram_bytes, count, "ram_bytes")
        # As MINT_NON_FUNGIBLE, with the canonical ROM stored and the meta-id row always present.
        quanta = recipient_row + supply_row
        for rom, ram in zip(roms, rams, strict=True):
            quanta += storage_quanta_for(_NFT_INSTANCE_ROW_OVERHEAD + phantasma_canonical_rom_bytes(rom)) + 3
            if ram > 0:
                quanta += storage_quanta_for(_NFT_RAM_ROW_OVERHEAD + ram)
        # Per instance: the mint itself, the series lookup by meta id, and the token-info read the
        # series-mode check performs. A duplicated series reads the token info a SECOND time per
        # instance, to pick up the series' shared ROM, and reads that series' supply once per
        # distinct series in the call. The chain remembers a supply it has already read, so the
        # supply fee does not scale with the instance count the way the other three do.
        duplicated_series = True if params.duplicated_series is None else params.duplicated_series
        queries_per_instance = 3 if duplicated_series else 2
        series_supply_queries = _distinct_series(params, count) if duplicated_series else 0
        work = (
            (config.gas_fee_transfer + config.gas_fee_query * queries_per_instance) * count
            + config.gas_fee_query * series_supply_queries
            + infusion_query
        )
        return _OperationModel(
            work_units=work,
            result_bytes=4 + 40 * count,  # instance count + (32-byte Phantasma id + u64 instance id) each
            new_quanta=quanta,
        )
    if kind is NativeFeeKind.BURN_NON_FUNGIBLE:
        roms = _per_instance(params.rom_bytes, count, "rom_bytes")
        rams = _per_instance(params.ram_bytes, count, "ram_bytes")
        # The instance, owner, lookup (and RAM, meta-id) rows are deleted and refunded; the burnt
        # counter row is created on the token's first burn, and the supply row when it must be
        # recreated. Each instance's infusion sweep reads the NFT address balances twice, and
        # whatever the sweep finds is returned to the burner and charged as the transfers it takes
        # (see _returned_assets). The deleted rows mirror what the mint created, which is why the
        # meta-id row is counted the same way here. On a burn this
        # total is reported, never billed.
        rom_has_meta_id = True if params.rom_has_meta_id is None else params.rom_has_meta_id
        deleted = 0
        for rom, ram in zip(roms, rams, strict=True):
            deleted += storage_quanta_for(_NFT_INSTANCE_ROW_OVERHEAD + rom) + 2
            if ram > 0:
                deleted += storage_quanta_for(_NFT_RAM_ROW_OVERHEAD + ram)
            if rom_has_meta_id:
                deleted += 1
        returned = _returned_assets(params, config)
        per_instance_work = config.gas_fee_transfer + config.gas_fee_query * 2
        return _OperationModel(
            work_units=per_instance_work * count + returned.work_units,
            new_quanta=burnt_row + supply_row + returned.new_quanta,
            deleted_quanta=deleted + returned.deleted_quanta,
        )
    if kind is NativeFeeKind.CREATE_TOKEN:
        shift = _symbol_shift(params.symbol_length, config.max_token_symbol_length, "symbol_length")
        has_symbol = params.symbol_length > 0
        # Rows: the symbol lookup, the token info, the null-address supply row, the series counter
        # for NFT tokens, the burnt counter with pre_burn, the next-inflation row with a schedule.
        quanta = (
            _quantum(has_symbol)
            + storage_quanta_for(_TOKEN_INFO_KEY_BYTES + params.token_info_bytes)
            + 1
            + _quantum(params.non_fungible)
            + _quantum(params.has_pre_burn)
            + _quantum(params.has_inflation_schedule)
        )
        if v2:
            base, symbol_price = config.policy_fee_create_token_base, config.policy_fee_create_token_symbol
        else:
            base, symbol_price = config.gas_fee_create_token_base, config.gas_fee_create_token_symbol
        symbol = symbol_price >> shift if has_symbol else 0
        # Validating the metadata looks up a staking organisation it names and reads a reward token
        # it names: one query fee each, on top of the policy fee.
        metadata_queries = config.gas_fee_query * (
            _quantum(params.has_staking_organisation) + _quantum(params.has_staking_reward_token)
        )
        return _OperationModel(
            work_units=metadata_queries if v2 else base + symbol + metadata_queries,
            policy_fee=base + symbol if v2 else 0,
            result_bytes=8,  # the new token id, u64
            new_quanta=quanta,
        )
    if kind is NativeFeeKind.CREATE_TOKEN_SERIES:
        # Rows: the series info, the series supply, the meta-id lookup when the metadata has `_i`.
        series_has_meta_id = True if params.series_has_meta_id is None else params.series_has_meta_id
        return _OperationModel(
            work_units=0 if v2 else config.gas_fee_create_token_series,
            policy_fee=config.policy_fee_create_token_series if v2 else 0,
            result_bytes=4,  # the new series id, u32
            new_quanta=storage_quanta_for(_SERIES_INFO_KEY_BYTES + params.series_info_bytes)
            + 1
            + _quantum(series_has_meta_id),
        )
    if kind is NativeFeeKind.REGISTER_NAME:
        if params.name_length <= 0:
            raise BuilderError("estimate_native_fee: name_length is required for REGISTER_NAME")
        shift = _symbol_shift(params.name_length, config.max_name_length, "name_length")
        # Governance-module rows are free data: the two name rows escrow nothing.
        return _OperationModel(
            work_units=0 if v2 else config.gas_fee_register_name >> shift,
            policy_fee=config.policy_fee_register_name >> shift if v2 else 0,
        )
    if kind is NativeFeeKind.SCRIPT:
        return _OperationModel(
            work_units=(
                _DEFAULT_SCRIPT_UNITS_ALLOWANCE
                if params.script_units_allowance is None
                else params.script_units_allowance
            ),
            result_bytes=_DEFAULT_SCRIPT_EVENT_BYTES
            if params.script_event_bytes is None
            else params.script_event_bytes,
            new_quanta=(
                _DEFAULT_SCRIPT_STORAGE_QUANTA if params.script_storage_quanta is None else params.script_storage_quanta
            ),
        )
    raise BuilderError(f"unknown fee kind: {kind}")


def _distinct_series(params: NativeFeeParams, count: int) -> int:
    # Distinct series a duplicated mint touches, defaulting to one. More series than instances is
    # impossible, because at least one instance writes into every series in the call. Catching
    # it here turns a caller's bookkeeping slip into an error instead of an over-offer nobody notices.
    distinct = 1 if params.distinct_series_count is None else params.distinct_series_count
    if distinct < 1 or distinct > count:
        raise BuilderError(f"estimate_native_fee: distinct_series_count must be between 1 and {count}")
    return distinct


def _returned_assets(params: NativeFeeParams, config: GasConfig) -> _OperationModel:
    # What burning the NFTs gives back to the burner, priced as the transfers the chain performs: per
    # fungible token one transfer plus the owner lookup of the NFT-address source; per NFT token one
    # instance query, one transfer per instance and that same lookup. Rows: a balance row of a token
    # the burner does not hold is created (paid unless the token is the gas or data token, which only
    # the id can tell, so an unknown id is priced as paid), every returned instance moves its lookup
    # row, and the NFT address's own rows are deleted. The deletions always match or exceed the
    # creations, so the returns never add block data; they add work, and rows to the escrow ceiling.
    model = _OperationModel()
    for asset in params.infusions or []:
        free_rows = asset.token_id is not None and asset.token_id in (config.gas_token_id, config.data_token_id)
        balance_row = _quantum(not asset.burner_holds_token and not free_rows)
        if asset.non_fungible:
            instances = 1 if asset.instance_count is None else asset.instance_count
            if instances < 1:
                raise BuilderError(
                    "estimate_native_fee: instance_count of a returned NFT token must be a positive integer"
                )
            model.work_units += config.gas_fee_query * 2 + config.gas_fee_transfer * instances
            model.new_quanta += balance_row + instances
            model.deleted_quanta += 1 + instances
        else:
            model.work_units += config.gas_fee_transfer + config.gas_fee_query
            model.new_quanta += balance_row
            model.deleted_quanta += _quantum(not free_rows)
    return model


def _per_instance(values: list[int], count: int, name: str) -> list[int]:
    # Expands a per-instance size list: empty means zero bytes for every instance, a single entry
    # applies to every instance, otherwise one entry per instance is required.
    if not values:
        return [0] * count
    if len(values) == 1:
        return [values[0]] * count
    if len(values) == count:
        return list(values)
    raise BuilderError(f"estimate_native_fee: {name} must have one entry per instance ({count})")


def _mul_shift(value: int, multiplier: int, shift: int) -> int:
    # Chain fee scaling: (value * fee_multiplier) >> fee_shift, saturating to u64. Saturation is what
    # the chain does under gas model v2, so a hostile config cannot wrap a bill; under v1 the live
    # values never approach 64 bits, so the same expression is bit-identical to the v1 arithmetic.
    if shift >= 64:
        return 0  # the chain clamps oversized shifts to a zero delta
    return min((value * multiplier) >> shift, _U64_MAX)


def _symbol_shift(length: int, max_length: int, param_name: str) -> int:
    if length < 0:
        raise BuilderError(f"estimate_native_fee: {param_name} must not be negative")
    if length == 0:
        return 0
    shift = length - 1
    # The chain asserts shift < max_name_length / max_token_symbol_length; a longer input could never
    # be admitted, so reject it here instead of quoting a fee for an impossible tx.
    if max_length and shift >= max_length:
        raise BuilderError(f"estimate_native_fee: {param_name} {length} exceeds the chain maximum {max_length}")
    # The calculator refuses instead of guessing, see _MAX_PRICEABLE_LENGTH. The transaction may well
    # be admitted. This
    # says only that no honest price can be quoted for it offline.
    if length > _MAX_PRICEABLE_LENGTH:
        raise BuilderError(
            f"estimate_native_fee: {param_name} {length} is longer than {_MAX_PRICEABLE_LENGTH} "
            "and cannot be priced offline"
        )
    return shift


@dataclass(slots=True)
class FeePlanOptions:
    """Facts about chain state and signing that a message does not carry but the fee depends on.

    Every state fact defaults to the case that costs more, so an unspecified plan is an upper bound
    the settlement can only undercut; the defaults themselves belong to NativeFeeParams, which
    documents each one, and are not restated here so the two cannot drift apart. The one fact
    without a costlier reading is `infusions`: a burned NFT can hold any number of assets, so
    plan_fees demands it instead of assuming, and the RPC-side planner reads it from the chain. The
    facts the message itself carries - counts, sizes, token ids, the NFT-address recipient, the
    token flags, the metadata keys - are deliberately absent: plan_fees reads those out of the
    message, and a caller-supplied value could only contradict it.

    Roughly in order of how likely a caller is to know the answer: an ordinary wallet may well know
    recipient_holds_token, big_fungible, token_burned_before and supply_row_exists;
    duplicated_series, rom_has_meta_id and series_has_meta_id need the token's schema or the
    series' metadata, so state them only if you read them; the three script allowances size the
    budget of a VM script, whose cost no formula can predict.
    """

    #: How many witnesses will sign a CALL / CALL_MULTI / TRADE / PHANTASMA message. Required for
    #: those types and for them only: their witness set is chosen by the caller, nothing in the
    #: message says how large it will be, and each witness adds 96 bytes the chain bills. Every
    #: other type fixes its own witness set, so a count stated for one of them must agree with it.
    #: PhantasmaRPC.send_transaction fills it in from the signers it was given.
    witness_count: int | None = None
    #: See NativeFeeParams.recipient_holds_token.
    recipient_holds_token: bool = False
    #: See NativeFeeParams.big_fungible.
    big_fungible: bool | None = None
    #: See NativeFeeParams.token_burned_before.
    token_burned_before: bool = False
    #: See NativeFeeParams.supply_row_exists.
    supply_row_exists: bool = False
    #: See NativeFeeParams.duplicated_series.
    duplicated_series: bool | None = None
    #: See NativeFeeParams.rom_has_meta_id.
    rom_has_meta_id: bool | None = None
    #: See NativeFeeParams.series_has_meta_id.
    series_has_meta_id: bool | None = None
    #: What the burned NFTs hold. It is required to plan a burn here, and it is read from the chain by
    #: PhantasmaRPC.fees. An empty list states that the NFT holds nothing. See
    #: NativeFeeParams.infusions.
    infusions: list[InfusedAsset] | None = None
    #: See NativeFeeParams.script_units_allowance.
    script_units_allowance: int | None = None
    #: See NativeFeeParams.script_event_bytes.
    script_event_bytes: int | None = None
    #: See NativeFeeParams.script_storage_quanta.
    script_storage_quanta: int | None = None


@dataclass(slots=True, frozen=True)
class BurnedInstance:
    """One NFT instance that a message burns. See burned_instances."""

    token_id: int
    instance_id: int


@dataclass(slots=True, frozen=True)
class FeePlan:
    """A fee plan for one message: the estimate, what it was computed from, and how to apply it."""

    #: The operations the message was recognised as, in call order. The bill was computed from them.
    #: An ordinary message has one entry, and a CALL_MULTI has one entry per inner call.
    #:
    #: Every kind but SCRIPT is priced with the chain's own formula for that operation. SCRIPT covers
    #: VM scripts and unmodelled calls. Their work depends on execution, so they can only be budgeted
    #: (see script_units_allowance and its neighbours).
    #:
    #: This field says what was priced. How firm the number is, FeePlan.exact answers.
    kinds: tuple[NativeFeeKind, ...]
    #: True when this bill is a prediction of the settlement, False when it is an upper bound on it.
    #: A wallet showing a fee reads this one field to choose between "0.0073 KCAL" and "up to 0.0073
    #: KCAL".
    #:
    #: The field is False in two cases. A chain-state fact was left at its costlier reading and that
    #: reading decided part of the price. Or some part of the message had to be budgeted.
    #:
    #: recipient_holds_token, token_burned_before and supply_row_exists are plain bools whose False
    #: is the costlier reading, so a caller that states the costlier value and a caller that states
    #: nothing arrive here the same way, and both get a bound. Stating a costlier fact buys nothing,
    #: so nothing is lost by that.
    #:
    #: big_fungible is flipped even when the caller stated it. It claims nothing about chain state:
    #: it asks the model to price the widest answer a variable-length balance can have. A plan that
    #: rests on it reports False however it was arrived at.
    exact: bool
    #: The signed size the plan was computed for. These are the bytes the block will carry.
    envelope_bytes: int
    #: See FeeQuote.max_gas.
    max_gas: int
    #: See FeeQuote.max_data.
    max_data: int
    #: See FeeQuote.expected_gas_bill.
    expected_gas_bill: int
    #: See NativeFeeEstimate.new_storage_quanta.
    new_storage_quanta: int
    #: See NativeFeeEstimate.deleted_storage_quanta.
    deleted_storage_quanta: int

    def quote(self) -> FeeQuote:
        """The quote alone."""
        return FeeQuote(self.max_gas, self.max_data, self.expected_gas_bill)

    def apply(self, msg: TxMsg) -> TxMsg:
        """A copy of `msg` with max_gas and max_data set to the plan. The input is left untouched."""
        return dataclasses.replace(msg, max_gas=self.max_gas, max_data=self.max_data)


def burned_instances(msg: TxMsg) -> list[BurnedInstance]:
    """The NFT instances a message burns. It covers the native burn types, a Token.BurnNonFungible
    call, and every such call inside a CALL_MULTI.

    A burn returns whatever the instance's own address holds, and the chain charges for each returned
    asset. A planner with a chain to ask reads those assets per instance and hands the union to
    FeePlanOptions.infusions.

    This function sits beside the decomposition that decides which calls are burns, so the two cannot
    come to disagree."""
    inner = msg.msg
    if isinstance(inner, TxMsgBurnNonFungible | TxMsgBurnNonFungibleGasPayer):
        return [BurnedInstance(inner.token_id, inner.instance_id)]
    if isinstance(inner, TxMsgCall):
        return _burned_by_call(inner)
    if isinstance(inner, TxMsgCallMulti):
        return [instance for call in inner.calls for instance in _burned_by_call(call)]
    return []


def _burned_by_call(call: TxMsgCall) -> list[BurnedInstance]:
    if call.sections is not None and call.sections.has_sections:
        return []
    if call.module_id != ModuleID.TOKEN or call.method_id != TokenContractMethod.BURN_NON_FUNGIBLE:
        return []
    try:
        args = deserialize(call.args, BurnNonFungibleArgs)
    except Exception:
        return []
    assert isinstance(args, BurnNonFungibleArgs)
    return [BurnedInstance(args.token_id, instance_id) for instance_id in args.instance_ids]


def plan_fees(msg: TxMsg, config: GasConfig, options: FeePlanOptions | None = None) -> FeePlan:
    """Plans the gas offer and storage ceiling of a message from the message itself: its type and
    contents decide the operation model, its signed size is computed with placeholder witnesses, and
    the chain config supplies the prices. It touches no network: fetch the config with
    PhantasmaRPC.fees or get_gas_config and pass it in."""
    options = options if options is not None else FeePlanOptions()
    # A witness-array message does not say how many signatures it will carry, and each one is 96
    # billed bytes. An assumed single witness would under-offer every multi-party transaction by 96
    # bytes per extra signature, and the chain would reject it. So the count is demanded here.
    # estimate_native_fee takes the same stance on a missing envelope size.
    if required_witnesses(msg) is None and options.witness_count is None:
        raise BuilderError(
            f"plan_fees: {msg.type.name} transactions choose their own witnesses: set witness_count to plan one"
        )
    parts = _describe(msg, options)
    transaction = NativeFeeTransactionParams(envelope_bytes=envelope_bytes(msg, options.witness_count))
    estimate = estimate_native_fee_batch(parts, config, transaction)
    kinds = tuple(part.kind for part in parts)
    budgeted = NativeFeeKind.SCRIPT in kinds
    exact = not budgeted and not _assumptions_mattered(msg, config, options, transaction, estimate)
    return FeePlan(
        kinds=kinds,
        exact=exact,
        envelope_bytes=transaction.envelope_bytes,
        max_gas=estimate.max_gas,
        max_data=estimate.max_data,
        expected_gas_bill=estimate.expected_gas_bill,
        new_storage_quanta=estimate.new_storage_quanta,
        deleted_storage_quanta=estimate.deleted_storage_quanta,
    )


def _assumptions_mattered(
    msg: TxMsg,
    config: GasConfig,
    options: FeePlanOptions,
    transaction: NativeFeeTransactionParams,
    quoted: NativeFeeEstimate,
) -> bool:
    # Whether a fact left at its costlier reading changed this quote.
    #
    # The message is priced a second time, with every state fact at its CHEAPER reading. The two
    # quotes are then compared. If they agree, the costlier readings decided nothing and the bill is
    # a prediction.
    #
    # The question is asked this way to keep ONE definition of which facts an operation reads: the
    # operation models themselves. A list written here would drift from them as the models change.
    #
    # The answer is also per message, not per kind. That matters. A gas-token transfer does not
    # depend on recipient_holds_token at all, because the chain's own rows are free. A plan that
    # reported the fact as assumed would send every ordinary transfer to the "up to" branch.
    #
    # infusions is not flipped. It has no cheaper reading, and the planner demands it.
    cheapest = dataclasses.replace(
        options,
        recipient_holds_token=True,
        token_burned_before=True,
        supply_row_exists=True,
        big_fungible=False,
        rom_has_meta_id=bool(options.rom_has_meta_id),
        series_has_meta_id=bool(options.series_has_meta_id),
        duplicated_series=bool(options.duplicated_series),
    )
    cheaper = estimate_native_fee_batch(_describe(msg, cheapest), config, transaction)
    return (
        cheaper.expected_gas_bill != quoted.expected_gas_bill
        or cheaper.max_gas != quoted.max_gas
        or cheaper.max_data != quoted.max_data
    )


def _state_facts(options: FeePlanOptions) -> NativeFeeParams:
    # Passed through as they are: the calculator owns every default, so no default is decided in
    # two places.
    return NativeFeeParams(
        recipient_holds_token=options.recipient_holds_token,
        big_fungible=options.big_fungible,
        token_burned_before=options.token_burned_before,
        supply_row_exists=options.supply_row_exists,
        rom_has_meta_id=options.rom_has_meta_id,
    )


def _describe(msg: TxMsg, options: FeePlanOptions) -> list[NativeFeePart]:
    # Recognises the operations a message performs and reads their facts out of the message. An
    # ordinary message gives one part, and a CALL_MULTI gives one part per inner call. That is what
    # lets a batch be priced.
    #
    # Whether the recipient is an NFT-derived address is NOT taken from the options: the address form
    # decides it, and the message carries the address, so each branch reads it out.
    inner = msg.msg
    state = _state_facts(options)
    if isinstance(inner, TxMsgTransferFungible | TxMsgTransferFungibleGasPayer):
        state.token_id = inner.token_id
        state.to_is_nft_address = is_nft_address(inner.to)
        return [NativeFeePart(NativeFeeKind.TRANSFER_FUNGIBLE, state)]
    if isinstance(inner, TxMsgTransferNonFungibleSingle | TxMsgTransferNonFungibleSingleGasPayer):
        state.token_id = inner.token_id
        state.count = 1
        state.to_is_nft_address = is_nft_address(inner.to)
        return [NativeFeePart(NativeFeeKind.TRANSFER_NON_FUNGIBLE, state)]
    if isinstance(inner, TxMsgTransferNonFungibleMulti | TxMsgTransferNonFungibleMultiGasPayer):
        state.token_id = inner.token_id
        state.count = _instance_count(len(inner.instance_ids))
        state.to_is_nft_address = is_nft_address(inner.to)
        return [NativeFeePart(NativeFeeKind.TRANSFER_NON_FUNGIBLE, state)]
    if isinstance(inner, TxMsgMintFungible):
        state.token_id = inner.token_id
        state.to_is_nft_address = is_nft_address(inner.to)
        return [NativeFeePart(NativeFeeKind.MINT_FUNGIBLE, state)]
    if isinstance(inner, TxMsgBurnFungible | TxMsgBurnFungibleGasPayer):
        state.token_id = inner.token_id
        return [NativeFeePart(NativeFeeKind.BURN_FUNGIBLE, state)]
    if isinstance(inner, TxMsgMintNonFungible):
        state.token_id = inner.token_id
        state.rom_bytes = [len(inner.rom)]
        state.ram_bytes = [len(inner.ram)]
        state.to_is_nft_address = is_nft_address(inner.to)
        return [NativeFeePart(NativeFeeKind.MINT_NON_FUNGIBLE, state)]
    if isinstance(inner, TxMsgBurnNonFungible | TxMsgBurnNonFungibleGasPayer):
        return [_describe_burn(inner.token_id, 1, state, options.infusions)]
    if isinstance(inner, TxMsgCall):
        return [_describe_call(inner, state, options, options.infusions)]
    if isinstance(inner, TxMsgCallMulti):
        # The chain runs the calls in a loop and bills their sum, so the plan is the sum of their
        # models. infusions covers every burn in the batch. The returns cost the same wherever they
        # are counted, so the first burn takes the whole list and the burns after it take none.
        parts: list[NativeFeePart] = []
        returns = options.infusions
        for call in inner.calls:
            part = _describe_call(call, _state_facts(options), options, returns)
            if part.kind is NativeFeeKind.BURN_NON_FUNGIBLE:
                returns = []
            parts.append(part)
        return parts
    if isinstance(inner, TxMsgTrade | TxMsgPhantasma):
        return [_script_part(options)]
    if isinstance(inner, TxMsgPhantasmaRaw):
        raise BuilderError(f"plan_fees: cannot plan fees for a {msg.type.name} transaction")
    raise BuilderError(f"plan_fees: unrecognised message payload {type(inner).__name__}")


def _describe_burn(
    token_id: int, count: int, state: NativeFeeParams, infusions: list[InfusedAsset] | None
) -> NativeFeePart:
    # Prices an NFT burn. What the NFTs hold is chain state with no costlier bound, so it is
    # demanded, not assumed: a burn planned as if the addresses were empty is short by every returned
    # asset and aborts, billed, on every retry. The stored ROM is chain state the message does not
    # carry, so the deleted quanta are a lower bound. That does not touch the offer, because a burn
    # deletes more rows than it creates and only the rows it creates are escrowed.
    if infusions is None:
        raise BuilderError(
            "plan_fees: a burn returns whatever the NFT holds: set infusions (empty when it holds nothing) "
            "or plan through PhantasmaRPC.fees, which reads them from the chain"
        )
    state.token_id = token_id
    state.count = count
    state.infusions = list(infusions)
    return NativeFeePart(NativeFeeKind.BURN_NON_FUNGIBLE, state)


def _describe_call(
    call: TxMsgCall,
    state: NativeFeeParams,
    options: FeePlanOptions,
    infusions: list[InfusedAsset] | None,
) -> NativeFeePart:
    # A call can build its arguments at execution time from the results of earlier calls. Such a call
    # carries none of them yet. There is nothing to read a price from, so it takes the script budget.
    if call.sections is not None and call.sections.has_sections:
        return _script_part(options)
    if call.module_id == ModuleID.TOKEN:
        # The five token movements below cost exactly what they cost as native transaction types.
        # Both paths enter the same contract method. They arrive as module calls because a wallet
        # batched them.
        #
        # TokenContractMethod.MINT_NON_FUNGIBLE is absent on purpose. The chain refuses it where
        # governance has not allowed caller-supplied ROM ids, whichever way it arrives, so there is
        # nothing to price.
        if call.method_id == TokenContractMethod.TRANSFER_FUNGIBLE:
            transfer = _read_args(call, TransferFungibleArgs, "TransferFungible")
            state.token_id = transfer.token_id
            state.to_is_nft_address = is_nft_address(transfer.to)
            return NativeFeePart(NativeFeeKind.TRANSFER_FUNGIBLE, state)
        if call.method_id == TokenContractMethod.TRANSFER_NON_FUNGIBLE:
            transfer_nft = _read_args(call, TransferNonFungibleArgs, "TransferNonFungible")
            state.token_id = transfer_nft.token_id
            state.count = _instance_count(len(transfer_nft.instance_ids))
            state.to_is_nft_address = is_nft_address(transfer_nft.to)
            return NativeFeePart(NativeFeeKind.TRANSFER_NON_FUNGIBLE, state)
        if call.method_id == TokenContractMethod.MINT_FUNGIBLE:
            mint = _read_args(call, MintFungibleArgs, "MintFungible")
            state.token_id = mint.token_id
            state.to_is_nft_address = is_nft_address(mint.to)
            return NativeFeePart(NativeFeeKind.MINT_FUNGIBLE, state)
        if call.method_id == TokenContractMethod.BURN_FUNGIBLE:
            burn = _read_args(call, BurnFungibleArgs, "BurnFungible")
            state.token_id = burn.token_id
            return NativeFeePart(NativeFeeKind.BURN_FUNGIBLE, state)
        if call.method_id == TokenContractMethod.BURN_NON_FUNGIBLE:
            burn_nft = _read_args(call, BurnNonFungibleArgs, "BurnNonFungible")
            count = _instance_count(len(burn_nft.instance_ids))
            return _describe_burn(burn_nft.token_id, count, state, infusions)
        if call.method_id == TokenContractMethod.CREATE_TOKEN:
            info = _read_args(call, TokenInfo, "CreateToken")
            # The token-info row is the Call arguments as submitted: the chain stores the TokenInfo
            # it was given, metadata included, and measured bills confirm the row equals the
            # arguments. Which extra rows the creation writes, and which lookups validating it
            # costs, is decided by the metadata, which is a named struct the plan can read.
            metadata = VMDynamicStruct()
            if info.metadata:
                try:
                    decoded = deserialize(info.metadata, VMDynamicStruct)
                except Exception as exc:
                    raise BuilderError(f"plan_fees: CreateToken metadata: {exc}") from exc
                assert isinstance(decoded, VMDynamicStruct)
                metadata = decoded
            return NativeFeePart(
                NativeFeeKind.CREATE_TOKEN,
                NativeFeeParams(
                    symbol_length=len(info.symbol.value.encode("utf-8")),
                    token_info_bytes=len(call.args),
                    non_fungible=bool(info.flags & TokenFlags.NON_FUNGIBLE),
                    has_pre_burn=metadata.get(STANDARD_META_TOKEN_PRE_BURN) is not None,
                    has_inflation_schedule=metadata.get(STANDARD_META_TOKEN_INFLATION_PERIOD) is not None,
                    has_staking_organisation=metadata.get(STANDARD_META_TOKEN_STAKING_ORG_ID) is not None,
                    has_staking_reward_token=metadata.get(STANDARD_META_TOKEN_STAKING_REWARD_TOKEN) is not None,
                ),
            )
        if call.method_id == TokenContractMethod.CREATE_TOKEN_SERIES:
            # The arguments are the u64 token id followed by the SeriesInfo, which becomes the row.
            return NativeFeePart(
                NativeFeeKind.CREATE_TOKEN_SERIES,
                NativeFeeParams(
                    series_info_bytes=max(len(call.args) - 8, 0),
                    series_has_meta_id=options.series_has_meta_id,
                ),
            )
        if call.method_id == TokenContractMethod.MINT_PHANTASMA_NON_FUNGIBLE:
            phantasma_mint = _read_args(call, MintPhantasmaNonFungibleArgs, "MintPhantasmaNonFungible")
            # Each instance names the series it is minted into. The number of distinct series a
            # duplicated mint touches is therefore readable from the call, and the caller never has
            # to supply it. The chain's per-series supply reads are charged per distinct series.
            state.token_id = phantasma_mint.token_id
            state.count = _instance_count(len(phantasma_mint.tokens))
            state.rom_bytes = [len(token.rom) for token in phantasma_mint.tokens]
            state.ram_bytes = [len(token.ram) for token in phantasma_mint.tokens]
            state.duplicated_series = options.duplicated_series
            state.distinct_series_count = len({token.phantasma_series_id.value for token in phantasma_mint.tokens})
            state.to_is_nft_address = is_nft_address(phantasma_mint.address)
            return NativeFeePart(NativeFeeKind.MINT_PHANTASMA_NON_FUNGIBLE, state)
        return _script_part(options)
    if call.module_id == ModuleID.GOVERNANCE and call.method_id == GovernanceContractMethod.REGISTER_NAME:
        name_args = _read_args(call, RegisterNameArgs, "RegisterName")
        return NativeFeePart(
            NativeFeeKind.REGISTER_NAME,
            NativeFeeParams(name_length=len(name_args.name.value.encode("utf-8"))),
        )
    return _script_part(options)


_ArgsT = TypeVar("_ArgsT", bound=CarbonSerializable)


def _read_args(call: TxMsgCall, args_type: type[_ArgsT], name: str) -> _ArgsT:
    # Reads one call's typed arguments, naming the method in the error a malformed call produces.
    try:
        args = deserialize(call.args, args_type)
    except Exception as exc:
        raise BuilderError(f"plan_fees: {name} arguments: {exc}") from exc
    assert isinstance(args, args_type)
    return args


def _script_part(options: FeePlanOptions) -> NativeFeePart:
    return NativeFeePart(
        NativeFeeKind.SCRIPT,
        NativeFeeParams(
            script_units_allowance=options.script_units_allowance,
            script_event_bytes=options.script_event_bytes,
            script_storage_quanta=options.script_storage_quanta,
        ),
    )


def _instance_count(length: int) -> int:
    if length < 1:
        raise BuilderError("plan_fees: the message must carry at least one instance")
    return length


@dataclass(slots=True)
class PlanAndSignOptions:
    """The options of the build_*_tx_and_sign conveniences: how to plan the fee, or what to write
    instead."""

    #: The facts the plan cannot read from the message; see FeePlanOptions.
    facts: FeePlanOptions = field(default_factory=FeePlanOptions)
    #: The limits the builder writes into the message. A nonzero max_gas fixes the offer and skips
    #: planning; see TxLimits.
    limits: TxLimits = field(default_factory=TxLimits)


def plan_and_sign_with_keys(
    msg: TxMsg,
    keys: Sequence[PhantasmaKeys],
    config: GasConfig | None = None,
    options: PlanAndSignOptions | None = None,
) -> bytes:
    """Plans a freshly built message against config, unless the caller fixed max_gas themselves,
    and signs it with in-memory keys. The convenience behind every build_*_tx_and_sign helper; a
    wallet with an external signer plans with plan_fees and signs with sign_tx_msg_with."""
    options = options or PlanAndSignOptions()
    # Whether the fee is already settled is read from the MESSAGE: the builders are what write the
    # caller's limits into it, so the message is the one place that is right for every helper. The
    # same rule as PhantasmaRPC.send_transaction.
    if msg.max_gas != 0:
        return sign_and_serialize_tx_msg(msg, *keys)
    if config is None:
        raise BuilderError(
            "plan_and_sign: a message without a gas offer needs the chain's gas config to plan it: "
            "pass the config, or fix max_gas in the limits"
        )
    # Only the witness-array types take their witness count from the caller, and these keys are
    # that caller's answer; for every other type the message fixes its own slots and one key may
    # fill two of them, so passing a count would contradict the message.
    facts = dataclasses.replace(options.facts)
    if required_witnesses(msg) is None and facts.witness_count is None:
        facts.witness_count = len(keys)
    plan = plan_fees(msg, config, facts)
    return sign_and_serialize_tx_msg(plan.apply(msg), *keys)


#: Decimals of the gas token: 1 KCAL = 1e10 kcal-base.
KCAL_DECIMALS = 10
#: Decimals of the data token: 1 SOUL = 1e8 atoms.
SOUL_DECIMALS = 8


@dataclass(slots=True, frozen=True)
class FeePlanSummary:
    """A fee plan in the units a person reads: KCAL for gas, SOUL for the storage deposit."""

    #: What the transaction will cost in gas, as a decimal amount.
    gas_bill: str
    #: The gas offer written into the transaction; the difference to gas_bill is refunded.
    gas_offer: str
    #: The storage deposit the transaction may take, as a decimal amount. It is escrowed while the
    #: transaction settles and refunded when the rows it paid for are deleted; a wallet shows it
    #: separately from the fee, as a refundable deposit.
    storage_ceiling: str


def summarize_fee_plan(plan: FeePlan) -> FeePlanSummary:
    """Renders a plan for display in KCAL and SOUL."""
    return summarize_fee_plan_with_decimals(plan, KCAL_DECIMALS, SOUL_DECIMALS)


def summarize_fee_plan_with_decimals(plan: FeePlan, gas_decimals: int, data_decimals: int) -> FeePlanSummary:
    """Renders a plan for display on a chain whose gas or data token has other decimals."""
    # The client module imports this one, so the decimal renderer is fetched on use.
    from .rpc import convert_decimals

    return FeePlanSummary(
        gas_bill=convert_decimals(plan.expected_gas_bill, gas_decimals),
        gas_offer=convert_decimals(plan.max_gas, gas_decimals),
        storage_ceiling=convert_decimals(plan.max_data, data_decimals),
    )
