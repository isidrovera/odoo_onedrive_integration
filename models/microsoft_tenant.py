# -*- coding: utf-8 -*-
import logging
import secrets
from datetime import timedelta
from urllib.parse import urlencode, urlparse

import requests

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

_logger = logging.getLogger(__name__)


class MicrosoftTenant(models.Model):
    _name = "onedrive.account"
    _description = "Tenant Microsoft 365"
    _rec_name = "name"
    _order = "name"

    name = fields.Char(required=True)
    client_id = fields.Char(string="Client ID", required=True, groups="odoo_onedrive_integration.group_microsoft_admin")
    client_secret = fields.Char(string="Client Secret", required=True, copy=False, groups="odoo_onedrive_integration.group_microsoft_admin")
    tenant_id = fields.Char(string="Tenant ID", required=True, groups="odoo_onedrive_integration.group_microsoft_admin")
    sharepoint_root_url = fields.Char(
        string="URL raíz SharePoint",
        help="Ejemplo: https://empresa.sharepoint.com",
        groups="odoo_onedrive_integration.group_microsoft_admin",
    )
    redirect_uri = fields.Char(compute="_compute_redirect_uri", string="Redirect URI")
    invite_redirect_url = fields.Char(
        string="URL después de aceptar",
        help="Si está vacío, el invitado será enviado a Odoo.",
    )
    active = fields.Boolean(default=True)

    # Token delegado histórico. Se conserva para no romper la instalación actual.
    access_token = fields.Char(copy=False, groups="odoo_onedrive_integration.group_microsoft_admin")
    refresh_token = fields.Char(copy=False, groups="odoo_onedrive_integration.group_microsoft_admin")
    token_expiry = fields.Datetime(copy=False, groups="odoo_onedrive_integration.group_microsoft_admin")
    oauth_state = fields.Char(copy=False, groups="odoo_onedrive_integration.group_microsoft_admin")
    oauth_state_expiry = fields.Datetime(copy=False, groups="odoo_onedrive_integration.group_microsoft_admin")
    oauth_state_user_id = fields.Many2one("res.users", copy=False, groups="odoo_onedrive_integration.group_microsoft_admin")

    # Tokens de aplicación. Nunca se muestran en vistas.
    app_access_token = fields.Char(copy=False, groups="odoo_onedrive_integration.group_microsoft_admin")
    app_token_expiry = fields.Datetime(copy=False, groups="odoo_onedrive_integration.group_microsoft_admin")
    sharepoint_access_token = fields.Char(copy=False, groups="odoo_onedrive_integration.group_microsoft_admin")
    sharepoint_token_expiry = fields.Datetime(copy=False, groups="odoo_onedrive_integration.group_microsoft_admin")

    is_connected = fields.Boolean(compute="_compute_connection_status")
    token_status = fields.Selection(
        [("not_connected", "Sin conexión"), ("connected", "Conectado"), ("expired", "Requiere renovación")],
        compute="_compute_connection_status",
    )
    last_sync = fields.Datetime(readonly=True)
    last_error = fields.Text(readonly=True)

    _sql_constraints = [
        ("microsoft_tenant_client_unique", "unique(tenant_id, client_id)", "Esta aplicación ya está registrada para el tenant."),
    ]

    @api.depends("access_token", "refresh_token", "token_expiry", "app_access_token", "app_token_expiry")
    def _compute_connection_status(self):
        now = fields.Datetime.now()
        for record in self:
            delegated_ok = bool(record.access_token and record.token_expiry and record.token_expiry > now)
            app_ok = bool(record.app_access_token and record.app_token_expiry and record.app_token_expiry > now)
            record.is_connected = delegated_ok or app_ok
            if record.is_connected:
                record.token_status = "connected"
            elif record.refresh_token or record.app_access_token:
                record.token_status = "expired"
            else:
                record.token_status = "not_connected"

    @api.depends_context("uid")
    def _compute_redirect_uri(self):
        base_url = self.env["ir.config_parameter"].sudo().get_param("web.base.url", "").rstrip("/")
        for record in self:
            record.redirect_uri = f"{base_url}/microsoft/oauth/callback" if base_url else False

    @api.constrains("sharepoint_root_url")
    def _check_sharepoint_root_url(self):
        for record in self:
            if not record.sharepoint_root_url:
                continue
            parsed = urlparse(record.sharepoint_root_url.strip())
            if parsed.scheme != "https" or not parsed.netloc or parsed.path not in ("", "/"):
                raise ValidationError("La URL raíz debe tener el formato https://empresa.sharepoint.com")

    def _check_admin(self):
        if not self.env.user.has_group("odoo_onedrive_integration.group_microsoft_admin"):
            raise AccessError("Solo un administrador Microsoft 365 puede realizar esta operación.")

    def _authority(self):
        self.ensure_one()
        return f"https://login.microsoftonline.com/{self.tenant_id}"

    def _delegated_scope(self):
        return self.env["ir.config_parameter"].sudo().get_param(
            "microsoft365.oauth.delegated_scope",
            "openid profile email offline_access User.Read Files.ReadWrite.All Sites.ReadWrite.All",
        )

    def _new_oauth_state(self):
        self.ensure_one()
        state = "t." + secrets.token_urlsafe(40)
        self.sudo().write({
            "oauth_state": state,
            "oauth_state_expiry": fields.Datetime.now() + timedelta(minutes=10),
            "oauth_state_user_id": self.env.user.id,
        })
        return state

    def get_auth_url(self):
        self.ensure_one()
        self._check_admin()
        if not self.redirect_uri:
            raise UserError("Configure web.base.url antes de conectar Microsoft.")
        params = {
            "client_id": self.client_id,
            "response_type": "code",
            "redirect_uri": self.redirect_uri,
            "response_mode": "query",
            "scope": self._delegated_scope(),
            "state": self._new_oauth_state(),
            "prompt": "select_account",
        }
        return f"{self._authority()}/oauth2/v2.0/authorize?{urlencode(params)}"

    def _token_request(self, data):
        self.ensure_one()
        try:
            response = requests.post(f"{self._authority()}/oauth2/v2.0/token", data=data, timeout=30)
            result = response.json()
        except requests.RequestException as error:
            raise UserError(f"No se pudo conectar con Microsoft: {error}") from error
        except ValueError as error:
            raise UserError("Microsoft devolvió una respuesta inválida al solicitar el token.") from error
        if response.status_code >= 400:
            message = result.get("error_description") or result.get("error") or "Solicitud rechazada"
            _logger.warning("OAuth Microsoft rechazado para tenant %s: %s", self.id, message)
            raise UserError(f"Microsoft rechazó la autenticación: {message}")
        return result

    def exchange_code_for_token(self, code, state):
        self.ensure_one()
        if not code or not state or state != self.oauth_state:
            raise UserError("La respuesta OAuth no corresponde a esta conexión.")
        if not self.oauth_state_expiry or fields.Datetime.now() > self.oauth_state_expiry:
            raise UserError("La solicitud OAuth venció. Vuelva a conectar la cuenta.")
        if self.oauth_state_user_id != self.env.user:
            raise AccessError("La conexión debe finalizarla el administrador que la inició.")
        result = self._token_request({
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "code": code,
            "redirect_uri": self.redirect_uri,
            "grant_type": "authorization_code",
            "scope": self._delegated_scope(),
        })
        self._save_delegated_token(result)
        return True

    def _save_delegated_token(self, result):
        expires = int(result.get("expires_in", 3600))
        self.sudo().write({
            "access_token": result.get("access_token"),
            "refresh_token": result.get("refresh_token") or self.refresh_token,
            "token_expiry": fields.Datetime.now() + timedelta(seconds=max(expires - 90, 60)),
            "oauth_state": False,
            "oauth_state_expiry": False,
            "oauth_state_user_id": False,
            "last_error": False,
        })

    def refresh_access_token(self):
        self.ensure_one()
        if not self.refresh_token:
            raise UserError("No existe refresh token. Conecte nuevamente la cuenta.")
        result = self._token_request({
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": self.refresh_token,
            "grant_type": "refresh_token",
            "scope": self._delegated_scope(),
        })
        self._save_delegated_token(result)
        return True

    def get_valid_token(self):
        """Compatibilidad con el explorador OneDrive existente."""
        self.ensure_one()
        if self.refresh_token and (not self.access_token or not self.token_expiry or fields.Datetime.now() >= self.token_expiry):
            self.sudo().refresh_access_token()
        if not self.access_token:
            raise UserError("La cuenta OneDrive todavía no está conectada con un usuario Microsoft.")
        return self.sudo().access_token

    def get_app_token(self, resource="graph"):
        self.ensure_one()
        now = fields.Datetime.now()
        if resource == "sharepoint":
            if not self.sharepoint_root_url:
                raise UserError("Configure la URL raíz de SharePoint.")
            token_field, expiry_field = "sharepoint_access_token", "sharepoint_token_expiry"
            scope = self.sharepoint_root_url.rstrip("/") + "/.default"
        else:
            token_field, expiry_field = "app_access_token", "app_token_expiry"
            scope = "https://graph.microsoft.com/.default"
        token = self[token_field]
        expiry = self[expiry_field]
        if token and expiry and expiry > now:
            return token
        result = self._token_request({
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "client_credentials",
            "scope": scope,
        })
        expires = int(result.get("expires_in", 3600))
        self.sudo().write({
            token_field: result.get("access_token"),
            expiry_field: now + timedelta(seconds=max(expires - 90, 60)),
        })
        return result.get("access_token")

    def action_connect_onedrive(self):
        self.ensure_one()
        return {"type": "ir.actions.act_url", "url": self.get_auth_url(), "target": "self"}

    def action_refresh_token(self):
        self._check_admin()
        self.refresh_access_token()
        return self._notification("Token delegado renovado.", "success")

    def action_test_application(self):
        self._check_admin()
        from ..services.graph_client import GraphClient
        GraphClient(token=self.get_app_token(), env=self.env).request("GET", "/sites/root?$select=id,name,webUrl")
        return self._notification("La aplicación administrativa se conectó correctamente.", "success")

    def action_sync_directory(self):
        self._check_admin()
        from ..services.directory_service import DirectoryService
        from ..services.sharepoint_service import SharePointService
        for tenant in self:
            DirectoryService(tenant).sync_groups()
            SharePointService(tenant).sync_sites()
            tenant.sudo().write({"last_sync": fields.Datetime.now(), "last_error": False})
        return self._notification("Grupos y sitios sincronizados.", "success")

    def action_create_onedrive_location(self):
        """Crea la ubicación virtual /me/drive para los usuarios conectados."""
        self._check_admin()
        Location = self.env["microsoft.storage.location"].sudo()
        for tenant in self:
            location = Location.search([
                ("tenant_id", "=", tenant.id),
                ("location_type", "=", "onedrive"),
                ("drive_id", "=", False),
            ], limit=1)
            if not location:
                Location.create({
                    "name": f"OneDrive personal - {tenant.name}",
                    "tenant_id": tenant.id,
                    "location_type": "onedrive",
                })
        return self._notification("La ubicación OneDrive personal está preparada.", "success")

    def action_clear_tokens(self):
        self._check_admin()
        self.sudo().write({
            "access_token": False, "refresh_token": False, "token_expiry": False,
            "app_access_token": False, "app_token_expiry": False,
            "sharepoint_access_token": False, "sharepoint_token_expiry": False,
            "oauth_state": False, "oauth_state_expiry": False, "oauth_state_user_id": False,
        })
        return self._notification("Tokens eliminados.", "warning")

    def _notification(self, message, notification_type="info"):
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {"title": "Microsoft 365", "message": message, "type": notification_type, "sticky": False},
        }
