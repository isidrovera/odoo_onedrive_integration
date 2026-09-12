# -*- coding: utf-8 -*-
import logging
import time

import requests

from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class GraphClient:
    BASE_URL = "https://graph.microsoft.com/v1.0"
    TIMEOUT = 60

    def __init__(self, token=None, token_provider=None, env=None, base_url=None):
        self.token = token
        self.token_provider = token_provider
        self.env = env
        if not base_url and env:
            base_url = env["ir.config_parameter"].sudo().get_param(
                "microsoft365.graph.base_url", self.BASE_URL
            )
        self.base_url = (base_url or self.BASE_URL).rstrip("/")

    def _get_token(self):
        token = self.token_provider() if self.token_provider else self.token
        if not token:
            raise UserError("No existe un token Microsoft válido.")
        return token

    def request(self, method, endpoint, *, absolute=False, expected=None, timeout=None, **kwargs):
        url = endpoint if absolute else f"{self.base_url}{endpoint}"
        headers = dict(kwargs.pop("headers", {}) or {})
        headers.setdefault("Authorization", f"Bearer {self._get_token()}")
        headers.setdefault("Accept", "application/json")
        if "json" in kwargs:
            headers.setdefault("Content-Type", "application/json")

        response = None
        for attempt in range(3):
            try:
                response = requests.request(method, url, headers=headers, timeout=timeout or self.TIMEOUT, **kwargs)
            except requests.Timeout as error:
                if attempt == 2:
                    raise UserError("Microsoft no respondió a tiempo.") from error
                continue
            except requests.RequestException as error:
                raise UserError(f"No se pudo conectar con Microsoft: {error}") from error

            if response.status_code == 429 or response.status_code in (502, 503, 504):
                if attempt == 2:
                    break
                retry_after = response.headers.get("Retry-After")
                try:
                    wait = min(max(int(retry_after or 1), 1), 8)
                except ValueError:
                    wait = 1
                time.sleep(wait)
                continue
            break

        allowed = set(expected or ())
        if response.status_code >= 400 and response.status_code not in allowed:
            try:
                payload = response.json()
                error = payload.get("error", {})
                message = error.get("message") or str(payload)
            except ValueError:
                message = response.text[:1000]
            request_id = response.headers.get("request-id") or response.headers.get("client-request-id")
            _logger.warning("Microsoft API %s %s -> %s request_id=%s", method, url, response.status_code, request_id)
            raise UserError(f"Microsoft rechazó la operación ({response.status_code}): {message}")

        if response.status_code == 204 or not response.content:
            return {}
        try:
            return response.json()
        except ValueError:
            return {"raw": response.content, "content_type": response.headers.get("Content-Type")}

    def fetch_all(self, endpoint, *, params=None, max_pages=100):
        values = []
        next_endpoint = endpoint
        next_params = params
        pages = 0
        while next_endpoint and pages < max_pages:
            pages += 1
            data = self.request("GET", next_endpoint, absolute=next_endpoint.startswith("http"), params=next_params)
            values.extend(data.get("value") or [])
            next_endpoint = data.get("@odata.nextLink")
            next_params = None
        if next_endpoint:
            _logger.warning("Paginación Microsoft detenida después de %s páginas", max_pages)
        return values
