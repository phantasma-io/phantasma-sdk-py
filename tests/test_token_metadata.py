"""Token metadata: the contract seal, the keys the chain reserves, and the Token.UpdateTokenMetadata
builder.

The chain keeps the metadata keys that start with _ for its own settings: staking, inflation and the
contract seal. It refuses any other such key in the metadata a token is left with. It refuses _brn in
an update, because only genesis sets it. The seal is the field _sealed_c holding the Int64 1. The
chain refuses a VM contract for a sealed token, a removal of the seal, and _sealed_c of any other
type or value. An update may name another _ key only to remove it, with an empty Array_Dynamic, the
only value the chain reads as a removal. These tests hold the SDK's builders to the same rules."""

from __future__ import annotations

import pytest
from test_fee_plan import CREATOR, CREATOR_KEYS, config

from phantasma_py import (
    BuilderError,
    FeePlanOptions,
    NativeFeeKind,
    PlanAndSignOptions,
    build_contract_seal_update,
    build_token_metadata,
    build_update_token_metadata_tx,
    build_update_token_metadata_tx_and_sign,
    build_update_token_metadata_tx_and_sign_hex,
    plan_fees,
)
from phantasma_py.carbon import (
    Bytes32,
    ModuleID,
    SignedTxMsg,
    TokenContractMethod,
    TxLimits,
    TxMsg,
    TxMsgCall,
    TxType,
    UpdateTokenMetadataArgs,
    VMDynamicStruct,
    VMDynamicVariable,
    VMNamedDynamicVariable,
    VMType,
    deserialize,
)

OWNER = Bytes32(bytes([3]) * 32)

# The four string fields of creation_fields() as a struct writes them: per field a length-prefixed
# name, the type byte of String (0x16) and the zero-terminated value. The writer sorts the fields by
# name.
STRING_FIELDS_HEX = (
    "0B6465736372697074696F6E164120746F6B656E20776974686F7574206120636F6E747261637400"  # description
    "0469636F6E16646174613A696D6167652F706E673B6261736536342C6956424F5277304B47676F3D00"  # icon
    "046E616D65165365616C656420746F6B656E00"  # name
    "0375726C1668747470733A2F2F6578616D706C652E696E76616C69642F7365616C656400"  # url
)
# The metadata the TypeScript SDK writes for creation_fields(), without the seal and with it. A struct
# starts with its u32 field count. _sealed_c sorts before the string fields, so the seal is the first
# field: its name, the type byte of Int64 (0x0C) and the value 1.
PLAIN_CREATION_HEX = "04000000" + STRING_FIELDS_HEX
SEALED_CREATION_HEX = "05000000" + "09" + b"_sealed_c".hex().upper() + "0C" + "0100000000000000" + STRING_FIELDS_HEX


def creation_fields() -> dict[str, str]:
    return {
        "name": "Sealed token",
        "icon": "data:image/png;base64,iVBORw0KGgo=",
        "url": "https://example.invalid/sealed",
        "description": "A token without a contract",
    }


def update(*fields: VMNamedDynamicVariable) -> VMDynamicStruct:
    return VMDynamicStruct(list(fields))


def removal(key: str) -> VMNamedDynamicVariable:
    """Returns the field as an empty Array_Dynamic, the only value the chain reads as a removal."""
    return VMNamedDynamicVariable.make(key, VMType.ARRAY_DYNAMIC, [])


def call_args(msg: TxMsg) -> bytes:
    assert isinstance(msg.msg, TxMsgCall)
    return msg.msg.args


def refusal(token_id: int, metadata: VMDynamicStruct) -> str:
    """Returns the message of the error that build_update_token_metadata_tx raises for these inputs.
    The test fails if it raises none."""
    with pytest.raises(BuilderError) as refused:
        build_update_token_metadata_tx(token_id, metadata, OWNER)
    return str(refused.value)


def test_seals_a_token_at_creation_with__sealed_c_as_the_int64_1() -> None:
    raw = build_token_metadata(creation_fields(), seal_contract=True)
    assert raw.hex().upper() == SEALED_CREATION_HEX
    metadata = deserialize(raw, VMDynamicStruct)
    assert isinstance(metadata, VMDynamicStruct)
    assert metadata.get("_sealed_c") == VMDynamicVariable(VMType.INT64, 1)
    assert metadata.get("name") == VMDynamicVariable(VMType.STRING, "Sealed token")
    assert len(metadata.fields) == 5


def test_writes_no_seal_without_the_option() -> None:
    raw = build_token_metadata(creation_fields())
    assert raw.hex().upper() == PLAIN_CREATION_HEX
    metadata = deserialize(raw, VMDynamicStruct)
    assert isinstance(metadata, VMDynamicStruct)
    assert metadata.get("_sealed_c") is None


def test_refuses__sealed_c_among_the_string_fields() -> None:
    fields = creation_fields()
    fields["_sealed_c"] = "1"
    with pytest.raises(BuilderError) as refused:
        build_token_metadata(fields)
    assert str(refused.value) == (
        "pass seal_contract=True to seal a token against contracts: "
        "_sealed_c holds the Int64 1, which a string field cannot carry"
    )


