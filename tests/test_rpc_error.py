"""How the node's answers reach the caller: a JSON-RPC error body is the answer whatever the HTTP
status, and a parameterless call still puts an empty parameter list on the wire."""

from __future__ import annotations

from typing import Any

import pytest
from _canned_node import CannedNode, CannedResponse

from phantasma_py import PhantasmaRPC, RPCError


class StatusSession:
    def __init__(self, body: dict[str, Any] | None, status_code: int) -> None:
        self.body = body
        self.status_code = status_code

    def post(
        self,
        url: str,
        *,
        json: dict[str, Any],
        timeout: float,
        stream: bool = False,
        headers: dict[str, str] | None = None,
    ) -> Any:
        if self.body is None:
            return _NotJson(self.status_code)
        return CannedResponse({**self.body, "id": json["id"]}, self.status_code)


class _NotJson:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code

    def json(self) -> Any:
        raise ValueError("upstream down")


# The node answers a lookup it cannot satisfy with an HTTP error status AND a JSON-RPC error body.
# The body is the answer, and it reaches the caller as the typed JSON-RPC error - not as a transport
# error with the reason buried in its text.
def test_a_json_rpc_error_inside_an_http_error_response_is_surfaced_as_the_json_rpc_error() -> None:
    for status in (400, 500, 200):
        body = {"jsonrpc": "2.0", "error": {"code": -32603, "message": "Token symbol not found"}}
        client = PhantasmaRPC("http://node.invalid/rpc", session=StatusSession(body, status))
        with pytest.raises(RPCError) as info:
            client.get_token("NOPE", extended=False)
        assert info.value.code == -32603, status
        assert str(info.value) == "Token symbol not found", status


# Without a JSON-RPC body there is nothing to surface: an HTTP error stays a transport-level error
# naming its status.
def test_an_http_error_without_a_json_rpc_body_stays_a_transport_error() -> None:
    client = PhantasmaRPC("http://node.invalid/rpc", session=StatusSession(None, 502))
    with pytest.raises(RPCError, match="HTTP 502") as info:
        client.get_token("NOPE", extended=False)
    assert info.value.code is None


# The node answers a request without a params field with HTTP 400 "Parse error", so every
# parameterless wrapper must put an empty list on the wire.
def test_get_gas_config_sends_an_empty_parameter_list() -> None:
    node = CannedNode()
    assert node.client().get_gas_config().gas_model_version == 2
    assert node.requests[0]["method"] == "getGasConfig"
    assert node.requests[0]["params"] == []
