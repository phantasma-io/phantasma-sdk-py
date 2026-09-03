"""Envelope tests: the layout the chain reads for each witness form, and the size prediction the
gas-model-v2 fee planning is built on (every envelope byte is billed, so the size the planner
prices must be the size that goes on the wire)."""

from __future__ import annotations

import pytest

from phantasma_py.carbon import (
    Bytes32,
    Bytes64,
    IntX,
    SignedTxMsg,
    SmallString,
    TxMsg,
    TxMsgBurnFungibleGasPayer,
    TxMsgBurnNonFungibleGasPayer,
    TxMsgCall,
    TxMsgPhantasmaRaw,
    TxMsgTransferFungible,
    TxMsgTransferFungibleGasPayer,
    TxMsgTransferNonFungibleMultiGasPayer,
    TxMsgTransferNonFungibleSingleGasPayer,
    TxPayload,
    TxType,
    Witness,
    bytes32_from_public_key,
    deserialize,
    envelope_bytes,
    required_witnesses,
    serialize,
)
from phantasma_py.crypto import PhantasmaKeys
from phantasma_py.errors import BuilderError, SerializationError

# Deterministic, non-funded keys: the gas payer and the token owner of a gas-payer transaction.
PAYER_KEYS = PhantasmaKeys.from_wif("KwPpBSByydVKqStGHAnZzQofCqhDmD2bfRgc9BmZqM3ZmsdWJw4d")
OWNER_KEYS = PhantasmaKeys.from_wif("KwVG94yjfVg1YKFyRxAGtug93wdRbmLnqqrFV6Yd2CiA9KZDAp4H")
PAYER = bytes32_from_public_key(PAYER_KEYS.public_key)
OWNER = bytes32_from_public_key(OWNER_KEYS.public_key)
RECEIVER = Bytes32(bytes([0x33]) * 32)


def base_tx(tx_type: TxType, gas_from: Bytes32, msg: TxPayload) -> TxMsg:
    return TxMsg(tx_type, 1_759_711_416_000, 10_000_000, 1_000, gas_from, SmallString("p"), msg)


def gas_payer_messages() -> list[TxMsg]:
    """One message of every gas-payer type: OWNER owns the tokens, PAYER pays the gas."""
    return [
        base_tx(
            TxType.TRANSFER_FUNGIBLE_GAS_PAYER, PAYER, TxMsgTransferFungibleGasPayer(RECEIVER, OWNER, 1, 100_000_000)
        ),
        base_tx(
            TxType.TRANSFER_NON_FUNGIBLE_SINGLE_GAS_PAYER,
            PAYER,
            TxMsgTransferNonFungibleSingleGasPayer(RECEIVER, OWNER, 7, 42),
        ),
        base_tx(
            TxType.TRANSFER_NON_FUNGIBLE_MULTI_GAS_PAYER,
            PAYER,
            TxMsgTransferNonFungibleMultiGasPayer(RECEIVER, OWNER, 7, [42, 43]),
        ),
        base_tx(TxType.BURN_FUNGIBLE_GAS_PAYER, PAYER, TxMsgBurnFungibleGasPayer(1, OWNER, IntX(5))),
        base_tx(TxType.BURN_NON_FUNGIBLE_GAS_PAYER, PAYER, TxMsgBurnNonFungibleGasPayer(7, OWNER, 42)),
    ]


def raw_signature(keys: PhantasmaKeys, msg: TxMsg) -> Bytes64:
    return Bytes64(keys.sign(serialize(msg)).data)


def witness(keys: PhantasmaKeys, msg: TxMsg) -> Witness:
    return Witness(bytes32_from_public_key(keys.public_key), raw_signature(keys, msg))


def call_tx(args: bytes = bytes(10)) -> TxMsg:
    return base_tx(TxType.CALL, PAYER, TxMsgCall(1, 2, args))


