# -*- coding: utf-8 -*-
import secrets
from datetime import timedelta
from urllib.parse import urlencode

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError


class MicrosoftUserConnection(models.Model):
    _name = "microsoft.user.connection"
    _description = "Conexión personal Microsoft"
    _order = "user_id, tenant_id"

    user_id = fields.Many2one("res.users", required=True, default=lambda self: self.env.user, ondelete="cascade", index=True)
    tenant_id = fields.Many2one("onedrive.account", required=True, ondelete="cascade", index=True)
    email = fields.Char(required=True)
    entra_user_id = fields.Char(string="ID Microsoft", copy=False, index=True)
    access_token = fields.Char(copy=False, groups="odoo_onedrive_integration.group_microsoft_admin")
    refresh_token = fields.Char(copy=False, groups="odoo_onedrive_integration.group_microsoft_admin")
    token_expiry = fields.Datetime(copy=False, groups="odoo_onedrive_integration.group_microsoft_admin")
    oauth_state = fields.Char(copy=False, groups="odoo_onedrive_integration.group_microsoft_admin")
    oauth_state_expiry = fields.Datetime(copy=False, groups="odoo_onedrive_integration.group_microsoft_admin")
    state = fields.Selection(
        [("not_connected", "Sin conectar"), ("connected", "Conectado"), ("expired", "Requiere conexión"), ("error", "Error")],
        default="not_connected", required=True, readonly=True,
    )
    last_error = fields.Text(readonly=True)
    last_connected = fields.Datetime(readonly=True)
    active = fields.Boolean(default=True)

    _sql_constraints = [
        ("microsoft_user_tenant_unique", "unique(user_id, tenant_id)", "El usuario ya tiene una conexión para este tenant."),
    ]

    def _check_owner_or_admin(self):
        if self.user_id != self.env.user and not self.env.user.has_group("odoo_onedrive_integration.group_microsoft_admin"):
            raise AccessError("No puede administrar la conexión Microsoft de otro usuario.")

    def _scope(self):
        return self.env["ir.config_parameter"].sudo().get_param(
            "microsoft365.oauth.delegated_scope",
            "openid profile email offline_access User.Read Files.ReadWrite.All Sites.ReadWrite.All",
        )

    def get_auth_url(self):
        self.ensure_one()
        self._check_owner_or_admin()
        state = "u." + secrets.token_urlsafe(40)
        self.sudo().write({"oauth_state": state, "oauth_state_expiry": fields.Datetime.now() + timedelta(minutes=10)})
        params = {
            "client_id": self.tenant_id.client_id,
            "response_type": "code",
            "redirect_uri": self.tenant_id.redirect_uri,
            "response_mode": "query",
            "scope": self._scope(),
            "state": state,
            "login_hint": self.email,
        }
        return f"{self.tenant_id._authority()}/oauth2/v2.0/authorize?{urlencode(params)}"

    def exchange_code_for_token(self, code, state):
        self.ensure_one()
        if not state or state != self.oauth_state or not code:
            raise UserError("La respuesta OAuth no corresponde a esta conexión personal.")
        if not self.oauth_state_expiry or fields.Datetime.now() > self.oauth_state_expiry:
            raise UserError("La conexión venció. Vuelva a iniciarla desde Odoo.")
        result = self.tenant_id._token_request({
            "client_id": self.tenant_id.client_id,
            "client_secret": self.tenant_id.client_secret,
            "code": code,
            "redirect_uri": self.tenant_id.redirect_uri,
            "grant_type": "authorization_code",
            "scope": self._scope(),
        })
        self._save_token(result)
        from ..services.graph_client import GraphClient
        profile = GraphClient(token=self.access_token, env=self.env).request("GET", "/me?$select=id,displayName,mail,userPrincipalName")
        self.sudo().write({
            "entra_user_id": profile.get("id"),
            # Para invitados, el UPN suele ser una dirección #EXT#. Conservamos el
            # correo real de Odoo cuando Microsoft no publica el atributo mail.
            "email": profile.get("mail") or self.email,
            "last_connected": fields.Datetime.now(),
        })
        return True

    def _save_token(self, result):
        expires = int(result.get("expires_in", 3600))
        self.sudo().write({
            "access_token": result.get("access_token"),
            "refresh_token": result.get("refresh_token") or self.refresh_token,
            "token_expiry": fields.Datetime.now() + timedelta(seconds=max(expires - 90, 60)),
            "oauth_state": False,
            "oauth_state_expiry": False,
            "state": "connected",
            "last_error": False,
        })

    def refresh_access_token(self):
        self.ensure_one()
        if not self.refresh_token:
            raise UserError("Debe conectar nuevamente su cuenta Microsoft.")
        try:
            result = self.tenant_id._token_request({
                "client_id": self.tenant_id.client_id,
                "client_secret": self.tenant_id.client_secret,
                "refresh_token": self.refresh_token,
                "grant_type": "refresh_token",
                "scope": self._scope(),
            })
            self._save_token(result)
        except Exception as error:
            self.sudo().write({"state": "error", "last_error": str(error)})
            raise
        return True

    def get_valid_token(self):
        self.ensure_one()
        self._check_owner_or_admin()
        if self.refresh_token and (not self.access_token or not self.token_expiry or fields.Datetime.now() >= self.token_expiry):
            self.refresh_access_token()
        if not self.access_token:
            raise UserError("Conecte su cuenta Microsoft para continuar.")
        return self.sudo().access_token

    def action_connect(self):
        self.ensure_one()
        return {"type": "ir.actions.act_url", "url": self.get_auth_url(), "target": "self"}

    def action_disconnect(self):
        self.ensure_one()
        self._check_owner_or_admin()
        self.sudo().write({
            "access_token": False, "refresh_token": False, "token_expiry": False,
            "oauth_state": False, "oauth_state_expiry": False, "state": "not_connected",
        })
        return True
