# -*- coding: utf-8 -*-
import logging
import secrets
from datetime import timedelta
from urllib.parse import urlencode, urlparse
from uuid import UUID

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

    
    _microsoft_tenant_client_unique = models.Constraint(
        'UNIQUE(tenant_id, client_id)',
        'Esta aplicación ya está registrada para el tenant.',
    )


    @api.depends("access_token", "refresh_token", "token_expiry", "app_access_token", "app_token_expiry")
    def _compute_connection_status(self):
        now = fields.Datetime.now()
        for record in self:
            # Los tokens son secretos: el cálculo usa lectura privilegiada,
            # pero solo publica el estado de conexión (nunca el token).
            secure_record = record.sudo()
            delegated_ok = bool(secure_record.access_token and secure_record.token_expiry and secure_record.token_expiry > now)
            app_ok = bool(secure_record.app_access_token and secure_record.app_token_expiry and secure_record.app_token_expiry > now)
            record.is_connected = delegated_ok or app_ok
            if record.is_connected:
                record.token_status = "connected"
            elif secure_record.refresh_token or secure_record.app_access_token:
                record.token_status = "expired"
            else:
                record.token_status = "not_connected"

    @api.depends_context("uid")
    def _compute_redirect_uri(self):
        base_url = self.env["ir.config_parameter"].sudo().get_param("web.base.url", "").rstrip("/")
        for record in self:
            record.redirect_uri = f"{base_url}/microsoft/oauth/callback" if base_url else False

    @api.constrains("tenant_id", "client_id")
    def _check_identifiers(self):
        for record in self:
            for value, label in ((record.tenant_id, "Tenant ID"), (record.client_id, "Client ID")):
                try:
                    UUID((value or "").strip())
                except (ValueError, AttributeError, TypeError) as error:
                    raise ValidationError(f"{label} debe ser un identificador UUID válido.") from error

    @api.constrains("sharepoint_root_url")
    def _check_sharepoint_root_url(self):
        for record in self:
            if not record.sharepoint_root_url:
                continue
            parsed = urlparse(record.sharepoint_root_url.strip())
            if (parsed.scheme != "https" or not parsed.hostname
                    or not parsed.hostname.lower().endswith(".sharepoint.com")
                    or parsed.port not in (None, 443) or parsed.username or parsed.password
                    or parsed.query or parsed.fragment or parsed.path not in ("", "/")):
                raise ValidationError("La URL raíz debe ser HTTPS y pertenecer a un dominio *.sharepoint.com.")

    def _check_admin(self):
        if not self.env.user.has_group("odoo_onedrive_integration.group_microsoft_admin"):
            raise AccessError("Solo un administrador Microsoft 365 puede realizar esta operación.")

    def _authority(self):
        self.ensure_one()
        try:
            tenant_uuid = str(UUID((self.tenant_id or "").strip()))
        except (ValueError, AttributeError, TypeError) as error:
            raise UserError("Tenant ID inválido. Debe ser un GUID de Microsoft Entra.") from error
        return f"https://login.microsoftonline.com/{tenant_uuid}"

    def _delegated_scope(self):
        return self.env["ir.config_parameter"].sudo().get_param(
            "microsoft365.oauth.delegated_scope",
            "openid profile email offline_access User.Read Files.ReadWrite.All Sites.ReadWrite.All",
        )

    def _new_oauth_state(self):
        self.ensure_one()
        self._check_admin()
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
            response = requests.post(
                f"{self._authority()}/oauth2/v2.0/token", data=data,
                timeout=(10, 30), allow_redirects=False,
            )
            result = response.json()
        except requests.RequestException as error:
            raise UserError(f"No se pudo conectar con Microsoft: {error}") from error
        except ValueError as error:
            raise UserError("Microsoft devolvió una respuesta inválida al solicitar el token.") from error
        if not isinstance(result, dict):
            raise UserError("Microsoft devolvió una respuesta de token incorrecta.")
        if response.status_code >= 400 or response.status_code < 200 or response.status_code >= 300:
            message = result.get("error_description") or result.get("error") or "Solicitud rechazada"
            _logger.warning("Token Microsoft rechazado para tenant id=%s; código HTTP=%s", self.id, response.status_code)
            raise UserError(f"Microsoft rechazó la autenticación: {str(message)[:400]}")
        if not result.get("access_token"):
            raise UserError("Microsoft no devolvió un access_token válido.")
        return result

    def exchange_code_for_token(self, code, state):
        self.ensure_one()
        self._check_admin()
        if not code or not state or state != self.oauth_state:
            raise UserError("La respuesta OAuth no corresponde a esta conexión.")
        if not self.oauth_state_expiry or fields.Datetime.now() > self.oauth_state_expiry:
            raise UserError("La solicitud OAuth venció. Vuelva a conectar la cuenta.")
        if self.oauth_state_user_id != self.env.user:
            raise AccessError("La conexión debe finalizarla el administrador que la inició.")
        # Consume el estado antes de la llamada exterior para impedir reutilización.
        # El estado expirado debe renovarse iniciando OAuth otra vez.
        self.sudo().write({
            "oauth_state": False,
            "oauth_state_expiry": False,
            "oauth_state_user_id": False,
        })
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
        self.ensure_one()
        if not result.get("access_token"):
            raise UserError("Microsoft no devolvió un token delegado.")
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
        self._check_admin()
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
        """Compatibilidad con el explorador OneDrive existente.

        El token histórico solo puede obtenerse por administradores autorizados.
        Los usuarios normales usan microsoft.user.connection y su token propio.
        """
        self.ensure_one()
        self._check_admin()
        if self.refresh_token and (not self.access_token or not self.token_expiry or fields.Datetime.now() >= self.token_expiry):
            self.sudo().refresh_access_token()
        if not self.access_token or not self.token_expiry or fields.Datetime.now() >= self.token_expiry:
            raise UserError("La cuenta OneDrive no dispone de un token delegado vigente.")
        return self.sudo().access_token

    def get_app_token(self, resource="graph"):
        self.ensure_one()
        # Acceso interno a secretos del tenant; los controladores verifican permisos
        # de ubicación antes de invocar los servicios de Graph.
        tenant = self.sudo()
        if resource not in ("graph", "sharepoint"):
            raise UserError("Recurso de autenticación Microsoft no permitido.")
        # La seguridad de los endpoints se aplica en los modelos y controladores.
        # No se expone este método como ruta pública ni se devuelve el token al cliente.
        now = fields.Datetime.now()
        if resource == "sharepoint":
            if not tenant.sharepoint_root_url:
                raise UserError("Configure la URL raíz de SharePoint.")
            token_field, expiry_field = "sharepoint_access_token", "sharepoint_token_expiry"
            parsed = urlparse(tenant.sharepoint_root_url.strip())
            if (parsed.scheme != "https" or not parsed.hostname
                    or not parsed.hostname.lower().endswith(".sharepoint.com")
                    or parsed.port not in (None, 443) or parsed.username or parsed.password
                    or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
                raise UserError("La URL raíz SharePoint no es válida.")
            scope = f"https://{parsed.hostname}/.default"
        else:
            token_field, expiry_field = "app_access_token", "app_token_expiry"
            scope = "https://graph.microsoft.com/.default"
        token = tenant[token_field]
        expiry = tenant[expiry_field]
        if token and expiry and expiry > now:
            return token
        result = tenant._token_request({
            "client_id": tenant.client_id,
            "client_secret": tenant.client_secret,
            "grant_type": "client_credentials",
            "scope": scope,
        })
        if not result.get("access_token"):
            raise UserError("Microsoft no devolvió un token de aplicación.")
        expires = int(result.get("expires_in", 3600))
        tenant.write({
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
            try:
                DirectoryService(tenant).sync_groups()
                SharePointService(tenant).sync_sites()
                tenant.sudo().write({"last_sync": fields.Datetime.now(), "last_error": False})
            except Exception as error:
                _logger.exception("Error sincronizando Microsoft 365 tenant id=%s", tenant.id)
                # Registrar diagnóstico mediante cursor aislado no es seguro aquí;
                # no silenciamos el fallo ni persistimos datos incompletos.
                raise UserError("Falló la sincronización de grupos o sitios: %s" % str(error)[:400]) from error
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