# _sl is a chain key, and the chain takes it only as an Int64. These fields are strings.
@pytest.mark.parametrize("key", ["_note", "_sl", "_brn", "_phs"])
def test_refuses_a_string_field_whose_key_starts_with_an_underscore(key: str) -> None:
    fields = creation_fields()
    fields[key] = "x"
    with pytest.raises(BuilderError) as refused:
        build_token_metadata(fields)
    assert str(refused.value) == f"{key} is reserved for the chain: string metadata fields cannot start with _"


def test_takes_a_key_whose_underscore_is_not_its_first_character() -> None:
    fields = creation_fields()
    fields["my_note"] = "a"
    fields["note_"] = "b"
    metadata = deserialize(build_token_metadata(fields), VMDynamicStruct)
    assert isinstance(metadata, VMDynamicStruct)
    assert metadata.get("my_note") == VMDynamicVariable(VMType.STRING, "a")
    assert metadata.get("note_") == VMDynamicVariable(VMType.STRING, "b")


def test_builds_a_seal_update_that_holds_the_seal_alone() -> None:
    # The chain merges an update field by field, so an update with the seal alone keeps every other
    # field of the token.
    assert build_contract_seal_update().fields == [VMNamedDynamicVariable.make("_sealed_c", VMType.INT64, 1)]


def test_builds_the_call_with_the_token_id_then_the_update_as_its_arguments() -> None:
    msg = build_update_token_metadata_tx(
        42, build_contract_seal_update(), OWNER, TxLimits(max_gas=1, max_data=2, expiry=3)
    )
    assert msg.type == TxType.CALL
    assert msg.gas_from == OWNER
    assert (msg.max_gas, msg.max_data, msg.expiry) == (1, 2, 3)
    assert isinstance(msg.msg, TxMsgCall)
    assert msg.msg.module_id == ModuleID.TOKEN
    assert msg.msg.method_id == TokenContractMethod.UPDATE_TOKEN_METADATA
    # The chain reads a u64 token id, then a dynamic struct: a u32 field count, and per field a
    # length-prefixed name, a type byte (Int64 is 0x0C) and the value. The C# and TypeScript SDKs
    # write the same bytes for this update.
    assert msg.msg.args.hex().upper() == (
        "2A00000000000000" + "01000000" + "09" + b"_sealed_c".hex().upper() + "0C" + "0100000000000000"
    )


@pytest.mark.parametrize("token_id", [0, -1, 1 << 64])
def test_refuses_a_token_id_the_chain_never_assigns(token_id: int) -> None:
    assert refusal(token_id, build_contract_seal_update()) == (
        "token_id must be between 1 and 2^64 - 1: the chain assigns no token id 0"
    )


def test_takes_the_first_and_the_last_token_id_of_the_range() -> None:
    for token_id in (1, (1 << 64) - 1):
        args = deserialize(
            call_args(build_update_token_metadata_tx(token_id, build_contract_seal_update(), OWNER)),
            UpdateTokenMetadataArgs,
        )
        assert isinstance(args, UpdateTokenMetadataArgs)
        assert args.token_id == token_id


def test_refuses_an_update_without_fields() -> None:
    # Such an update would change nothing and still pay the fee.
    assert refusal(42, VMDynamicStruct()) == "a metadata update needs at least one field"


# An Int256 or an Int32 that holds 1 is still refused: the chain takes the seal only as the Int64 1. A
# removal is refused too, because a seal cannot be removed.
@pytest.mark.parametrize(
    ("vm_type", "data"),
    [
        (VMType.STRING, "1"),
        (VMType.INT64, 2),
        (VMType.INT256, 1),
        (VMType.INT32, 1),
        (VMType.ARRAY_DYNAMIC, []),
    ],
    ids=["string", "int64_2", "int256_1", "int32_1", "removal"],
)
def test_refuses_a_seal_other_than_the_int64_1(vm_type: VMType, data: object) -> None:
    seal = VMNamedDynamicVariable.make("_sealed_c", vm_type, data)
    assert refusal(42, update(seal)) == "_sealed_c must be the Int64 1: build it with build_contract_seal_update"


def test_refuses__brn_as_a_value_and_as_a_removal() -> None:
    # Only genesis sets _brn, so the chain refuses an update that names it in any form.
    for field in (VMNamedDynamicVariable.make("_brn", VMType.INT256, 70), removal("_brn")):
        assert refusal(42, update(field)) == "_brn is set at genesis only"


@pytest.mark.parametrize(
    "field",
    [
        VMNamedDynamicVariable.make("_note", VMType.STRING, "x"),
        # The chain reads _phs in block 0 only, so an update may not set it.
        VMNamedDynamicVariable.make("_phs", VMType.BYTES, b"\x01"),
        # A key that resembles the contract seal is not the seal.
        VMNamedDynamicVariable.make("_sealed", VMType.INT64, 1),
        # The chain stores a Dynamic value without data as a value. Only an empty Array_Dynamic
        # removes a field.
        VMNamedDynamicVariable.make("_note", VMType.DYNAMIC, None),
    ],
    ids=["string", "_phs", "_sealed", "dynamic_without_data"],
)
def test_refuses_a_value_for_an_underscore_key_the_chain_does_not_define(field: VMNamedDynamicVariable) -> None:
    assert refusal(42, update(field)) == f"{field.name.value} is reserved for the chain: an update can only remove it"


