from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from atera_sync.api import AteraClient


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[AteraClient]:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ATERA_TECHNICIAN_ID", raising=False)
    monkeypatch.delenv("ATERA_TECHNICIAN_EMAIL", raising=False)
    client = AteraClient(api_token="test-token")
    client._client.close()
    yield client
    client._client.close()


@pytest.mark.parametrize(
    "url",
    [
        "https://outside.example/api/v3/alerts",
        "https://app.atera.com/api/v3/alerts",
        "http://api.atera.com/api/v3/alerts",
        "https://api.atera.com.outside.example/api/v3/alerts",
        "https://api.atera.com:8443/api/v3/alerts",
        "https://user:password@api.atera.com/api/v3/alerts",
        "https://api.atera.com@outside.example/api/v3/alerts",
        "//outside.example/api/v3/alerts",
    ],
)
def test_untrusted_url_is_rejected_before_sending(client: AteraClient, url: str) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        pytest.fail("An untrusted request reached the transport")

    client._client = httpx.Client(transport=httpx.MockTransport(handle))

    with pytest.raises(ValueError, match="Atera API requests must use"):
        client.request("GET", url)


@pytest.mark.parametrize("next_link", [
    "https://api.atera.com:443/api/v3/alerts?page=2&itemsInPage=50&alertStatus=Resolved",
    "https://app.atera.com/api/v3/alerts?page=2&itemsInPage=50&alertStatus=Resolved",
    "https://app.atera.com:443/api/v3/alerts?page=2&itemsInPage=50&alertStatus=Resolved",
    "/api/v3/alerts?page=2&itemsInPage=50&alertStatus=Resolved",
    "?page=2&itemsInPage=50&alertStatus=Resolved",
])
def test_pagination_uses_api_origin_and_preserves_query(
    client: AteraClient, next_link: str
) -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(200, json={
                "items": [{"AlertID": 1}],
                "nextLink": next_link,
            })
        return httpx.Response(200, json={"items": [{"AlertID": 2}]})

    client._client = httpx.Client(transport=httpx.MockTransport(handle))

    assert client.list_alerts("Resolved") == [{"AlertID": 1}, {"AlertID": 2}]
    assert len(requests) == 2
    assert requests[0].url.host == "api.atera.com"
    assert requests[1].url.host == "api.atera.com"
    assert requests[1].url.scheme == "https"
    assert requests[1].url.path == "/api/v3/alerts"
    assert requests[1].url.params["page"] == "2"
    assert requests[1].url.params["itemsInPage"] == "50"
    assert requests[1].url.params["alertStatus"] == "Resolved"
    assert all(request.headers["Authorization"] == "Bearer test-token" for request in requests)


@pytest.mark.parametrize("next_link", [
    "https://outside.example/next",
    "http://api.atera.com/api/v3/alerts?page=2",
    "http://app.atera.com/api/v3/alerts?page=2",
    "https://app.atera.com:8443/api/v3/alerts?page=2",
    "https://user:password@app.atera.com/api/v3/alerts?page=2",
    "https://app.atera.com/api/v3/tickets?page=2",
    "https://app.atera.com.outside.example/api/v3/alerts?page=2",
])
def test_untrusted_pagination_link_is_not_followed(client: AteraClient, next_link: str) -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"items": [], "nextLink": next_link})

    client._client = httpx.Client(transport=httpx.MockTransport(handle))

    with pytest.raises(ValueError, match="Atera API requests must use"):
        client.list_alerts("Resolved")

    assert len(requests) == 1
    assert requests[0].url.host == "api.atera.com"


def test_redirect_is_not_followed(client: AteraClient) -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(302, headers={"Location": "https://outside.example/next"})

    client._client = httpx.Client(
        transport=httpx.MockTransport(handle), follow_redirects=True
    )

    response = client.get_alert(1)

    assert response.status_code == 302
    assert len(requests) == 1
    assert requests[0].url.host == "api.atera.com"
