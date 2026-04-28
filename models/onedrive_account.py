import requests
import logging
from datetime import timedelta
from odoo import models, fields
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class OneDriveAccount(models.Model):
    _name = "onedrive.account"
    _description = "Cuenta OneDrive"

    name = fields.Char(required=True)

    client_id = fields.Char(required=True)
    client_secret = fields.Char(required=True)
    tenant_id = fields.Char(required=True)

    redirect_uri = fields.Char(
        default=lambda self: self._default_redirect_uri()
    )

    access_token = fields.Text()
    refresh_token = fields.Text()
    token_expiry = fields.Datetime()

    active = fields.Boolean(default=True)

    # ---------------------------------------
    # CONFIG
    # ---------------------------------------

    def _default_redirect_uri(self):
        base_url = self.env['ir.config_parameter'].sudo().get_param('web.base.url')
        return f"{base_url}/onedrive/callback"

    def _get_authority_url(self):
        return f"https://login.microsoftonline.com/{self.tenant_id}"

    def _get_scope(self):
        return self.env['ir.config_parameter'].sudo().get_param(
            'onedrive.oauth.scope',
            default="offline_access Files.ReadWrite.All User.Read"
        )

    # ---------------------------------------
    # AUTH URL
    # ---------------------------------------

    def get_auth_url(self):
        self.ensure_one()

        return (
            f"{self._get_authority_url()}/oauth2/v2.0/authorize"
            f"?client_id={self.client_id}"
            f"&response_type=code"
            f"&redirect_uri={self.redirect_uri}"
            f"&response_mode=query"
            f"&scope={self._get_scope()}"
        )

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
            raise UserError(f"Error token: {result}")

        self._save_token(result)

    def refresh_access_token(self):
        self.ensure_one()

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
            self.refresh_access_token()

        return self.access_token

    # ---------------------------------------
    # BOTÓN UI
    # ---------------------------------------

    def action_connect_onedrive(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_url",
            "url": f"/onedrive/login/{self.id}",
            "target": "self",
        }