# -*- coding: utf-8 -*-
"""Invitaciones Microsoft Entra B2B y sincronización con Odoo 19.

El estado aceptado proviene exclusivamente de Microsoft Graph. Las invitaciones
personalizadas se envían desde mail.mail, sin suscribir seguidores al registro.
"""
import logging
import re
from html import escape
from urllib.parse import urlsplit

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

_logger = logging.getLogger(__name__)
_EMAIL_RE = re.compile(r"^[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+$")
_ADMIN_GROUP = "odoo_onedrive_integration.group_microsoft_admin"


class MicrosoftGuest(models.Model):
    _name = "microsoft.guest"
    _description = "Usuario invitado Microsoft"
    _rec_name = "name"
    _order = "name, email, id"
    _inherit = ["mail.thread", "mail.activity.mixin"]

    tenant_id = fields.Many2one("onedrive.account", required=True, ondelete="cascade", tracking=True, index=True)
    user_id = fields.Many2one("res.users", string="Usuario Odoo", ondelete="set null", tracking=True, index=True)
    name = fields.Char(string="Nombre", required=True, tracking=True)
    email = fields.Char(required=True, tracking=True, index=True)
    entra_user_id = fields.Char(string="ID Microsoft", copy=False, readonly=True, index=True)
    user_principal_name = fields.Char(string="UPN invitado", copy=False, readonly=True)
    state = fields.Selection([
        ("draft", "No registrado"),
        ("pending", "Pendiente de aceptación"),
        ("accepted", "Aceptado"),
        ("blocked", "Bloqueado"),
        ("error", "Error"),
    ], default="draft", required=True, tracking=True, index=True)
    account_enabled = fields.Boolean(default=True, readonly=True)
    group_ids = fields.Many2many("microsoft.group", "microsoft_group_guest_rel", "guest_id", "group_id", string="Grupos como miembro")
    owner_group_ids = fields.Many2many("microsoft.group", "microsoft_group_owner_guest_rel", "guest_id", "group_id", string="Grupos como propietario")
    invitation_url = fields.Char(copy=False, groups=_ADMIN_GROUP)
    invitation_date = fields.Datetime(readonly=True)
    accepted_date = fields.Datetime(readonly=True)
    last_sync = fields.Datetime(readonly=True)
    last_error = fields.Text(readonly=True)
    active = fields.Boolean(default=True)

    _microsoft_guest_email_tenant_unique = models.Constraint(
        "UNIQUE(tenant_id, email)", "El correo ya está registrado en este tenant."
    )
    _microsoft_guest_entra_tenant_unique = models.Constraint(
        "UNIQUE(tenant_id, entra_user_id)", "La identidad Microsoft ya está vinculada."
    )

    @api.model
    def _clean_email(self, email):
        return (email or "").strip().lower()

    @api.constrains("email")
    def _check_email(self):
        for guest in self:
            if not _EMAIL_RE.fullmatch(guest.email or "") or len(guest.email) > 254:
                raise ValidationError("Ingrese un correo electrónico válido.")

    @api.constrains("tenant_id", "group_ids", "owner_group_ids", "user_id")
    def _check_tenant_consistency(self):
        for guest in self:
            if any(group.tenant_id != guest.tenant_id for group in guest.group_ids | guest.owner_group_ids):
                raise ValidationError("Todos los grupos deben pertenecer al tenant del invitado.")
            if guest.user_id and guest.user_id.microsoft_tenant_id and guest.user_id.microsoft_tenant_id != guest.tenant_id:
                raise ValidationError("El tenant del invitado debe coincidir con el del usuario Odoo vinculado.")

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        for vals in vals_list:
            vals = dict(vals)
            legacy_name = vals.pop("display_name", False)
            if "email" in vals:
                vals["email"] = self._clean_email(vals["email"])
            if not vals.get("name"):
                vals["name"] = legacy_name or vals.get("email") or "Invitado Microsoft"
            prepared.append(vals)
        return super().create(prepared)

    def write(self, vals):
        vals = dict(vals)
        legacy_name = vals.pop("display_name", False)
        if legacy_name and not vals.get("name"):
            vals["name"] = legacy_name
        if "email" in vals:
            vals["email"] = self._clean_email(vals["email"])
        return super().write(vals)

    def _check_admin(self):
        if not self.env.user.has_group(_ADMIN_GROUP):
            raise AccessError("Solo el administrador Microsoft 365 puede administrar invitados.")

    @staticmethod
    def _remote_identity(data):
        if not isinstance(data, dict):
            return {}
        invited = data.get("invitedUser") or {}
        if isinstance(invited, dict) and invited.get("id"):
            return invited
        return data

    def _apply_remote_user(self, data):
        """No inferir aceptación si Graph omite externalUserState."""
        self.ensure_one()
        remote = self._remote_identity(data)
        if not remote.get("id"):
            raise UserError("Microsoft no devolvió una identidad de usuario válida.")
        if self.entra_user_id and remote["id"] != self.entra_user_id:
            raise UserError("Microsoft devolvió una identidad distinta a la registrada.")

        external_raw = remote.get("externalUserState")
        external = str(external_raw or "").strip().lower().replace(" ", "")
        user_type = str(remote.get("userType") or "").strip().lower()
        enabled = remote.get("accountEnabled")
        # Un objeto parcial de POST /invitations no implica aceptación.
        if enabled is False:
            state = "blocked"
        elif external == "accepted":
            state = "accepted"
        elif external in ("pendingacceptance", "pending"):
            state = "pending"
        elif user_type == "member":
            # Los usuarios Member no necesariamente pasan por invitación B2B.
            state = self.state if self.state != "draft" else "pending"
        elif external_raw is None:
            state = self.state if self.state not in ("draft", "error") else "pending"
        else:
            _logger.warning("[MICROSOFT][GUEST][UNKNOWN_STATE] guest=%s value=%s", self.id, external[:40])
            state = self.state if self.state != "draft" else "pending"

        values = {
            "entra_user_id": remote["id"],
            "user_principal_name": remote.get("userPrincipalName") or self.user_principal_name or False,
            "state": state,
            "last_sync": fields.Datetime.now(),
            "last_error": False,
        }
        if enabled is not None:
            values["account_enabled"] = bool(enabled)
        if state == "accepted" and not self.accepted_date:
            values["accepted_date"] = fields.Datetime.now()
        if state == "pending" and external in ("pendingacceptance", "pending"):
            values["accepted_date"] = False
        self.sudo().with_context(tracking_disable=True, mail_create_nosubscribe=True).write(values)
        _logger.info(
            "[MICROSOFT][GUEST][STATE] guest=%s tenant=%s user=%s remote=%s local=%s",
            self.id, self.tenant_id.id, remote["id"], external or "not_provided", state,
        )
        return state

    def _audit(self, operation, status="success", message=None):
        self.ensure_one()
        try:
            self.env["microsoft.audit.log"]._log(
                operation, status=status, tenant_id=self.tenant_id.id,
                item_id=self.entra_user_id or False, item_name=self.email,
                message=message,
            )
        except Exception:
            _logger.exception("[MICROSOFT][GUEST][AUDIT_ERROR] guest=%s operation=%s", self.id, operation)

    def _fetch_remote(self, directory):
        self.ensure_one()
        data = None
        if self.entra_user_id:
            data = directory.get_user(self.entra_user_id)
            if not isinstance(data, dict) or not data.get("id"):
                data = None
        if not data:
            data = directory.find_user_by_email(self.email)
        return data if isinstance(data, dict) and data.get("id") else None

    @staticmethod
    def _validate_invitation_url(url):
        if not isinstance(url, str) or len(url) > 10000:
            raise UserError("Microsoft no devolvió un enlace de aceptación válido.")
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        # Enlaces B2B de Microsoft pueden utilizar login.microsoftonline.com,
        # invitations.microsoft.com y distintos endpoints bajo microsoft.com.
        allowed = (
            host in ("login.microsoftonline.com", "invitations.microsoft.com")
            or host.endswith(".login.microsoftonline.com")
            or host.endswith(".microsoft.com")
        )
        if (parsed.scheme != "https" or not allowed or parsed.username is not None
                or parsed.password is not None or parsed.port not in (None, 443)
                or parsed.fragment):
            raise UserError("El enlace de aceptación no tiene un dominio Microsoft autorizado.")
        return url

    def _send_invitation_email(self, url):
        """Correo transaccional aislado, con el enlace B2B auténtico."""
        self.ensure_one()
        url = self._validate_invitation_url(url)
        name = escape(self.name or self.email)
        tenant_name = escape(self.tenant_id.name or "Microsoft 365")
        safe_url = escape(url, quote=True)
        body = (
            '<div style="font-family:Arial,sans-serif;font-size:14px;color:#243247;line-height:1.6;max-width:600px">'
            '<h2>Invitación de acceso a Microsoft 365</h2>'
            f'<p>Hola, {name}:</p>'
            f'<p>Has recibido una invitación para acceder al entorno documental de <strong>{tenant_name}</strong>.</p>'
            '<p>Para activar tu acceso, inicia sesión con el correo al que llegó este mensaje '
            'y completa los pasos de autorización de Microsoft.</p>'
            f'<p><a href="{safe_url}" style="display:inline-block;padding:12px 20px;'
            'background:#0f6cbd;color:white;text-decoration:none;border-radius:6px;font-weight:bold">'
            'Aceptar invitación Microsoft 365</a></p>'
            '<p>Si no solicitaste este acceso, puedes ignorar este correo.</p>'
            '<p style="font-size:12px;color:#718096">Este enlace es individual. No lo compartas.</p>'
            '</div>'
        )
        mail = self.env["mail.mail"].sudo().create({
            "subject": "Invitación de acceso a Microsoft 365 - %s" % self.tenant_id.name,
            "email_to": self.email,
            "body_html": body,
            "auto_delete": False,
        })
        # send() devuelve sin excepción cuando el correo no logra salir en
        # algunas configuraciones: comprobar el estado real después del envío.
        mail.send(raise_exception=False)
        mail.invalidate_recordset(["state", "failure_reason"])
        if mail.state != "sent":
            _logger.warning(
                "[MICROSOFT][GUEST][MAIL_FAILED] guest=%s mail_id=%s state=%s",
                self.id, mail.id, mail.state,
            )
            raise UserError("La invitación se creó en Microsoft, pero el correo Odoo no pudo enviarse. Revise el servidor saliente.")
        _logger.info("[MICROSOFT][GUEST][MAIL_SENT] guest=%s mail_id=%s", self.id, mail.id)
        return True

    def _record_invitation(self, invitation):
        self.ensure_one()
        remote = self._remote_identity(invitation)
        if not remote.get("id"):
            raise UserError("Microsoft no devolvió el identificador del invitado.")
        url = invitation.get("inviteRedeemUrl")
        if not url:
            raise UserError("Microsoft no devolvió el enlace para aceptar la invitación.")
        self._validate_invitation_url(url)
        self.sudo().with_context(tracking_disable=True).write({
            "invitation_url": url,
            "invitation_date": fields.Datetime.now(),
            "last_error": False,
        })
        self._apply_remote_user(remote)
        return url

    def action_invite(self):
        """Crear invitados nuevos; para invitados existentes no reiniciar aceptación."""
        self._check_admin()
        from ..services.directory_service import DirectoryService

        failed = []
        for guest in self:
            try:
                with self.env.cr.savepoint():
                    directory = DirectoryService(guest.tenant_id)
                    remote = guest._fetch_remote(directory)
                    if remote:
                        guest._apply_remote_user(remote)
                        if guest.state == "accepted":
                            _logger.info("[MICROSOFT][GUEST][ALREADY_ACCEPTED] guest=%s", guest.id)
                        elif guest.invitation_url:
                            guest._send_invitation_email(guest.invitation_url)
                        else:
                            _logger.info("[MICROSOFT][GUEST][PENDING_NO_LINK] guest=%s", guest.id)
                            raise UserError("El invitado ya existe pero no tiene un enlace reutilizable. Utilice Reenviar invitación.")
                    else:
                        invitation = directory.invite_guest(guest.email, guest.name, send_message=False)
                        if invitation.get("existingUser"):
                            raise UserError("El invitado ya existe. Actualice su estado antes de invitar.")
                        link = guest._record_invitation(invitation)
                        guest._send_invitation_email(link)
                    if guest.entra_user_id:
                        guest.action_apply_groups()
                    guest._audit("invite", message="Invitado procesado desde Odoo.")
            except Exception as exc:
                _logger.exception("[MICROSOFT][GUEST][INVITE_ERROR] tenant=%s guest=%s", guest.tenant_id.id, guest.id)
                guest.sudo().with_context(tracking_disable=True).write({
                    "last_error": str(exc)[:1000], "last_sync": fields.Datetime.now(),
                    "state": "error" if not guest.entra_user_id else guest.state,
                })
                guest._audit("invite", status="error", message=str(exc)[:300])
                failed.append(guest.email)
        if failed:
            _logger.warning("[MICROSOFT][GUEST][INVITES_FAILED] count=%s", len(failed))
        return True

    def action_resend_invitation(self):
        """Reenvío explícito: restablece una invitación aún pendiente en Entra."""
        self._check_admin()
        from ..services.directory_service import DirectoryService

        for guest in self:
            try:
                with self.env.cr.savepoint():
                    directory = DirectoryService(guest.tenant_id)
                    remote = guest._fetch_remote(directory)
                    if not remote:
                        raise UserError("El invitado todavía no existe en Microsoft Entra. Use Registrar / invitar.")
                    guest._apply_remote_user(remote)
                    if guest.state == "accepted":
                        raise UserError("La invitación ya fue aceptada. No se debe reenviar.")
                    if str(remote.get("externalUserState") or "").lower() != "pendingacceptance":
                        raise UserError("Microsoft no confirma que la invitación esté pendiente.")
                    invitation = directory.reset_guest_invitation(remote["id"], guest.email, send_message=False)
                    link = guest._record_invitation(invitation)
                    guest._send_invitation_email(link)
                    guest._audit("invite", message="Invitación B2B restablecida y reenviada.")
            except Exception as exc:
                _logger.exception("[MICROSOFT][GUEST][RESEND_ERROR] guest=%s", guest.id)
                guest.sudo().with_context(tracking_disable=True).write({"last_error": str(exc)[:1000]})
        return True

    def action_sync(self):
        self._check_admin()
        from ..services.directory_service import DirectoryService
        for guest in self:
            try:
                with self.env.cr.savepoint():
                    remote = guest._fetch_remote(DirectoryService(guest.tenant_id))
                    if remote:
                        guest._apply_remote_user(remote)
                    else:
                        guest.sudo().with_context(tracking_disable=True).write({
                            "state": "error", "last_error": "Usuario no encontrado en Microsoft Entra.",
                            "last_sync": fields.Datetime.now(),
                        })
            except Exception as exc:
                _logger.exception("[MICROSOFT][GUEST][SYNC_ERROR] guest=%s", guest.id)
                guest.sudo().with_context(tracking_disable=True).write({
                    "last_error": str(exc)[:1000], "last_sync": fields.Datetime.now(),
                })
        return True

    def action_apply_groups(self):
        self._check_admin()
        for guest in self:
            if not guest.entra_user_id:
                raise UserError("Primero invite a %s." % guest.email)
            for group in guest.group_ids | guest.owner_group_ids:
                if group.tenant_id != guest.tenant_id:
                    raise ValidationError("El grupo pertenece a otro tenant Microsoft.")
                group.action_apply_members()
        return True

    @api.model
    def _cron_sync_pending_guests(self):
        """La fuente del estado es Graph; no se acepta a nadie por la URL de Odoo."""
        from ..services.directory_service import DirectoryService
        guests = self.sudo().search([
            ("state", "in", ("pending", "error")),
            ("entra_user_id", "!=", False),
            ("active", "=", True),
        ], limit=500)
        _logger.info("[MICROSOFT][GUEST][CRON_START] count=%s", len(guests))
        for guest in guests:
            try:
                with self.env.cr.savepoint():
                    remote = guest._fetch_remote(DirectoryService(guest.tenant_id))
                    if remote:
                        guest._apply_remote_user(remote)
                    else:
                        guest.sudo().with_context(tracking_disable=True).write({
                            "state": "error", "last_error": "Identidad no encontrada en Microsoft Entra.",
                            "last_sync": fields.Datetime.now(),
                        })
            except Exception as exc:
                _logger.exception("[MICROSOFT][GUEST][CRON_ERROR] guest=%s", guest.id)
                guest.sudo().with_context(tracking_disable=True).write({
                    "last_error": str(exc)[:1000], "last_sync": fields.Datetime.now(),
                })
        _logger.info("[MICROSOFT][GUEST][CRON_DONE] count=%s", len(guests))
        return True
