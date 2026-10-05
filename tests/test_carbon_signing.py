"""Multi-witness signing: the signer set is matched to the envelope slots the message fixes, or
taken in the caller's order for the witness-array types, and every witness signs the same bytes."""

from __future__ import annotations

import pytest

from phantasma_py.carbon import (
    Bytes32,
    SignedTxMsg,
    SmallString,
    TxMsg,
    TxMsgCall,
    TxMsgPhantasmaRaw,
    TxMsgTransferFungible,
    TxMsgTransferFungibleGasPayer,
    TxType,
    bytes32_from_public_key,
    deserialize,
    serialize,
    sign_and_serialize_tx_msg,
    sign_and_serialize_tx_msg_with,
    sign_tx_msg,
    sign_tx_msg_with,
)
from phantasma_py.crypto import PhantasmaKeys
from phantasma_py.errors import BuilderError

PAYER_KEYS = PhantasmaKeys.from_wif("KwPpBSByydVKqStGHAnZzQofCqhDmD2bfRgc9BmZqM3ZmsdWJw4d")
OWNER_KEYS = PhantasmaKeys.from_wif("KwVG94yjfVg1YKFyRxAGtug93wdRbmLnqqrFV6Yd2CiA9KZDAp4H")
PAYER = bytes32_from_public_key(PAYER_KEYS.public_key)
OWNER = bytes32_from_public_key(OWNER_KEYS.public_key)
RECEIVER = Bytes32(bytes([0x33]) * 32)


def gas_payer_transfer(payer: Bytes32, owner: Bytes32) -> TxMsg:
    return TxMsg(
        TxType.TRANSFER_FUNGIBLE_GAS_PAYER,
        1_759_711_416_000,
        10_000_000,
        0,
        payer,
        SmallString(""),
        TxMsgTransferFungibleGasPayer(RECEIVER, owner, 1, 5),
    )


def call_tx(gas_from: Bytes32) -> TxMsg:
    return TxMsg(
        TxType.CALL, 1_759_711_416_000, 10_000_000, 0, gas_from, SmallString(""), TxMsgCall(1, 1, b"\x01\x02\x03")
    )


def native_transfer(sender: Bytes32, to: Bytes32, max_gas: int) -> TxMsg:
    return TxMsg(TxType.TRANSFER_FUNGIBLE, 1, max_gas, 0, sender, SmallString(""), TxMsgTransferFungible(to, 1, 1))


class CountingSigner:
    """A signer that answers through the protocol the way a hardware wallet or a signing service
    would, and counts how often it was asked."""

    def __init__(self, keys: PhantasmaKeys) -> None:
        self.keys = keys
        self.calls = 0

    @property
    def public_key(self) -> bytes:
        return self.keys.public_key

    def sign_message(self, message: bytes) -> bytes:
        self.calls += 1
        return self.keys.sign(message).data


def test_keys_are_ordered_into_the_envelope_order() -> None:
    msg = gas_payer_transfer(PAYER, OWNER)
    # Keys are given owner first; the envelope wants the gas payer first, as the node reads them.
    signed = sign_tx_msg(msg, OWNER_KEYS, PAYER_KEYS)
    assert [w.address for w in signed.witnesses] == [PAYER, OWNER]
    message = serialize(msg)
    assert bytes(signed.witnesses[0].signature) == PAYER_KEYS.sign(message).data
    assert bytes(signed.witnesses[1].signature) == OWNER_KEYS.sign(message).data
    encoded = serialize(signed)
    assert serialize(deserialize(encoded, SignedTxMsg)) == encoded


def test_signers_sign_through_the_protocol() -> None:
    msg = gas_payer_transfer(PAYER, OWNER)
    payer_signer = CountingSigner(PAYER_KEYS)
    owner_signer = CountingSigner(OWNER_KEYS)
    through_signers = sign_and_serialize_tx_msg_with(msg, owner_signer, payer_signer)
    through_keys = sign_and_serialize_tx_msg(msg, PAYER_KEYS, OWNER_KEYS)
    assert through_signers == through_keys
    assert (payer_signer.calls, owner_signer.calls) == (1, 1)
    # In-memory keys are signers too.
    assert sign_and_serialize_tx_msg_with(msg, PAYER_KEYS, OWNER_KEYS) == through_keys


# The same account pays the gas and owns the tokens: two envelope slots, one signature.
def test_a_double_slot_signer_is_asked_once() -> None:
    msg = gas_payer_transfer(PAYER, PAYER)
    signer = CountingSigner(PAYER_KEYS)
    signed = sign_tx_msg_with(msg, signer)
    assert signer.calls == 1
    assert len(signed.witnesses) == 2
    assert signed.witnesses[0].signature == signed.witnesses[1].signature


