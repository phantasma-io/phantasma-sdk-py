"""Token metadata: the contract seal and the Token.UpdateTokenMetadata builder.

The seal is the field _sealed_c holding the Int64 1. The chain refuses a VM contract for a sealed
token, a removal of the seal, and _sealed_c of any other type or value. An update removes a field
with an empty Array_Dynamic, the only value the chain reads as a removal. These tests hold the SDK's
builders to the same rules."""

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