def test_reports_the_first_broken_rule_in_a_fixed_order() -> None:
    # Each call breaks two rules. The message names the rule that comes first in this order: the token
    # id, the field count, the seal, _brn, the other _ keys. The two updates list the field of the
    # later rule first, so a check in the order of the fields would name the later rule.
    sealed_as_text = VMNamedDynamicVariable.make("_sealed_c", VMType.STRING, "1")
    assert refusal(42, update(removal("_brn"), sealed_as_text)).startswith("_sealed_c must be the Int64 1")
    unknown_key = VMNamedDynamicVariable.make("_a", VMType.STRING, "x")
    assert refusal(42, update(unknown_key, removal("_brn"))).startswith("_brn is set at genesis only")
    assert refusal(0, VMDynamicStruct()).startswith("token_id must be between 1 and 2^64 - 1")


def test_removes_a_reserved_key_with_an_empty_array_dynamic() -> None:
    args = call_args(build_update_token_metadata_tx(42, update(removal("_note")), OWNER))
    # The token id as a u64, a u32 field count, the length-prefixed name, then the type byte of
    # Array_Dynamic (0x01) and its u32 item count 0. The chain reads exactly this value as a removal.
    # The C# and TypeScript SDKs write the same bytes for this update.
    assert args.hex().upper() == "2A00000000000000" + "01000000" + "05" + b"_note".hex().upper() + "01" + "00000000"
    # The writer writes an Array_Dynamic whose data is None as the same empty array.
    without_data = VMNamedDynamicVariable.make("_note", VMType.ARRAY_DYNAMIC, None)
    assert call_args(build_update_token_metadata_tx(42, update(without_data), OWNER)) == args


# The keys an update may set, as the chain names them. The SDK checks only which keys are reserved,
# so an Int64 stands for every value here; the chain checks the type of each setting itself. The
# Int64 1 is also the one value _sealed_c takes.
@pytest.mark.parametrize(
    "key",
    [
        "_soi",
        "_sot",
        "_srt",
        "_srp",
        "_srm",
        "_srd",
        "_sl",
        "_sbt",
        "_sbm",
        "_sbd",
        "_sbl",
        "_ip",
        "_ia",
        "_iib",
        "_iie",
        "_ifr",
        "_sealed_c",
    ],
)
def test_lets_an_update_set_the_chain_key(key: str) -> None:
    field = VMNamedDynamicVariable.make(key, VMType.INT64, 1)
    args = deserialize(call_args(build_update_token_metadata_tx(42, update(field), OWNER)), UpdateTokenMetadataArgs)
    assert isinstance(args, UpdateTokenMetadataArgs)
    assert args.metadata.fields == [field]


def test_update_and_sign_plans_the_call_with_the_script_budget_before_signing() -> None:
    plan = plan_fees(
        build_update_token_metadata_tx(9, build_contract_seal_update(), CREATOR),
        config(),
        FeePlanOptions(witness_count=1),
    )
    # The planner has no formula for this call, so its plan is an upper bound.
    assert plan.kinds == (NativeFeeKind.SCRIPT,)
    assert not plan.exact
    signed = build_update_token_metadata_tx_and_sign(9, build_contract_seal_update(), CREATOR_KEYS, config())
    assert len(signed) == plan.envelope_bytes
    decoded = deserialize(signed, SignedTxMsg)
    assert isinstance(decoded, SignedTxMsg)
    assert (decoded.msg.max_gas, decoded.msg.max_data) == (plan.max_gas, plan.max_data)
    assert [witness.address for witness in decoded.witnesses] == [CREATOR]
    assert deserialize(call_args(decoded.msg), UpdateTokenMetadataArgs) == UpdateTokenMetadataArgs(
        9, build_contract_seal_update()
    )
    as_hex = build_update_token_metadata_tx_and_sign_hex(
        9,
        build_contract_seal_update(),
        CREATOR_KEYS,
        config(),
        PlanAndSignOptions(limits=TxLimits(expiry=decoded.msg.expiry)),
    )
    assert as_hex == signed.hex()


def test_update_and_sign_keeps_an_offer_the_caller_fixed_and_needs_no_config_for_it() -> None:
    options = PlanAndSignOptions(limits=TxLimits(max_gas=55_000_000, max_data=7))
    signed = build_update_token_metadata_tx_and_sign(9, build_contract_seal_update(), CREATOR_KEYS, None, options)
    decoded = deserialize(signed, SignedTxMsg)
    assert isinstance(decoded, SignedTxMsg)
    assert (decoded.msg.max_gas, decoded.msg.max_data) == (55_000_000, 7)
    with pytest.raises(BuilderError, match="gas config"):
        build_update_token_metadata_tx_and_sign(9, build_contract_seal_update(), CREATOR_KEYS)