def test_signer_sets_that_do_not_match_the_message_are_refused() -> None:
    stranger = PhantasmaKeys.generate()
    msg = gas_payer_transfer(PAYER, OWNER)
    with pytest.raises(BuilderError, match=f"no signer for witness {OWNER.hex()}"):
        sign_tx_msg(msg, PAYER_KEYS)
    with pytest.raises(BuilderError, match="is not a witness of this transaction"):
        sign_tx_msg(msg, PAYER_KEYS, OWNER_KEYS, stranger)
    with pytest.raises(BuilderError, match="no signer for witness"):
        sign_tx_msg(native_transfer(PAYER, OWNER, 1), OWNER_KEYS)
    # A raw Phantasma transaction carries no witnesses at all.
    raw = TxMsg(TxType.PHANTASMA_RAW, 1, 1, 0, PAYER, SmallString(""), TxMsgPhantasmaRaw(b"\x01"))
    with pytest.raises(BuilderError, match="carry no witnesses"):
        sign_tx_msg(raw, PAYER_KEYS)
    assert sign_tx_msg(raw).witnesses == []


def test_witness_array_transactions_keep_the_caller_order() -> None:
    stranger = PhantasmaKeys.generate()
    signed = sign_tx_msg(call_tx(PAYER), OWNER_KEYS, PAYER_KEYS, stranger)
    assert [w.address for w in signed.witnesses] == [OWNER, PAYER, bytes32_from_public_key(stranger.public_key)]
    decoded = deserialize(serialize(signed), SignedTxMsg)
    assert isinstance(decoded, SignedTxMsg)
    assert len(decoded.witnesses) == 3
    # The node rejects a call the gas payer did not sign; the SDK refuses to build one.
    with pytest.raises(BuilderError, match="gas payer"):
        sign_tx_msg(call_tx(PAYER), OWNER_KEYS)
    with pytest.raises(BuilderError, match="at least one witness"):
        sign_tx_msg(call_tx(PAYER))


# A zero offer can never be admitted, so it marks a message that was built but not planned; signing
# it would only produce a rejection.
def test_an_unplanned_message_is_refused() -> None:
    with pytest.raises(BuilderError, match="no gas offer"):
        sign_tx_msg(native_transfer(PAYER, OWNER, 0), PAYER_KEYS)


# The chain reads a native transfer amount as a signed 64-bit value that must be above zero, so zero
# and 2^63 fail on chain while 1 and the int64 maximum do not. Both transfer types and both signing
# paths refuse the first two before anything is signed. A Python int can also be negative, which the
# same check refuses.
def test_a_native_transfer_amount_the_chain_refuses_is_refused() -> None:
    int64_max = (1 << 63) - 1

    def plain(amount: int) -> TxMsg:
        return TxMsg(
            TxType.TRANSFER_FUNGIBLE, 1, 42_600_000, 0, PAYER, SmallString(""), TxMsgTransferFungible(OWNER, 1, amount)
        )

    def with_gas_payer(amount: int) -> TxMsg:
        msg = gas_payer_transfer(PAYER, OWNER)
        msg.msg.amount = amount
        return msg

    with pytest.raises(BuilderError, match="int64 maximum"):
        sign_tx_msg(plain(int64_max + 1), PAYER_KEYS)
    with pytest.raises(BuilderError, match="int64 maximum"):
        sign_tx_msg(with_gas_payer(int64_max + 1), PAYER_KEYS, OWNER_KEYS)
    signer = CountingSigner(PAYER_KEYS)
    with pytest.raises(BuilderError, match="int64 maximum"):
        sign_tx_msg_with(plain(int64_max + 1), signer)
    assert signer.calls == 0

    with pytest.raises(BuilderError, match="above zero"):
        sign_tx_msg(plain(0), PAYER_KEYS)
    with pytest.raises(BuilderError, match="above zero"):
        sign_tx_msg(plain(-1), PAYER_KEYS)
    with pytest.raises(BuilderError, match="above zero"):
        sign_tx_msg(with_gas_payer(0), PAYER_KEYS, OWNER_KEYS)
    with pytest.raises(BuilderError, match="above zero"):
        sign_tx_msg_with(plain(0), signer)
    assert signer.calls == 0

    for amount in (1, int64_max):
        assert len(sign_tx_msg(plain(amount), PAYER_KEYS).witnesses) == 1
        assert len(sign_tx_msg(with_gas_payer(amount), PAYER_KEYS, OWNER_KEYS).witnesses) == 2


# The single-witness path keeps its historical behaviour and is the same signature the signer
# protocol produces.
def test_the_single_witness_path_still_signs_the_historical_way() -> None:
    msg = native_transfer(PAYER, OWNER, 10_000_000)
    signed = sign_tx_msg(msg, PAYER_KEYS)
    assert [w.address for w in signed.witnesses] == [PAYER]
    assert bytes(signed.witnesses[0].signature) == PAYER_KEYS.sign(serialize(msg)).data
    assert serialize(sign_tx_msg_with(msg, CountingSigner(PAYER_KEYS))) == serialize(signed)
    assert sign_and_serialize_tx_msg(msg, PAYER_KEYS) == serialize(signed)
