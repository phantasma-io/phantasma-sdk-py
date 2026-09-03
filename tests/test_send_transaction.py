"""The one-step send: pre-flight, fee plan, signatures, broadcast - against a canned node."""

from __future__ import annotations

import pytest
from _canned_node import (
    OWNER,
    OWNER_KEYS,
    PAYER,
    PAYER_KEYS,
    CannedNode,
    burn_tx,
    create_token_tx,
    register_name_tx,
    transfer_tx,
)

from phantasma_py import (
    FeePlanOptions,
    InfusedAsset,
    PreflightError,
    PreflightResult,
    PreflightVerdict,
    RPCError,
    SendTransactionOptions,
    plan_fees,
)
from phantasma_py.carbon import get_nft_address


def test_plans_an_unplanned_message_signs_it_and_broadcasts_the_envelope() -> None:
    node = CannedNode()
    msg = transfer_tx(OWNER, PAYER)
    assert node.client().send_transaction(msg, [OWNER_KEYS]) == "HASH"
    sent = node.decode_sent()
    assert sent.msg.max_gas == 42_600_000
    assert len(sent.witnesses) == 1
    assert msg.max_gas == 0, "the input must be left unplanned"


def test_sends_a_message_the_caller_already_planned_as_it_is() -> None:
    node = CannedNode()
    node.client().send_transaction(transfer_tx(OWNER, PAYER, max_gas=55_000_000), [OWNER_KEYS])
    assert node.decode_sent().msg.max_gas == 55_000_000


def test_collects_every_witness_of_a_gas_payer_transfer() -> None:
    node = CannedNode()
    node.client().send_transaction(transfer_tx(OWNER, PAYER, gas_payer=PAYER), [OWNER_KEYS, PAYER_KEYS])
    sent = node.decode_sent()
    assert [w.address for w in sent.witnesses] == [PAYER, OWNER]
    assert len(node.sent[0]) == (106 + 32 + 128) * 2


# CreateToken consumes its policy fee before checking the symbol, so a taken symbol is refused here,
# before anything is signed or sent.
def test_refuses_to_create_a_token_whose_symbol_is_taken() -> None:
    node = CannedNode()
    node.tokens["TAKEN"] = {"symbol": "TAKEN", "carbonId": "5"}
    client = node.client()
    with pytest.raises(PreflightError):
        client.send_transaction(create_token_tx(OWNER, "TAKEN"), [OWNER_KEYS])
    assert node.sent == []
    client.send_transaction(create_token_tx(OWNER, "FRESH"), [OWNER_KEYS])
    assert len(node.sent) == 1


# The pre-flight covers token creation and nothing else. A name registration is sent without a
# lookup: the node reports a free name as an error and a taken one as an address, so there is no
# answer that means "free" to check against.
def test_sends_a_name_registration_without_looking_anything_up() -> None:
    node = CannedNode()
    node.client().send_transaction(register_name_tx(OWNER, "alice"), [OWNER_KEYS])
    assert len(node.sent) == 1
    assert node.lookups == 0


# A free symbol is established, not inferred from the error text: the node refuses to answer about
# FRESH, so the check asks it for a token that certainly exists. That answer proves the lookup works
# and is being truthful, which is what makes the refusal about FRESH mean "absent". Whatever the
# error says is irrelevant - including "Method not found", the JSON-RPC name of error -32601, which
# contains the words "not found" and means the question was never asked.
def test_establishes_a_free_symbol_from_a_control_lookup_not_from_the_error_text() -> None:
    node = CannedNode()
    client = node.client()
    for text in ("Method not found", "backend unavailable", "Token symbol not found"):
        node.lookup_error = text
        client.send_transaction(create_token_tx(OWNER, "FRESH"), [OWNER_KEYS])
    assert len(node.sent) == 3


# And the case the whole check exists for: a node that cannot answer at all. Nothing is established,
# so nothing is signed - the policy fee is not spent on a guess.
def test_refuses_when_the_lookup_cannot_answer_even_about_a_token_that_exists() -> None:
    node = CannedNode()
    node.reachable = False
    client = node.client()
    for text in ("Method not found", "backend unavailable", "Token symbol not found"):
        node.lookup_error = text
        with pytest.raises(PreflightError, match="could not establish whether token symbol FRESH is taken") as info:
            client.send_transaction(create_token_tx(OWNER, "FRESH"), [OWNER_KEYS])
        assert text in str(info.value)
    assert node.sent == []


