from __future__ import annotations

import os
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional

import httpx
from pydantic import BaseModel, ConfigDict
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

BASE_URL = "https://api.atera.com/api/v3"
TIMEOUT_SECONDS = 30
ITEMS_IN_PAGE = 50


class TicketModel(BaseModel):
    model_config = ConfigDict(extra="allow")


class AlertModel(BaseModel):
    model_config = ConfigDict(extra="allow")


class ApiError(RuntimeError):
    def __init__(self, message: str, status_code: Optional[int] = None, response_text: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.response_text = response_text


@dataclass
class ApiResponse:
    status_code: int
    json_data: Any
    text: str


def _validated_api_url(path_or_url: str) -> httpx.URL:
    try:
        url = httpx.URL(path_or_url)
        if not url.scheme and not url.host:
            url = httpx.URL(f"{BASE_URL}/{path_or_url.lstrip('/')}")
    except httpx.InvalidURL:
        raise ValueError("Invalid Atera API request URL") from None

    base_url = httpx.URL(BASE_URL)
    if (
        (url.scheme, url.host, url.port)
        != (base_url.scheme, base_url.host, base_url.port)
        or url.userinfo
    ):
        raise ValueError(
            "Atera API requests must use https://api.atera.com on port 443 "
            "without embedded credentials"
        )
    return url


def _pagination_url(next_link: str, current_url: str) -> str:
    current = _validated_api_url(current_url)
    try:
        url = current.join(next_link)
    except httpx.InvalidURL:
        raise ValueError("Invalid Atera pagination URL") from None

    # Atera's API can return pagination links using its legacy app hostname.
    # Rewrite only that HTTPS origin and the same endpoint; never send it a token.
    if (
        (url.scheme, url.host, url.port) == ("https", "app.atera.com", None)
        and not url.userinfo
        and url.path == current.path
    ):
        url = url.copy_with(host=current.host)
    return str(_validated_api_url(str(url)))


class AteraClient:
    def __init__(self, api_token: Optional[str] = None) -> None:
        self.api_token = (api_token or os.getenv("ATERA_API_TOKEN", "")).strip()
        if not self.api_token:
            self.api_token = _load_env_value_from_env_file("ATERA_API_TOKEN")
        if not self.api_token:
            raise ApiError("Missing ATERA_API_TOKEN in environment")
        self.technician_id = _load_optional_int_env("ATERA_TECHNICIAN_ID")
        self.technician_email = _load_optional_str_env("ATERA_TECHNICIAN_EMAIL")

        self._client = httpx.Client(timeout=TIMEOUT_SECONDS, headers=self._base_headers())

    def _base_headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_token}",
            "accept": "application/json",
        }

    def _headers_for_update(self) -> Dict[str, str]:
        headers = self._base_headers()
        headers["content-type"] = "application/json"
        return headers

    @retry(
        retry=retry_if_exception_type(ApiError),
        wait=wait_exponential(multiplier=1, min=1, max=16),
        stop=stop_after_attempt(5),
        reraise=True,
    )
    def request(
        self,
        method: str,
        path_or_url: str,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        use_update_headers: bool = False,
    ) -> ApiResponse:
        url = _validated_api_url(path_or_url)
        headers = self._headers_for_update() if use_update_headers else self._base_headers()

        response = self._client.request(
            method, url, params=params, json=json_body, headers=headers, follow_redirects=False
        )

        if response.status_code == 429 or 500 <= response.status_code <= 599:
            raise ApiError("Transient API failure", response.status_code, response.text)

        try:
            json_data = response.json()
        except ValueError:
            json_data = None

        return ApiResponse(status_code=response.status_code, json_data=json_data, text=response.text)

    def get_ticket(self, ticket_id: int) -> ApiResponse:
        return self.request("GET", f"/tickets/{ticket_id}")

    def get_alert(self, alert_id: int) -> ApiResponse:
        return self.request("GET", f"/alerts/{alert_id}")

    def list_tickets(self, status: str) -> List[Dict[str, Any]]:
        params = {"ticketStatus": status, "itemsInPage": ITEMS_IN_PAGE}
        return self._paginate_items(f"{BASE_URL}/tickets", params)

    def list_alerts(self, status: Optional[str] = None, max_items: Optional[int] = None) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {"itemsInPage": ITEMS_IN_PAGE}
        if status:
            params["alertStatus"] = status
        try:
            return self._paginate_items(f"{BASE_URL}/alerts", params, max_items=max_items)
        except ApiError as exc:
            if status and exc.status_code == 400:
                params.pop("alertStatus", None)
                return self._paginate_items(f"{BASE_URL}/alerts", params, max_items=max_items)
            raise

    def update_ticket_status(self, ticket_id: int, new_status: str) -> ApiResponse:
        body = {"TicketStatus": new_status}
        methods = ["POST", "PUT", "PATCH"]

        last_response: Optional[ApiResponse] = None
        for method in methods:
            response = self.request(
                method,
                f"/tickets/{ticket_id}",
                json_body=body,
                use_update_headers=True,
            )
            last_response = response

            if response.status_code not in (404, 405):
                import logging

                logging.getLogger("atera_sync").info(
                    "event=ticket_update_method ticketId=%s method=%s statusCode=%s",
                    ticket_id,
                    method,
                    response.status_code,
                )
                return response

        return last_response or ApiResponse(status_code=0, json_data=None, text="")

    def add_ticket_comment(self, ticket_id: int, comment_text: str) -> ApiResponse:
        if not self.technician_id:
            raise ApiError("Missing ATERA_TECHNICIAN_ID in environment for ticket comments")

        body: Dict[str, Any] = {
            "CommentText": comment_text,
            "TechnicianCommentDetails": {
                "TechnicianId": self.technician_id,
                "IsInternal": True,
            },
        }
        if self.technician_email:
            body["TechnicianCommentDetails"]["TechnicianEmail"] = self.technician_email

        return self.request(
            "POST",
            f"/tickets/{ticket_id}/comments",
            json_body=body,
            use_update_headers=True,
        )

    def _paginate_items(
        self,
        initial_url: str,
        params: Dict[str, Any],
        max_items: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        next_url: Optional[str] = initial_url
        next_params: Optional[Dict[str, Any]] = params

        while next_url:
            response = self.request("GET", next_url, params=next_params)
            if response.status_code != 200:
                raise ApiError(
                    f"Failed to list items from {next_url}",
                    response.status_code,
                    response.text,
                )

            payload = response.json_data or {}
            page_items = payload.get("items") or []
            if isinstance(page_items, list):
                items.extend(page_items)
                if max_items is not None and len(items) >= max_items:
                    return items[:max_items]

            next_link = payload.get("nextLink")
            if not next_link:
                next_url = None
                next_params = None
            else:
                next_url = _pagination_url(next_link, next_url)
                next_params = None

        return items


def _load_env_value_from_env_file(name: str) -> str:
    env_path = Path(".env")
    if not env_path.exists():
        return ""

    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() == name:
            return value.strip().strip("'").strip('"')
    return ""


def _load_optional_int_env(name: str) -> Optional[int]:
    raw = os.getenv(name, "").strip()
    if not raw:
        raw = _load_env_value_from_env_file(name)
    if not raw:
        return None
    if raw.isdigit():
        return int(raw)
    raise ApiError(f"Invalid {name}; expected integer")


def _load_optional_str_env(name: str) -> Optional[str]:
    raw = os.getenv(name, "").strip()
    if not raw:
        raw = _load_env_value_from_env_file(name)
    return raw or None
