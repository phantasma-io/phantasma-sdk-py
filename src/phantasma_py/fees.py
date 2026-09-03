"""Fee calculation under both gas models: the exact bill and storage escrow of a native operation,
reproducing the chain's own settlement arithmetic and the gas each contract path charges."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .carbon import GasConfig
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
    is billed and every new storage row is escrowed, so the inputs are the sizes the chain will see:
    the signed envelope, the serialized structures the operation stores, and the facts about
    existing state that decide whether a row is new.

    The inputs are of two kinds, and they are defaulted differently:

    - Facts the CALLER CANNOT KNOW without reading chain state - whether the recipient already holds
      the token, whether a ROM carries an `_i` id, which mode a series mints in. Each defaults to
      the case that costs MORE, so an estimate built from the defaults is an upper bound the
      settlement can only undercut, never a short offer. The facts whose costlier reading is True
      are `bool | None` with None meaning "unstated"; the others are plain bools whose False is the
      costlier reading.
    - Facts carried by the MESSAGE ITSELF - the instance count, the serialized sizes, whether the
      token being created is non-fungible or carries `pre_burn`. These have no safe default because
      they are not guesses: pass them. plan_fees reads every one of them out of the message.
    """

    #: Full signed transaction size in bytes - the envelope carried in the block. Required under gas
    #: model v2 (see envelope_bytes and envelope_bytes_for); ignored under v1, which billed only the
    #: payload note.
    envelope_bytes: int = 0
    #: Instance count for NFT kinds (transferred / minted / burned instances). None = 1.
    count: int | None = None
    #: Token moved by a transfer / mint / burn. Balance rows of the chain's gas and data tokens are
    #: free, so with the token id known the estimate escrows nothing for them; None prices the rows
    #: as paid.
    token_id: int | None = None
    #: The recipient already holds this token, so its balance row exists and costs nothing. False
    #: (the default) prices a fresh row.
    recipient_holds_token: bool = False
    #: The recipient is an NFT-derived address (an infusion): the chain reads that NFT's owner - one
    #: extra query fee. Transfers and every mint kind pay it; a burn has no recipient. This is a fact
    #: of the recipient's address form, not of chain state - plan_fees derives it from the message's
    #: own recipient (is_nft_address) - so only direct callers of this calculator pass it.
    to_is_nft_address: bool = False
    #: The token's balances can exceed int64 (a big-fungible token). A fungible mint or burn answers
    #: with the RESULTING balance as a variable-length integer - 9 bytes while it fits int64, up to
    #: 33 for an int256 balance - and the resulting balance is chain state, so None prices the
    #: 33-byte maximum: a covering bound, refunded down. False prices the 9-byte result exactly, for
    #: an ordinary int64 token.
    big_fungible: bool | None = None
    #: The token has been burned before, so its burnt counter row exists. False (the default) prices
    #: the row the first burn creates.
    token_burned_before: bool = False
    #: The token's supply-tracking row exists. The chain drops that row when its balance reaches
    #: exactly zero, so this is chain state with two absent-row edges: a limited-supply token whose
    #: entire supply is in circulation (the next burn recreates the row) and an unlimited token with
    #: nothing outstanding (the next mint recreates it). Unstated, every mint and burn prices the
    #: recreation - one more storage quantum in the bill and the escrow ceiling - so the default
    #: covers both edges; pass True for the exact quote whenever the token is not at one of them.
    #: Rows of the chain's gas and data tokens are free either way.
    supply_row_exists: bool = False
    #: What the burned NFTs hold at their own addresses (BURN_NON_FUNGIBLE), one entry per asset per
    #: burned instance. The burn returns every one of them to the burner, and the chain charges for
    #: each: a transfer fee plus the owner-lookup query of the NFT-address source per fungible
    #: token, an instance query plus a transfer per instance plus that lookup per NFT token, and the
    #: burner's balance row of a returned token the burner does not hold. This is chain state the
    #: message does not carry, and it has no costlier bound - an NFT can hold any number of assets -
    #: so nothing is assumed: None prices an empty address (direct callers of this calculator state
    #: what they know), while plan_fees demands the list and the RPC-side planner reads it from the
    #: chain.
    infusions: list[InfusedAsset] | None = None
    #: Token symbol length in characters (CREATE_TOKEN). 0 = no symbol.
    symbol_length: int = 0
    #: Serialized TokenInfo length (CREATE_TOKEN) - the Call arguments; it becomes the token-info row.
    token_info_bytes: int = 0
    #: The token being created is non-fungible (CREATE_TOKEN): one more row, the series counter.
    non_fungible: bool = False
    #: The token metadata carries `pre_burn` (CREATE_TOKEN): the burnt counter row is created at once.
    has_pre_burn: bool = False
    #: The token metadata carries an inflation schedule (CREATE_TOKEN): the next-inflation row is created.
    has_inflation_schedule: bool = False
    #: The token metadata names a staking organisation (CREATE_TOKEN): the creation looks the
    #: organisation up, one query fee.
    has_staking_organisation: bool = False
    #: The token metadata names a staking reward token (CREATE_TOKEN): the creation reads that
    #: token's info, one query fee.
    has_staking_reward_token: bool = False
    #: Serialized SeriesInfo length (CREATE_TOKEN_SERIES) - the Call arguments after the token id.
    series_info_bytes: int = 0
    #: The series metadata carries a `_i` id (CREATE_TOKEN_SERIES): the meta-id lookup row is
    #: created. Schema-encoded like the ROM, so None prices the row that may be billed.
    series_has_meta_id: bool | None = None
    #: Registered name length in characters (REGISTER_NAME). Required for that kind.
    name_length: int = 0
    #: ROM bytes per minted or burned instance (MINT_NON_FUNGIBLE / BURN_NON_FUNGIBLE: as stored;
    #: MINT_PHANTASMA_NON_FUNGIBLE: the public ROM): one entry per instance, or a single entry that
    #: applies to every instance. Empty = 0 bytes.
    rom_bytes: list[int] = field(default_factory=list)
    #: RAM bytes per instance, in the same shape as rom_bytes. Empty = no RAM row.
    ram_bytes: list[int] = field(default_factory=list)
    #: The raw ROM carries a `_i` id, which the chain indexes in one more row (MINT_NON_FUNGIBLE /
    #: BURN_NON_FUNGIBLE). The ROM is schema-encoded, so a caller holding only the bytes cannot
    #: tell, and None assumes the id - on a mint that is the reading which escrows for the row, and
    #: on a burn it is the reading that mirrors what the mint created. The burn does not PRICE on it
    #: either way (see NativeFeeEstimate.deleted_storage_quanta); a Phantasma mint always has one
    #: and ignores this input.
    rom_has_meta_id: bool | None = None
    #: The series mints duplicated NFTs (MINT_PHANTASMA_NON_FUNGIBLE). A duplicated series costs one
    #: more query fee per instance than a unique one, plus one per distinct series (see
    #: distinct_series_count). A call whose instances mix duplicated and unique series is priced as
    #: if every instance were duplicated.
    #:
    #: None prices the duplicated mode: a series' mode is chain state the message does not carry,
    #: so the costlier reading is the only safe one - a duplicated mint priced as unique is short by
    #: exactly those query fees, and the planner offers the bill with no headroom, so it aborts.
    #: Pass False only when the series is known to be unique - the saving is a few query fees.
    duplicated_series: bool | None = None
    #: How many distinct series a duplicated Phantasma mint writes into
    #: (MINT_PHANTASMA_NON_FUNGIBLE with duplicated_series). The chain reads each series' supply
    #: once per transaction, not once per instance, so this is the count of distinct series ids in
    #: the call - never more than count. None = 1. Ignored for a unique series, which does not read
    #: the supply at all.
    distinct_series_count: int | None = None
    #: User payload bytes attached to the tx (billed under gas model v1 only).
    payload_bytes: int = 0
    #: VM work-unit allowance for the SCRIPT kind. None = 5000, which exceeds every script seen in
    #: mainnet history (max 3392 units) with margin.
    script_units_allowance: int | None = None
    #: Event bytes allowance for the SCRIPT kind (Notify payloads count as block data). None = 512.
    script_event_bytes: int | None = None
    #: New storage quanta allowance for the SCRIPT kind. None = 4.
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
    #: The bill the chain formula yields for exactly the provided inputs - exact for every native
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
    #: floors at zero either way. A burn's figure is therefore a lower bound - the stored ROM is
    #: chain state the message does not carry - and nothing depends on tightening it.
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


