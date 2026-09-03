"""A canned node for the RPC-side fee tests: a session whose answers are scripted - the gas config,
the token lookups a pre-flight makes, the account queries an infusion read makes, and the
broadcast, which records what it was given."""

from __future__ import annotations

from typing import Any

from phantasma_py.carbon import (
    Bytes32,
    GovernanceContractMethod,
    IntX,
    ModuleID,
    RegisterNameArgs,
    SignedTxMsg,
    SmallString,
    TxLimits,
    TxMsg,
    TxMsgCall,
    TxType,
    build_burn_non_fungible_tx,
    build_create_token_tx,
    build_token_info,
    build_token_metadata,
    build_transfer_fungible_tx,
    bytes32_from_public_key,
    deserialize,
    serialize,
)
from phantasma_py.crypto import PhantasmaKeys
from phantasma_py.rpc import PhantasmaRPC

# The getGasConfig response of the mainnet gas-model-v2 configuration (special resolution #79).
MAINNET_GAS_CONFIG: dict[str, Any] = {
    "gasModelVersion": 2,
    "blockRateTarget": 2000,
    "expiryWindow": 3600000,
    "unitsPerBlockDataByte": 25,
    "gasConfig": {
        "version": 1,
        "maxNameLength": 255,
        "maxTokenSymbolLength": 255,
        "feeShift": 0,
        "maxStructureSize": 1048576,
        "feeMultiplier": "10000",
        "gasTokenId": "1",
        "dataTokenId": "2",
        "minimumGasOffer": "10",
        "dataEscrowPerRow": "200000",
        "gasFeeTransfer": "10",
        "gasFeeQuery": "10",
        "gasFeeCreateTokenBase": "10000000000",
        "gasFeeCreateTokenSymbol": "10000000000",
        "gasFeeCreateTokenSeries": "2500000000",
        "gasFeePerByte": "250000",
        "gasFeeRegisterName": "10000000000000",
        "gasBurnRatioMul": "1",
        "gasBurnRatioShift": 0,
        "minimumGasBill": "10000000",
        "gasProducerRatioMul": "0",
        "gasProducerRatioShift": 0,
        "gasDappRatioMul": "0",
        "gasDappRatioShift": 0,
        "policyFeeCreateTokenBase": "100000000000000",
        "policyFeeCreateTokenSymbol": "100000000000000",
        "policyFeeCreateTokenSeries": "25000000000000",
        "policyFeeRegisterName": "100000000000000000",
        "legacyDataEscrowPerRow": "2",
    },
}

OWNER_KEYS = PhantasmaKeys.from_wif("KwPpBSByydVKqStGHAnZzQofCqhDmD2bfRgc9BmZqM3ZmsdWJw4d")
PAYER_KEYS = PhantasmaKeys.from_wif("KwVG94yjfVg1YKFyRxAGtug93wdRbmLnqqrFV6Yd2CiA9KZDAp4H")
OWNER = bytes32_from_public_key(OWNER_KEYS.public_key)
PAYER = bytes32_from_public_key(PAYER_KEYS.public_key)


class CannedResponse:
    def __init__(self, body: dict[str, Any], status_code: int = 200) -> None:
        self._body = body
        self.status_code = status_code

    def json(self) -> dict[str, Any]:
        return self._body


