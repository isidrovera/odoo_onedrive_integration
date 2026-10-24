# -*- coding: utf-8 -*-
"""Conexiones delegadas Microsoft 365 por usuario de Odoo 19."""
import logging
import secrets
from datetime import timedelta
from urllib.parse import urlencode

from odoo import api, fields, models, _
from odoo.exceptions import AccessError, UserError, ValidationError

_logger = logging.getLogger(__name__)


class MicrosoftUserConnection(models.Model):
    _name = "microsoft.user.connection"
    _description = "Conexión personal Microsoft"
    _order = "user_id, tenant_id"

    user_id = fields.Many2one(
        "res.users", required=True, default=lambda self: self.env.user,
        ondelete="cascade", index=True,
    )
    tenant_id = fields.Many2one("onedrive.account", required=True, ondelete="cascade", index=True)
    email = fields.Char(required=True)
    entra_user_id = fields.Char(string="ID Microsoft", copy=False, index=True)
    access_token = fields.Char(copy=False, groups="odoo_onedrive_integration.group_microsoft_admin")
    refresh_token = fields.Char(copy=False, groups="odoo_onedrive_integration.group_microsoft_admin")
    token_expiry = fields.Datetime(copy=False, groups="odoo_onedrive_integration.group_microsoft_admin")
    oauth_state = fields.Char(copy=False, groups="odoo_onedrive_integration.group_microsoft_admin")
    oauth_state_expiry = fields.Datetime(copy=False, groups="odoo_onedrive_integration.group_microsoft_admin")
    state = fields.Selection([
        ("not_connected", "Sin conectar"),
        ("connected", "Conectado"),
        ("expired", "Requiere conexión"),
        ("error", "Error"),
    ], default="not_connected", required=True, readonly=True)
    last_error = fields.Text(readonly=True)
    last_connected = fields.Datetime(readonly=True)
    active = fields.Boolean(default=True)

    _microsoft_user_tenant_unique = models.Constraint(
        "UNIQUE(user_id, tenant_id)",
        "El usuario ya tiene una conexión para este tenant.",
    )

    @api.constrains("email")
    def _check_email(self):
        for record in self:
            value = (record.email or "").strip()
            if not value or "@" not in value or len(value) > 254 or any(c.isspace() for c in value):
                raise ValidationError(_("Introduzca un correo electrónico Microsoft válido."))

    @api.constrains("user_id", "tenant_id")
    def _check_user_and_tenant(self):
        for record in self:
            if record.user_id.share:
                raise ValidationError(_("La conexión Microsoft debe pertenecer a un usuario interno de Odoo."))

    def _check_owner_or_admin(self):
        self.ensure_one()
        user = self.env.user
        if self.sudo().user_id.id != user.id and not user.has_group("odoo_onedrive_integration.group_microsoft_admin"):
            raise AccessError(_("No puede administrar la conexión Microsoft de otro usuario."))
        if not self.sudo().active or not self.sudo().tenant_id.active:
            raise UserError(_("La conexión o el tenant Microsoft están desactivados."))

    def _scope(self):
        return self.env["ir.config_parameter"].sudo().get_param(
            "microsoft365.oauth.delegated_scope",
            "openid profile email offline_access User.Read Files.ReadWrite.All Sites.ReadWrite.All",
        )

    def get_auth_url(self):
        self.ensure_one()
        self._check_owner_or_admin()
        tenant = self.sudo().tenant_id
        if not tenant.client_id or not tenant.redirect_uri:
            raise UserError(_("Configure primero la aplicación Microsoft y su URL de retorno."))
        state = "u." + secrets.token_urlsafe(40)
        self.sudo().write({
            "oauth_state": state,
            "oauth_state_expiry": fields.Datetime.now() + timedelta(minutes=10),
            "last_error": False,
        })
        params = {
            "client_id": tenant.client_id,
            "response_type": "code",
            "redirect_uri": tenant.redirect_uri,
            "response_mode": "query",
            "scope": self._scope(),
            "state": state,
            "login_hint": self.sudo().email.strip(),
        }
        return f"{tenant._authority()}/oauth2/v2.0/authorize?{urlencode(params)}"

    def exchange_code_for_token(self, code, state):
        self.ensure_one()
        self._check_owner_or_admin()
        # El controlador comprueba además que el callback pertenece al usuario Odoo.
        if self.sudo().user_id.id != self.env.user.id:
            raise AccessError(_("El retorno OAuth debe completarlo el propietario de la conexión."))
        record = self.sudo()
        if not state or not secrets.compare_digest(str(state), str(record.oauth_state or "")) or not code:
            raise UserError(_("La respuesta OAuth no corresponde a esta conexión personal."))
        if not record.oauth_state_expiry or fields.Datetime.now() > record.oauth_state_expiry:
            raise UserError(_("La conexión venció. Vuelva a iniciarla desde Odoo."))

        # Consumir el estado antes de solicitar el token. En errores la transacción
        # habitual de Odoo se revierte, por lo que no se informa falsamente conexión.
        record.write({"oauth_state": False, "oauth_state_expiry": False})
        tenant = record.tenant_id
        result = tenant._token_request({
            "client_id": tenant.client_id,
            "client_secret": tenant.client_secret,
            "code": code,
            "redirect_uri": tenant.redirect_uri,
            "grant_type": "authorization_code",
            "scope": self._scope(),
        })
        if not result.get("access_token"):
            raise UserError(_("Microsoft no devolvió un token válido."))

        from ..services.graph_client import GraphClient
        profile = GraphClient(token=result["access_token"], env=self.env).request(
            "GET", "/me", params={"$select": "id,displayName,mail,userPrincipalName"},
        )
        if not isinstance(profile, dict) or not profile.get("id"):
            raise UserError(_("No se pudo identificar la cuenta Microsoft conectada."))
        self._save_token(result)
        record.write({
            "entra_user_id": profile["id"],
            # Invitados B2B pueden tener un UPN #EXT#; conservar su correo real.
            "email": profile.get("mail") or record.email,
            "last_connected": fields.Datetime.now(),
        })
        _logger.info("Microsoft 365: conexión delegada establecida usuario=%s tenant=%s", record.user_id.id, tenant.id)
        return True

    def _save_token(self, result):
        self.ensure_one()
        token = result.get("access_token") if isinstance(result, dict) else None
        if not token:
            raise UserError(_("Microsoft no devolvió un access_token válido."))
        try:
            expires = int(result.get("expires_in", 3600))
        except (ValueError, TypeError):
            expires = 3600
        current_refresh = self.sudo().refresh_token
        self.sudo().write({
            "access_token": token,
            "refresh_token": result.get("refresh_token") or current_refresh,
            "token_expiry": fields.Datetime.now() + timedelta(seconds=max(expires - 90, 1)),
            "oauth_state": False,
            "oauth_state_expiry": False,
            "state": "connected",
            "last_error": False,
        })

    def refresh_access_token(self):
        self.ensure_one()
        self._check_owner_or_admin()
        refresh = self.sudo().refresh_token
        if not refresh:
            raise UserError(_("Debe conectar nuevamente su cuenta Microsoft."))
        try:
            tenant = self.sudo().tenant_id
            result = tenant._token_request({
                "client_id": tenant.client_id,
                "client_secret": tenant.client_secret,
                "refresh_token": refresh,
                "grant_type": "refresh_token",
                "scope": self._scope(),
            })
            self._save_token(result)
        except Exception:
            # No registrar tokens ni respuestas HTTP potencialmente sensibles.
            _logger.warning("Microsoft 365: no se pudo renovar token usuario=%s tenant=%s", self.sudo().user_id.id, self.sudo().tenant_id.id)
            self.sudo().write({
                "access_token": False,
                "token_expiry": False,
                "state": "expired",
                "last_error": _("No fue posible renovar el token. Vuelva a conectar Microsoft."),
            })
            # Evitar lanzar aquí una excepción que revierta este estado.
            return False
        return True

    def get_valid_token(self):
        self.ensure_one()
        self._check_owner_or_admin()
        record = self.sudo()
        now = fields.Datetime.now()
        if not record.access_token or not record.token_expiry or now >= record.token_expiry:
            if not record.refresh_token or not self.refresh_access_token():
                raise UserError(_("La conexión Microsoft venció. Acceda a Mi conexión y vuelva a conectar."))
            record.invalidate_recordset(["access_token", "token_expiry", "state"])
        if not record.access_token or not record.token_expiry or fields.Datetime.now() >= record.token_expiry:
            raise UserError(_("No existe un token Microsoft vigente."))
        return record.access_token

    def action_connect(self):
        self.ensure_one()
        return {"type": "ir.actions.act_url", "url": self.get_auth_url(), "target": "self"}

    def action_disconnect(self):
        self.ensure_one()
        self._check_owner_or_admin()
        self.sudo().write({
            "access_token": False,
            "refresh_token": False,
            "token_expiry": False,
            "oauth_state": False,
            "oauth_state_expiry": False,
            "entra_user_id": False,
            "state": "not_connected",
            "last_error": False,
        })
        _logger.info("Microsoft 365: conexión delegada desconectada usuario=%s tenant=%s", self.sudo().user_id.id, self.sudo().tenant_id.id)
        return True