def estimate_native_fee(
    kind: NativeFeeKind, config: GasConfig, params: NativeFeeParams | None = None
) -> NativeFeeEstimate:
    """The offline fee calculator: the exact gas bill and storage escrow of a native operation under
    both gas models (selected by GasConfig.version), reproducing the chain's own settlement
    arithmetic and the gas each contract path charges. Any change to those formulas ships as a new
    gas-model version, never silently, which is what makes an offline calculation safe."""
    params = params if params is not None else NativeFeeParams()
    count = 1 if params.count is None else params.count
    if count < 1:
        raise BuilderError("estimate_native_fee: count must be a positive integer")
    v2 = config.has_gas_model_v2
    model = _operation_model(kind, config, params, count)
    # Only the net growth of paid storage is block data; deleted rows are refunded, not billed.
    net_quanta = max(model.new_quanta - model.deleted_quanta, 0)

    if v2:
        if params.envelope_bytes <= 0:
            raise BuilderError("estimate_native_fee: envelope_bytes is required under gas model v2")
        # v2: bill = mul_shift(work + block_data * 25, mult, shift) + policy_fee, floored at
        # minimum_gas_bill, where block_data = envelope + net storage quanta + Call result bytes.
        block_data = params.envelope_bytes + net_quanta + model.result_bytes
        byte_units = block_data * GAS_MODEL_V2_UNITS_PER_BLOCK_DATA_BYTE
        bill = _mul_shift(model.work_units + byte_units, config.fee_multiplier, config.fee_shift) + model.policy_fee
        expected = max(min(bill, _U64_MAX), config.minimum_gas_bill)
        max_gas = max(expected, config.minimum_gas_offer)
    else:
        # v1: bill = (work * mult >> shift) + block_data * gas_fee_per_byte, where block_data =
        # payload + Call result bytes + net storage quanta; no envelope term, no floor. The v1
        # product prices ride the work term (see _operation_model).
        work = _mul_shift(model.work_units, config.fee_multiplier, config.fee_shift)
        block_data = params.payload_bytes + model.result_bytes + net_quanta
        expected = min(work + block_data * config.gas_fee_per_byte, _U64_MAX)
        # Offer shape mirrors the node's own test agent: a 2x minimum-offer pad plus a flat 1 KiB
        # block-data allowance on top of the work term.
        byte_allowance = max(block_data, 1024)
        max_gas = min(config.minimum_gas_offer * 2 + work + byte_allowance * config.gas_fee_per_byte, _U64_MAX)

    return NativeFeeEstimate(
        max_gas=max_gas,
        max_data=min(model.new_quanta * config.data_escrow_per_row, _U64_MAX),
        expected_gas_bill=expected,
        new_storage_quanta=model.new_quanta,
        deleted_storage_quanta=model.deleted_quanta,
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
        # distinct series in the call - the chain remembers the supply it already read, so the
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
        # meta-id row is counted the same way here - but see deleted_storage_quanta: on a burn this
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
    # impossible - every series in the call is written into by at least one instance - and catching
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
    # the id can tell - an unknown id is priced as paid), every returned instance moves its lookup
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
    # Refusing beats guessing: see _MAX_PRICEABLE_LENGTH. The transaction may well be admitted - this
    # says only that no honest price can be quoted for it offline.
    if length > _MAX_PRICEABLE_LENGTH:
        raise BuilderError(
            f"estimate_native_fee: {param_name} {length} is longer than {_MAX_PRICEABLE_LENGTH} "
            "and cannot be priced offline"
        )
    return shift
