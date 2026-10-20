# -*- coding: utf-8 -*-
"""Invitados Microsoft Entra B2B para Odoo 19.

La cuenta local refleja el estado de Microsoft: no concede por sí misma
permisos de SharePoint. Las operaciones remotas usan DirectoryService.
"""
import logging
import re

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
        """Normaliza GET /users y POST /invitations, que tienen estructuras diferentes."""
        if not isinstance(data, dict):
            return {}
        invited = data.get("invitedUser") or {}
        if isinstance(invited, dict) and invited.get("id"):
            # La invitación trae un objeto parcial: nunca usar su 'status'
            # como equivalente al externalUserState de Microsoft Graph.
            return invited
        return data

    def _apply_remote_user(self, data):
        self.ensure_one()
        remote = self._remote_identity(data)
        if not remote.get("id"):
            raise UserError("Microsoft no devolvió una identidad de usuario válida.")
        external_state = str(remote.get("externalUserState") or "").strip().lower().replace(" ", "")
        user_type = str(remote.get("userType") or "").lower()
        enabled = remote.get("accountEnabled", True)
        if enabled is False:
            state = "blocked"
        elif user_type == "member" or external_state == "accepted":
            state = "accepted"
        elif external_state in ("pendingacceptance", "pending", ""):
            # En una respuesta de invitación no se puede asegurar aceptación.
            state = "pending" if not self.accepted_date else self.state
        else:
            state = "pending"
        values = {
            "entra_user_id": remote["id"],
            "user_principal_name": remote.get("userPrincipalName") or self.user_principal_name or False,
            "state": state,
            "account_enabled": bool(enabled),
            "last_sync": fields.Datetime.now(),
            "last_error": False,
        }
        if state == "accepted" and not self.accepted_date:
            values["accepted_date"] = fields.Datetime.now()
        self.sudo().write(values)

    def _audit(self, operation, status="success", message=None):
        self.ensure_one()
        # Auditoría ligada al usuario ejecutor: no cambiar env.user aquí.
        self.env["microsoft.audit.log"]._log(
            operation, status=status, tenant_id=self.tenant_id.id,
            item_id=self.entra_user_id or False, item_name=self.email,
            message=message,
        )

    def _fetch_remote(self, directory):
        self.ensure_one()
        data = None
        if self.entra_user_id:
            data = directory.get_user(self.entra_user_id)
            # GraphClient puede devolver el cuerpo de un 404 permitido.
            if not isinstance(data, dict) or not data.get("id"):
                data = None
        if not data:
            data = directory.find_user_by_email(self.email)
        return data if isinstance(data, dict) and data.get("id") else None

    def action_invite(self):
        """Invita solamente si la identidad no existe; reaplica grupos existentes.

        Aísla cada invitado con SAVEPOINT; los errores se mantienen en la ficha
        (no se lanza excepción al final, para evitar rollback de los estados).
        """
        self._check_admin()
        from ..services.directory_service import DirectoryService

        failed = []
        for guest in self:
            try:
                with self.env.cr.savepoint():
                    directory = DirectoryService(guest.tenant_id)
                    remote = guest._fetch_remote(directory)
                    invitation = None
                    if not remote:
                        invitation = directory.invite_guest(guest.email, guest.name, send_message=True)
                        remote = guest._remote_identity(invitation)
                    if not remote.get("id"):
                        raise UserError("Microsoft no devolvió el identificador del invitado.")
                    if invitation and not invitation.get("existingUser"):
                        guest.sudo().write({
                            "invitation_url": invitation.get("inviteRedeemUrl") or False,
                            "invitation_date": fields.Datetime.now(),
                        })
                    guest._apply_remote_user(remote)
                    guest.action_apply_groups()
                    guest._audit("invite", message="Invitado asociado o invitación procesada.")
            except Exception as exc:
                _logger.exception("Error al invitar Microsoft: tenant=%s guest=%s", guest.tenant_id.id, guest.id)
                guest.sudo().write({"state": "error", "last_error": str(exc), "last_sync": fields.Datetime.now()})
                guest._audit("invite", status="error", message=str(exc))
                failed.append(guest.email)
        if failed:
            _logger.warning("Invitaciones Microsoft con errores: %s", ", ".join(failed))
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
                        guest.sudo().write({
                            "state": "error", "last_error": "Usuario no encontrado en Microsoft Entra.",
                            "last_sync": fields.Datetime.now(),
                        })
            except Exception as exc:
                _logger.exception("Error sincronizando invitado Microsoft %s", guest.id)
                guest.sudo().write({"state": "error", "last_error": str(exc), "last_sync": fields.Datetime.now()})
        return True

    def action_apply_groups(self):
        self._check_admin()
        for guest in self:
            if not guest.entra_user_id:
                raise UserError("Primero invite a %s." % guest.email)
            # Estos métodos aplican la composición del grupo, no únicamente
            # el vínculo del invitado; preservar la lógica existente.
            for group in guest.group_ids | guest.owner_group_ids:
                if group.tenant_id != guest.tenant_id:
                    raise ValidationError("El grupo pertenece a otro tenant Microsoft.")
                group.action_apply_members()
        return True

    @api.model
    def _cron_sync_pending_guests(self):
        """Sincroniza invitados pendientes/error sin exigir sesión administrativa."""
        from ..services.directory_service import DirectoryService

        guests = self.sudo().search([
            ("state", "in", ("pending", "error")),
            ("entra_user_id", "!=", False),
            ("active", "=", True),
        ], limit=500)
        for guest in guests:
            try:
                with self.env.cr.savepoint():
                    remote = guest._fetch_remote(DirectoryService(guest.tenant_id))
                    if remote:
                        guest._apply_remote_user(remote)
                    else:
                        guest.sudo().write({
                            "state": "error", "last_error": "Identidad no encontrada en Microsoft Entra.",
                            "last_sync": fields.Datetime.now(),
                        })
            except Exception as exc:
                _logger.exception("Error de cron de invitados Microsoft: id=%s", guest.id)
                guest.sudo().write({"last_error": str(exc), "last_sync": fields.Datetime.now()})
        return True