# The chain reads the gas payer's signature first and the owner's second, so the envelope is the
# unsigned message followed by exactly those two bare signatures in that order. A wrong order is
# not a decoding error - both signatures verify against addresses the chain resolves itself - so
# only the byte layout can catch it.
def test_gas_payer_envelopes_write_the_gas_signature_before_the_from_signature() -> None:
    for msg in gas_payer_messages():
        unsigned = serialize(msg)
        gas_sig = raw_signature(PAYER_KEYS, msg)
        from_sig = raw_signature(OWNER_KEYS, msg)
        encoded = serialize(SignedTxMsg(msg, [witness(PAYER_KEYS, msg), witness(OWNER_KEYS, msg)]))

        assert len(encoded) == len(unsigned) + 128, msg.type
        assert encoded[: len(unsigned)] == unsigned
        assert encoded[len(unsigned) : len(unsigned) + 64] == bytes(gas_sig)
        assert encoded[len(unsigned) + 64 :] == bytes(from_sig)

        # The envelope does not carry the second witness's address; the reader must recover it
        # from the payload's from_address, exactly as the node does.
        decoded = deserialize(encoded, SignedTxMsg)
        assert isinstance(decoded, SignedTxMsg)
        assert [w.address for w in decoded.witnesses] == [PAYER, OWNER]
        assert serialize(decoded) == encoded


def test_envelopes_refuse_witness_sets_that_do_not_match_the_message() -> None:
    msg = gas_payer_messages()[0]
    gas = witness(PAYER_KEYS, msg)
    frm = witness(OWNER_KEYS, msg)
    for witnesses, expected in [
        ([gas], "expects 2 witnesses"),
        ([frm, gas], "gas witness address mismatch"),
        ([gas, gas], "from witness address mismatch"),
    ]:
        with pytest.raises(SerializationError, match=expected):
            serialize(SignedTxMsg(msg, witnesses))

    # A witness-array envelope that omits the gas payer would be rejected by the node as "not
    # signed by gas payer"; the SDK refuses to produce it.
    call = call_tx()
    with pytest.raises(SerializationError, match="gas payer must be one of the witnesses"):
        serialize(SignedTxMsg(call, [witness(OWNER_KEYS, call)]))


# The envelope size decides the v2 gas bill, and a wallet must know it before anyone signs: the
# placeholder-witness size must equal the size of the really signed transaction.
def test_envelope_bytes_predicts_the_signed_size() -> None:
    for msg in gas_payer_messages():
        want = len(serialize(SignedTxMsg(msg, [witness(PAYER_KEYS, msg), witness(OWNER_KEYS, msg)])))
        assert envelope_bytes(msg) == want
        assert envelope_bytes(msg, 2) == want
        with pytest.raises(BuilderError, match=r"carries 2 witness\(es\)"):
            envelope_bytes(msg, 1)

    native = base_tx(TxType.TRANSFER_FUNGIBLE, PAYER, TxMsgTransferFungible(RECEIVER, 1, 1))
    assert envelope_bytes(native) == len(serialize(SignedTxMsg(native, [witness(PAYER_KEYS, native)])))

    call = call_tx()
    signers = [PAYER_KEYS, OWNER_KEYS, PhantasmaKeys.generate()]
    for count in (1, 2, 3):
        signed = SignedTxMsg(call, [witness(keys, call) for keys in signers[:count]])
        assert envelope_bytes(call, count) == len(serialize(signed))
    assert envelope_bytes(call) == envelope_bytes(call, 1)
    with pytest.raises(BuilderError, match="at least one witness"):
        envelope_bytes(call, 0)


def test_required_witnesses_names_the_envelope_order() -> None:
    for msg in gas_payer_messages():
        assert required_witnesses(msg) == [PAYER, OWNER]
    native = base_tx(TxType.TRANSFER_FUNGIBLE, PAYER, TxMsgTransferFungible(OWNER, 1, 1))
    assert required_witnesses(native) == [PAYER]
    # Witness-array types leave the witness set to the caller; a raw Phantasma transaction has none.
    assert required_witnesses(call_tx(b"")) is None
    raw = base_tx(TxType.PHANTASMA_RAW, PAYER, TxMsgPhantasmaRaw(b"\x01"))
    assert required_witnesses(raw) == []
