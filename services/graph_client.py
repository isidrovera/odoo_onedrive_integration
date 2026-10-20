# -*- coding: utf-8 -*-
"""Cliente HTTP compartido para Microsoft Graph y SharePoint REST (Odoo 19)."""
import logging
import random
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import requests

from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class GraphClient:
    BASE_URL = "https://graph.microsoft.com/v1.0"
    TIMEOUT = 60
    MAX_ATTEMPTS = 4
    MAX_RETRY_SECONDS = 30
    MAX_PAGES = 100
    RETRIABLE_CODES = {429, 500, 502, 503, 504}
    IDEMPOTENT_METHODS = {"GET", "HEAD", "OPTIONS", "PUT", "DELETE"}

    def __init__(self, token=None, token_provider=None, env=None, base_url=None):
        self.token = token
        self.token_provider = token_provider
        self.env = env
        if base_url is None and env is not None:
            base_url = env["ir.config_parameter"].sudo().get_param(
                "microsoft365.graph.base_url", self.BASE_URL
            )
        self.base_url = (base_url or self.BASE_URL).rstrip("/")
        parsed = urlsplit(self.base_url)
        if (
            parsed.scheme.lower() != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise UserError("La URL base de Microsoft debe ser HTTPS y válida.")
        if parsed.port not in (None, 443):
            raise UserError("La URL base de Microsoft no permite puertos personalizados.")
        host = parsed.hostname.lower()
        # Graph público o SharePoint del tenant para su API REST.
        if host != "graph.microsoft.com" and not host.endswith(".sharepoint.com"):
            raise UserError("El servidor configurado no pertenece a Microsoft Graph o SharePoint.")
        self._origin = (parsed.scheme.lower(), host, parsed.port or 443)
        self._base_path = parsed.path.rstrip("/")

    def _get_token(self):
        token = self.token_provider() if self.token_provider else self.token
        if not token or not isinstance(token, str):
            raise UserError("No existe un token Microsoft válido.")
        return token

    def _url(self, endpoint, absolute=False):
        if not isinstance(endpoint, str) or not endpoint:
            raise UserError("La ruta Microsoft no es válida.")
        if absolute:
            url = endpoint
        else:
            if not endpoint.startswith("/") or endpoint.startswith("//"):
                raise UserError("La ruta Microsoft debe comenzar por una sola barra.")
            url = f"{self.base_url}{endpoint}"
        parsed = urlsplit(url)
        if (
            parsed.scheme.lower() != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or (parsed.scheme.lower(), parsed.hostname.lower(), parsed.port or 443) != self._origin
        ):
            raise UserError("Microsoft devolvió una dirección externa no autorizada.")
        # Impide reutilizar un token de Graph contra rutas fuera de la API base.
        if self._base_path and not (
            parsed.path == self._base_path or parsed.path.startswith(self._base_path + "/")
        ):
            raise UserError("La dirección no pertenece a la API Microsoft configurada.")
        return url

    @classmethod
    def _delay(cls, response, attempt):
        value = response.headers.get("Retry-After") if response is not None else None
        if value:
            try:
                wait = float(value)
            except (ValueError, TypeError):
                try:
                    parsed = parsedate_to_datetime(value)
                    wait = (parsed - datetime.now(timezone.utc)).total_seconds()
                except (TypeError, ValueError, OverflowError):
                    wait = None
            if wait is not None:
                return max(0.0, min(float(wait), cls.MAX_RETRY_SECONDS))
        return min((2 ** attempt) + random.uniform(0, 0.5), cls.MAX_RETRY_SECONDS)

    @staticmethod
    def _error_message(response):
        try:
            payload = response.json()
            if isinstance(payload, dict):
                error = payload.get("error")
                if isinstance(error, dict):
                    return str(error.get("message") or error.get("code") or "Solicitud rechazada")[:600]
                return str(payload.get("message") or "Solicitud rechazada")[:600]
        except (ValueError, TypeError):
            pass
        return "Solicitud rechazada por Microsoft"

    def request(self, method, endpoint, *, absolute=False, expected=None, timeout=None, **kwargs):
        method = str(method).upper()
        if method not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}:
            raise UserError("Método HTTP no permitido.")
        url = self._url(endpoint, absolute=absolute)
        headers = dict(kwargs.pop("headers", {}) or {})
        # No permitir que un llamador sustituya la identidad de la aplicación.
        for key in list(headers):
            if key.lower() in {"authorization", "host", "cookie"}:
                headers.pop(key)
        headers["Authorization"] = f"Bearer {self._get_token()}"
        headers.setdefault("Accept", "application/json")
        if "json" in kwargs:
            headers.setdefault("Content-Type", "application/json")
        kwargs.pop("allow_redirects", None)
        allowed = set(expected or ())
        response = None
        for attempt in range(self.MAX_ATTEMPTS):
            try:
                response = requests.request(
                    method, url, headers=headers, timeout=timeout or self.TIMEOUT,
                    allow_redirects=False, **kwargs
                )
            except (requests.Timeout, requests.ConnectionError) as error:
                if attempt + 1 == self.MAX_ATTEMPTS or method not in self.IDEMPOTENT_METHODS:
                    raise UserError("No se pudo completar la conexión con Microsoft.") from error
                time.sleep(min(2 ** attempt, self.MAX_RETRY_SECONDS))
                continue
            except requests.RequestException as error:
                raise UserError("Error de comunicación con Microsoft.") from error

            if response.status_code in self.RETRIABLE_CODES and attempt + 1 < self.MAX_ATTEMPTS:
                # POST/PATCH pueden producir duplicados si la operación ya se ejecutó.
                if response.status_code == 429 or method in self.IDEMPOTENT_METHODS:
                    delay = self._delay(response, attempt)
                    response.close()
                    time.sleep(delay)
                    continue
            break

        if response is None:
            raise UserError("Microsoft no devolvió ninguna respuesta.")
        request_id = response.headers.get("request-id") or response.headers.get("client-request-id")
        if 300 <= response.status_code < 400:
            response.close()
            raise UserError("Microsoft solicitó una redirección no autorizada.")
        if response.status_code >= 400 and response.status_code not in allowed:
            message = self._error_message(response)
            _logger.warning(
                "Microsoft API fallo method=%s endpoint=%s status=%s request_id=%s",
                method, urlsplit(url).path, response.status_code, request_id
            )
            response.close()
            raise UserError(
                "Microsoft rechazó la operación (%s): %s%s" % (
                    response.status_code, message,
                    f" [Request ID: {request_id}]" if request_id else ""
                )
            )
        try:
            if response.status_code in allowed and response.status_code >= 400:
                return {}
            if response.status_code in (204, 205) or not response.content:
                return {}
            try:
                return response.json()
            except ValueError:
                return {
                    "raw": response.content,
                    "content_type": response.headers.get("Content-Type", "application/octet-stream"),
                }
        finally:
            response.close()

    def fetch_all(self, endpoint, *, params=None, max_pages=100):
        max_pages = min(max(int(max_pages), 1), self.MAX_PAGES)
        values = []
        next_endpoint = endpoint
        next_params = params
        seen = set()
        pages = 0
        while next_endpoint and pages < max_pages:
            url = self._url(next_endpoint, absolute=next_endpoint.startswith("https://"))
            if url in seen:
                raise UserError("Microsoft devolvió una paginación circular.")
            seen.add(url)
            data = self.request(
                "GET", next_endpoint, absolute=next_endpoint.startswith("https://"),
                params=next_params
            )
            if not isinstance(data, dict) or not isinstance(data.get("value", []), list):
                raise UserError("Microsoft devolvió una página de datos inválida.")
            values.extend(data.get("value") or [])
            next_endpoint = data.get("@odata.nextLink")
            if next_endpoint and not isinstance(next_endpoint, str):
                raise UserError("Microsoft devolvió un enlace de paginación inválido.")
            next_params = None
            pages += 1
        if next_endpoint:
            _logger.warning("Paginación Microsoft incompleta: máximo de %s páginas", max_pages)
            raise UserError("La consulta superó el límite de páginas; los resultados estarían incompletos.")
        return values
