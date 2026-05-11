# -*- coding: utf-8 -*-

import requests
import logging

from datetime import timedelta
from urllib.parse import urlencode

from odoo import models, fields, api
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class OneDriveAccount(models.Model):
    _name = "onedrive.account"
    _description = "Cuenta OneDrive"

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
    )

    access_token = fields.Text(
        string="Access Token",
        groups="base.group_system",
    )

    refresh_token = fields.Text(
        string="Refresh Token",
        groups="base.group_system",
    )

    token_expiry = fields.Datetime(
        string="Expira el token",
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

            elif record.token_expiry and now >= record.token_expiry:
                record.is_connected = False
                record.token_status = "expired"

            else:
                record.is_connected = True
                record.token_status = "connected"

    # ---------------------------------------
    # CONFIG
    # ---------------------------------------

    def _default_redirect_uri(self):
        base_url = self.env["ir.config_parameter"].sudo().get_param("web.base.url")
        return f"{base_url}/onedrive/callback"

    def _get_authority_url(self):
        self.ensure_one()

        if not self.tenant_id:
            raise UserError("Debe ingresar Tenant ID")

        return f"https://login.microsoftonline.com/{self.tenant_id}"

    def _get_scope(self):
        return self.env["ir.config_parameter"].sudo().get_param(
            "onedrive.oauth.scope",
            default="offline_access Files.ReadWrite.All User.Read",
        )

    # ---------------------------------------
    # AUTH URL
    # ---------------------------------------

    def get_auth_url(self):
        self.ensure_one()

        base_url = f"{self._get_authority_url()}/oauth2/v2.0/authorize"

        params = {
            "client_id": self.client_id,
            "response_type": "code",
            "redirect_uri": self.redirect_uri,
            "response_mode": "query",
            "scope": self._get_scope(),
        }

        return f"{base_url}?{urlencode(params)}"

    # ---------------------------------------
    # TOKEN
    # ---------------------------------------

    def exchange_code_for_token(self, code):
        self.ensure_one()

        token_url = f"{self._get_authority_url()}/oauth2/v2.0/token"

        data = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "code": code,
            "redirect_uri": self.redirect_uri,
            "grant_type": "authorization_code",
        }

        res = requests.post(token_url, data=data)
        result = res.json()

        if "access_token" not in result:
            _logger.error("Token error: %s", result)
            raise UserError(f"Error token: {result}")

        self._save_token(result)

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
        }

        res = requests.post(token_url, data=data)
        result = res.json()

        if "access_token" not in result:
            _logger.error("Refresh error: %s", result)
            raise UserError(f"Error refresh: {result}")

        self._save_token(result)

    def _save_token(self, data):
        expires_in = data.get("expires_in", 3600)

        self.write({
            "access_token": data.get("access_token"),
            "refresh_token": data.get("refresh_token", self.refresh_token),
            "token_expiry": fields.Datetime.now() + timedelta(seconds=expires_in - 60),
        })

    def get_valid_token(self):
        self.ensure_one()

        if not self.access_token or not self.token_expiry:
            raise UserError("Cuenta no autenticada")

        if fields.Datetime.now() >= self.token_expiry:
            _logger.info("Token expirado, refrescando...")
            self.refresh_access_token()

        return self.access_token

    # ---------------------------------------
    # BOTONES UI
    # ---------------------------------------

    def action_connect_onedrive(self):
        self.ensure_one()

        url = self.get_auth_url()

        _logger.info("OAuth URL: %s", url)

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
            },
        }

    def action_clear_tokens(self):
        self.ensure_one()

        self.write({
            "access_token": False,
            "refresh_token": False,
            "token_expiry": False,
        })

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "OneDrive",
                "message": "Tokens eliminados correctamente.",
                "type": "warning",
                "sticky": False,
            },
        }