# send_transaction acts on one verdict only. A caller who wants to stop on the others reads the
# verdict itself and sends separately - which is the whole reason the check reports one instead of
# deciding. This is that path, and it is the one a wallet uses to warn before spending the fee.
def test_preflight_hands_the_verdict_to_a_caller_who_wants_to_decide_for_itself() -> None:
    node = CannedNode()
    node.lookup_error = "Method not found"
    client = node.client()
    assert client.preflight_transaction(create_token_tx(OWNER, "FRESH")) == PreflightResult(
        PreflightVerdict.FREE, "token symbol FRESH"
    )

    # A client that cannot offer a control token - its gas config is unreadable - has nothing to
    # check the refusal against, so it says so rather than picking a side.
    blind = CannedNode()
    blind.lookup_error = "Method not found"
    blind.gas_config_error = "backend unavailable"
    assert blind.client().preflight_transaction(create_token_tx(OWNER, "FRESH")) == PreflightResult(
        PreflightVerdict.UNKNOWN, "token symbol FRESH", "Method not found"
    )

    assert client.preflight_transaction(register_name_tx(OWNER, "alice")).verdict is PreflightVerdict.NOT_APPLICABLE

    client.send_transaction(create_token_tx(OWNER, "FRESH"), [OWNER_KEYS], SendTransactionOptions(skip_preflight=True))
    assert len(node.sent) == 1


# A gas-payer envelope always carries two signatures, even when one account pays for its own
# transfer: the number of witness slots comes from the message, never from the signer list.
def test_sends_a_gas_payer_transfer_whose_payer_and_owner_are_the_same_account() -> None:
    node = CannedNode()
    node.client().send_transaction(transfer_tx(OWNER, PAYER, gas_payer=OWNER), [OWNER_KEYS])
    sent = node.decode_sent()
    assert len(sent.witnesses) == 2
    assert sent.witnesses[0].signature == sent.witnesses[1].signature
    assert len(node.sent[0]) == (106 + 32 + 128) * 2
    assert sent.msg.max_gas == 66_600_000


# A burn is sent for what the NFT holds: the one-step path reads the NFT address through the
# account queries and prices every returned asset, so the burn is not short by them.
def test_reads_what_a_burned_nft_holds_and_prices_its_return() -> None:
    node = CannedNode()
    kcal = {"symbol": "KCAL", "carbonId": "1"}
    gpx = {"symbol": "GPX", "carbonId": "97"}
    art = {"symbol": "ART", "carbonId": "9"}
    for token in (kcal, gpx, art):
        node.tokens[token["symbol"]] = token
    nft_address = get_nft_address(9, 5).hex()
    node.fungible[nft_address] = [
        {"chain": "main", "symbol": "KCAL", "amount": "1", "decimals": 10},
        {"chain": "main", "symbol": "GPX", "amount": "5", "decimals": 8},
    ]
    node.owned_nfts[nft_address] = [(art, "2")]
    client = node.client()

    burn = burn_tx(OWNER, 9, 5)
    client.send_transaction(burn, [OWNER_KEYS])
    sent = node.decode_sent()

    config = client.fees.config()
    holdings = [
        InfusedAsset(token_id=1),
        InfusedAsset(token_id=97),
        InfusedAsset(token_id=9, non_fungible=True, instance_count=2),
    ]
    expected = plan_fees(burn, config, FeePlanOptions(infusions=holdings))
    assert (sent.msg.max_gas, sent.msg.max_data) == (expected.max_gas, expected.max_data)
    # KCAL and GPX: a transfer and a query each; ART: a query, two transfers, a query - 80 units.
    empty = plan_fees(burn, config, FeePlanOptions(infusions=[]))
    assert sent.msg.max_gas - empty.max_gas == 800_000

    # The reader itself, as a caller of the planner would use it.
    assert client.infused_assets(9, 5) == holdings

    # An NFT the node knows nothing about holds nothing: the plan is the plain burn.
    node.sent.clear()
    client.send_transaction(burn_tx(OWNER, 9, 6), [OWNER_KEYS])
    assert node.decode_sent().msg.max_gas == empty.max_gas


def test_surfaces_a_broadcast_rejection_as_an_error() -> None:
    node = CannedNode()
    node.send_error = "mempool full"
    with pytest.raises(RPCError, match="mempool full"):
        node.client().send_transaction(transfer_tx(OWNER, PAYER), [OWNER_KEYS])
