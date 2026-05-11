# -*- coding: utf-8 -*-

import requests
import logging
import secrets

from datetime import timedelta
from urllib.parse import urlencode

from odoo import models, fields, api
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class OneDriveAccount(models.Model):
    _name = "onedrive.account"
    _description = "Cuenta OneDrive"
    _rec_name = "name"

    name = fields.Char(
        string="Nombre",
        required=True,
    )

    client_id = fields.Char(
        string="Client ID",
        required=True,
    )

    client_secret = fields.Char(
        string="Client Secret",
        required=True,
        groups="base.group_system",
    )

    tenant_id = fields.Char(
        string="Tenant ID",
        required=True,
    )

    redirect_uri = fields.Char(
        string="Redirect URI",
        default=lambda self: self._default_redirect_uri(),
        readonly=True,
    )

    access_token = fields.Text(
        string="Access Token",
        groups="base.group_system",
        copy=False,
    )

    refresh_token = fields.Text(
        string="Refresh Token",
        groups="base.group_system",
        copy=False,
    )

    token_expiry = fields.Datetime(
        string="Expira el token",
        copy=False,
    )

    oauth_state = fields.Char(
        string="OAuth State",
        copy=False,
        groups="base.group_system",
    )

    active = fields.Boolean(
        string="Activo",
        default=True,
    )

    is_connected = fields.Boolean(
        string="Conectado",
        compute="_compute_connection_status",
        store=False,
    )

    token_status = fields.Selection(
        selection=[
            ("not_connected", "Sin conexión"),
            ("connected", "Conectado"),
            ("expired", "Token vencido"),
        ],
        string="Estado del token",
        compute="_compute_connection_status",
        store=False,
    )

    # ---------------------------------------
    # COMPUTE
    # ---------------------------------------

    @api.depends("access_token", "refresh_token", "token_expiry")
    def _compute_connection_status(self):
        now = fields.Datetime.now()

        for record in self:
            if not record.access_token and not record.refresh_token:
                record.is_connected = False
                record.token_status = "not_connected"
                continue

            if record.token_expiry and now >= record.token_expiry:
                record.is_connected = False
                record.token_status = "expired"
                continue

            record.is_connected = True
            record.token_status = "connected"

    # ---------------------------------------
    # CONFIG
    # ---------------------------------------

    def _default_redirect_uri(self):
        base_url = self.env["ir.config_parameter"].sudo().get_param("web.base.url")

        if not base_url:
            raise UserError(
                "No está configurado el parámetro web.base.url en Odoo."
            )

        return f"{base_url}/onedrive/callback"

    def _get_authority_url(self):
        self.ensure_one()

        if not self.tenant_id:
            raise UserError("Debe ingresar Tenant ID.")

        return f"https://login.microsoftonline.com/{self.tenant_id}"

    def _get_scope(self):
        return self.env["ir.config_parameter"].sudo().get_param(
            "onedrive.oauth.scope",
            default="offline_access Files.ReadWrite.All User.Read",
        )

    def _prepare_oauth_state(self):
        self.ensure_one()

        state = secrets.token_urlsafe(32)

        self.sudo().write({
            "oauth_state": state,
        })

        return state

    # ---------------------------------------
    # AUTH URL
    # ---------------------------------------

    def get_auth_url(self):
        self.ensure_one()

        if not self.client_id:
            raise UserError("Debe ingresar Client ID.")

        if not self.client_secret:
            raise UserError("Debe ingresar Client Secret.")

        if not self.tenant_id:
            raise UserError("Debe ingresar Tenant ID.")

        if not self.redirect_uri:
            raise UserError("Debe configurar Redirect URI.")

        base_url = f"{self._get_authority_url()}/oauth2/v2.0/authorize"

        state = self._prepare_oauth_state()

        params = {
            "client_id": self.client_id,
            "response_type": "code",
            "redirect_uri": self.redirect_uri,
            "response_mode": "query",
            "scope": self._get_scope(),
            "state": state,
        }

        return f"{base_url}?{urlencode(params)}"

    # ---------------------------------------
    # TOKEN
    # ---------------------------------------

    def exchange_code_for_token(self, code, state=None):
        self.ensure_one()

        if not code:
            raise UserError("No se recibió el código de autorización de Microsoft.")

        if state and self.oauth_state and state != self.oauth_state:
            raise UserError(
                "El estado OAuth no coincide. Por seguridad, vuelva a conectar la cuenta."
            )

        token_url = f"{self._get_authority_url()}/oauth2/v2.0/token"

        data = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "code": code,
            "redirect_uri": self.redirect_uri,
            "grant_type": "authorization_code",
        }

        result = self._post_token_request(token_url, data, operation="token")

        if "access_token" not in result:
            _logger.error("Token error Microsoft: %s", self._safe_token_error(result))
            raise UserError(f"Error obteniendo token de Microsoft: {result}")

        self._save_token(result)

        self.sudo().write({
            "oauth_state": False,
        })

        return True

    def refresh_access_token(self):
        self.ensure_one()

        if not self.refresh_token:
            raise UserError(
                "No existe refresh token. Debe conectar nuevamente la cuenta con Microsoft."
            )

        token_url = f"{self._get_authority_url()}/oauth2/v2.0/token"

        data = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": self.refresh_token,
            "grant_type": "refresh_token",
            "scope": self._get_scope(),
        }

        result = self._post_token_request(token_url, data, operation="refresh")

        if "access_token" not in result:
            _logger.error("Refresh error Microsoft: %s", self._safe_token_error(result))
            raise UserError(f"Error renovando token de Microsoft: {result}")

        self._save_token(result)

        return True

    def _post_token_request(self, token_url, data, operation="token"):
        try:
            response = requests.post(
                token_url,
                data=data,
                timeout=30,
            )
        except requests.exceptions.Timeout:
            raise UserError(
                "Microsoft no respondió a tiempo. Intente nuevamente."
            )
        except requests.exceptions.RequestException as error:
            _logger.exception("Error HTTP OAuth Microsoft [%s]", operation)
            raise UserError(
                f"No se pudo conectar con Microsoft: {error}"
            )

        try:
            result = response.json()
        except ValueError:
            _logger.error(
                "Respuesta no JSON de Microsoft [%s]: status=%s text=%s",
                operation,
                response.status_code,
                response.text[:500],
            )
            raise UserError(
                "Microsoft devolvió una respuesta inválida al solicitar el token."
            )

        if response.status_code >= 400:
            _logger.error(
                "Error OAuth Microsoft [%s]: status=%s result=%s",
                operation,
                response.status_code,
                self._safe_token_error(result),
            )
            raise UserError(
                f"Microsoft rechazó la solicitud OAuth: {result}"
            )

        return result

    def _safe_token_error(self, data):
        if not isinstance(data, dict):
            return data

        hidden_keys = {
            "access_token",
            "refresh_token",
            "id_token",
            "client_secret",
        }

        clean_data = {}

        for key, value in data.items():
            if key in hidden_keys:
                clean_data[key] = "***OCULTO***"
            else:
                clean_data[key] = value

        return clean_data

    def _save_token(self, data):
        self.ensure_one()

        expires_in = data.get("expires_in", 3600)

        try:
            expires_in = int(expires_in)
        except Exception:
            expires_in = 3600

        self.sudo().write({
            "access_token": data.get("access_token"),
            "refresh_token": data.get("refresh_token") or self.refresh_token,
            "token_expiry": fields.Datetime.now() + timedelta(seconds=max(expires_in - 60, 60)),
        })

        return True

    def get_valid_token(self):
        self.ensure_one()

        if not self.access_token or not self.token_expiry:
            raise UserError(
                "Cuenta OneDrive no autenticada. Conecte la cuenta con Microsoft."
            )

        if fields.Datetime.now() >= self.token_expiry:
            _logger.info(
                "Token OneDrive expirado para cuenta ID %s. Renovando...",
                self.id,
            )
            self.refresh_access_token()

        return self.sudo().access_token

    # ---------------------------------------
    # BOTONES UI
    # ---------------------------------------

    def action_connect_onedrive(self):
        self.ensure_one()

        url = self.get_auth_url()

        _logger.info(
            "Generando URL OAuth OneDrive para cuenta ID %s",
            self.id,
        )

        return {
            "type": "ir.actions.act_url",
            "url": url,
            "target": "self",
        }

    def action_refresh_token(self):
        self.ensure_one()

        self.refresh_access_token()

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "OneDrive",
                "message": "Token renovado correctamente.",
                "type": "success",
                "sticky": False,
                "next": {
                    "type": "ir.actions.act_window",
                    "res_model": "onedrive.account",
                    "res_id": self.id,
                    "view_mode": "form",
                    "target": "current",
                },
            },
        }

    def action_clear_tokens(self):
        self.ensure_one()

        self.sudo().write({
            "access_token": False,
            "refresh_token": False,
            "token_expiry": False,
            "oauth_state": False,
        })

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "OneDrive",
                "message": "Tokens eliminados correctamente.",
                "type": "warning",
                "sticky": False,
                "next": {
                    "type": "ir.actions.act_window",
                    "res_model": "onedrive.account",
                    "res_id": self.id,
                    "view_mode": "form",
                    "target": "current",
                },
            },
        }