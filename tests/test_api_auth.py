from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from atera_sync.api import AteraClient, ApiError


def test_token_is_sent_as_bearer_for_reads_and_writes() -> None:
    client = AteraClient(api_token="  test-token  ")
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={})

    client._client.close()
    client._client = httpx.Client(transport=httpx.MockTransport(handle))

    client.get_alert(123)
    client.update_ticket_status(456, "Resolved")

    assert [request.method for request in requests] == ["GET", "POST"]
    assert [str(request.url) for request in requests] == [
        "https://api.atera.com/api/v3/alerts/123",
        "https://api.atera.com/api/v3/tickets/456",
    ]
    for request in requests:
        assert request.headers["Authorization"] == "Bearer test-token"
        assert "X-API-KEY" not in request.headers

    client._client.close()


def test_legacy_key_is_not_accepted_as_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ATERA_API_TOKEN", raising=False)
    monkeypatch.setenv("ATERA_API_KEY", "legacy-key")

    with pytest.raises(ApiError, match="Missing ATERA_API_TOKEN"):
        AteraClient()


def test_token_is_loaded_from_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ATERA_API_TOKEN", "new-token")

    client = AteraClient()

    assert client._base_headers()["Authorization"] == "Bearer new-token"
    client._client.close()