class CannedNode:
    """An HTTPSession that answers like a node would."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.sent: list[str] = []
        self.tokens: dict[str, dict[str, Any]] = {}
        self.lookups = 0
        self.gas_config_reads = 0
        self.infusion_reads = 0
        # What the node answers for something that is not there; changed to simulate a broken node.
        self.lookup_error = "Token symbol not found"
        # Whether the node answers lookups at all; False simulates one that cannot serve getToken.
        self.reachable = True
        # The node's refusal of the broadcast / of getGasConfig, when set.
        self.send_error: str | None = None
        self.gas_config_error: str | None = None
        # Fungible balances per Carbon-hex address; NFT tokens held per address with the instance
        # count each.
        self.fungible: dict[str, list[dict[str, Any]]] = {}
        self.owned_nfts: dict[str, list[tuple[dict[str, Any], str]]] = {}

    def client(self, **kwargs: Any) -> PhantasmaRPC:
        return PhantasmaRPC("http://canned.invalid/rpc", session=self, **kwargs)

    def decode_sent(self) -> SignedTxMsg:
        assert self.sent, "nothing was sent"
        decoded = deserialize(bytes.fromhex(self.sent[0]), SignedTxMsg)
        assert isinstance(decoded, SignedTxMsg)
        return decoded

    def post(
        self,
        url: str,
        *,
        json: dict[str, Any],
        timeout: float,
        stream: bool = False,
        headers: dict[str, str] | None = None,
    ) -> CannedResponse:
        self.requests.append(json)
        request_id = json["id"]
        try:
            result = self._answer(json["method"], list(json.get("params", [])))
        except _NodeError as error:
            return CannedResponse(
                {"jsonrpc": "2.0", "id": request_id, "error": {"code": error.code, "message": str(error)}}
            )
        return CannedResponse({"jsonrpc": "2.0", "id": request_id, "result": result})

    def _answer(self, method: str, params: list[Any]) -> Any:
        if method == "getGasConfig":
            self.gas_config_reads += 1
            if self.gas_config_error is not None:
                raise _NodeError(self.gas_config_error)
            return MAINNET_GAS_CONFIG
        if method == "getToken":
            self.lookups += 1
            if not self.reachable:
                raise _NodeError(self.lookup_error)
            # A live node resolves by id without looking at the symbol; that path is the pre-flight's
            # control, and it answers for the gas token whatever the caller's symbol turns out to be.
            if len(params) == 3 and params[2] != 0:
                return {"symbol": "KCAL", "carbonId": "1"}
            token = self.tokens.get(params[0])
            if token is None:
                raise _NodeError(self.lookup_error)
            return token
        if method == "sendCarbonTransaction":
            if self.send_error is not None:
                raise _NodeError(self.send_error)
            self.sent.append(params[0])
            return "HASH"
        if method == "getAccountFungibleTokens":
            self.infusion_reads += 1
            return {"result": self.fungible.get(params[0], []), "cursor": None}
        if method == "getAccountOwnedTokens":
            return {"result": [token for token, _ in self.owned_nfts.get(params[0], [])], "cursor": None}
        if method == "getTokenBalance":
            for token, instances in self.owned_nfts.get(params[0], []):
                if token["symbol"] == params[1]:
                    return {"chain": "main", "symbol": params[1], "amount": instances, "decimals": 0}
            return {"chain": "main", "symbol": params[1], "amount": "0", "decimals": 0}
        raise AssertionError(f"unexpected RPC method {method}")


class _NodeError(Exception):
    def __init__(self, message: str, code: int = -32603) -> None:
        super().__init__(message)
        self.code = code


def transfer_tx(owner: Bytes32, to: Bytes32, gas_payer: Bytes32 | None = None, max_gas: int = 0) -> TxMsg:
    return build_transfer_fungible_tx(
        from_address=owner, to=to, token_id=1, amount=5, gas_payer=gas_payer, limits=TxLimits(max_gas=max_gas)
    )


def create_token_tx(owner: Bytes32, symbol: str) -> TxMsg:
    metadata = build_token_metadata(
        {
            "name": "Send probe",
            "icon": "data:image/png;base64,iVBORw0KGgo=",
            "url": "https://example.invalid/p",
            "description": "x",
        }
    )
    info = build_token_info(symbol, IntX(0), is_nft=False, decimals=2, owner=owner, metadata=metadata)
    return build_create_token_tx(info, owner)


def register_name_tx(owner: Bytes32, name: str) -> TxMsg:
    args = RegisterNameArgs(owner, SmallString(name))
    return TxMsg(
        TxType.CALL,
        1_787_000_000_000,
        0,
        0,
        owner,
        SmallString(""),
        TxMsgCall(ModuleID.GOVERNANCE, GovernanceContractMethod.REGISTER_NAME, serialize(args)),
    )


def burn_tx(owner: Bytes32, token_id: int, instance_id: int) -> TxMsg:
    return build_burn_non_fungible_tx(from_address=owner, token_id=token_id, instance_id=instance_id)
