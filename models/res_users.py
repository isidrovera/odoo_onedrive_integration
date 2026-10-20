# -*- coding: utf-8 -*-
"""Aprovisionamiento de usuarios Odoo y accesos Microsoft 365.

Los roles Odoo controlan el explorador. La pertenencia a grupos Entra y
los permisos remotos de SharePoint son independientes y se sincronizan
mediante los modelos Microsoft del módulo.
"""
import logging
import re

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

_logger = logging.getLogger(__name__)
_ADMIN = "odoo_onedrive_integration.group_microsoft_admin"
_ROLE_GROUPS = {
    "reader": "odoo_onedrive_integration.group_microsoft_document_user",
    "editor": "odoo_onedrive_integration.group_microsoft_document_editor",
    "manager": "odoo_onedrive_integration.group_microsoft_document_manager",
}
_LOCATION_ROLE_FIELDS = {
    "reader": "reader_user_ids",
    "editor": "editor_user_ids",
    "manager": "manager_user_ids",
}
_EMAIL_RE = re.compile(r"^[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+$")


class ResUsers(models.Model):
    _inherit = "res.users"

    microsoft_access_enabled = fields.Boolean(string="Acceso Microsoft 365")
    microsoft_tenant_id = fields.Many2one("onedrive.account", string="Tenant Microsoft")
    microsoft_access_role = fields.Selection(
        [("reader", "Lector"), ("editor", "Editor"), ("manager", "Gestor")],
        string="Rol documental", default="reader",
    )
    microsoft_group_ids = fields.Many2many(
        "microsoft.group", "res_users_microsoft_group_rel", "user_id", "group_id",
        string="Grupos Microsoft solicitados",
    )
    microsoft_location_ids = fields.Many2many(
        "microsoft.storage.location", "res_users_microsoft_location_rel", "user_id", "location_id",
        string="Ubicaciones Microsoft",
    )
    microsoft_guest_ids = fields.One2many(
        "microsoft.guest", "user_id", string="Identidades invitadas",
    )
    microsoft_connection_ids = fields.One2many(
        "microsoft.user.connection", "user_id", string="Conexiones personales",
    )

    @api.constrains("microsoft_tenant_id", "microsoft_group_ids", "microsoft_location_ids", "microsoft_access_enabled")
    def _check_microsoft_tenant_assignments(self):
        for user in self:
            tenant = user.microsoft_tenant_id
            if (user.microsoft_group_ids or user.microsoft_location_ids) and not tenant:
                raise ValidationError("Seleccione un tenant Microsoft antes de asignar grupos o ubicaciones.")
            if any(group.tenant_id != tenant for group in user.microsoft_group_ids):
                raise ValidationError("Los grupos asignados deben pertenecer al tenant Microsoft del usuario.")
            if any(location.tenant_id != tenant for location in user.microsoft_location_ids):
                raise ValidationError("Las ubicaciones asignadas deben pertenecer al tenant Microsoft del usuario.")

    def _check_microsoft_admin(self):
        if not self.env.user.has_group(_ADMIN):
            raise AccessError("Solo el administrador Microsoft 365 puede aprovisionar usuarios.")

    def _apply_odoo_document_role(self):
        """Mantiene el grupo documental compatible con los XML IDs originales.

        La implicación de grupos Editor -> Lector y Gestor -> Editor se
        encarga de los permisos subordinados en Odoo.
        """
        self._check_microsoft_admin()
        groups = {role: self.env.ref(xmlid) for role, xmlid in _ROLE_GROUPS.items()}
        for user in self:
            commands = [(3, group.id) for group in groups.values()]
            if user.microsoft_access_enabled:
                role = user.microsoft_access_role or "reader"
                if role not in groups:
                    raise ValidationError("Rol documental Microsoft desconocido.")
                commands.append((4, groups[role].id))
            user.sudo().write({"group_ids": commands})

    def _sync_document_locations(self):
        """Aplica la selección completa, revocando ubicaciones desasignadas.

        Se usa sudo exclusivamente tras comprobar autorización administrativa.
        No modifica permisos remotos de SharePoint: su sincronización se hace
        explícitamente desde el sitio, evitando revocar permisos ajenos.
        """
        self._check_microsoft_admin()
        Location = self.env["microsoft.storage.location"].sudo()
        for user in self:
            role = user.microsoft_access_role or "reader"
            if role not in _LOCATION_ROLE_FIELDS:
                raise ValidationError("Rol documental Microsoft desconocido.")
            assigned = user.microsoft_location_ids if user.microsoft_access_enabled else Location.browse()
            assigned_ids = set(assigned.ids)
            locations = Location.search([
                "|", "|",
                ("reader_user_ids", "in", user.id),
                ("editor_user_ids", "in", user.id),
                ("manager_user_ids", "in", user.id),
            ]) | assigned
            for location in locations:
                # Primero retirar roles existentes; luego establecer el nuevo.
                vals = {field: [(3, user.id)] for field in _LOCATION_ROLE_FIELDS.values()}
                if location.id in assigned_ids:
                    vals[_LOCATION_ROLE_FIELDS[role]] = [(3, user.id), (4, user.id)]
                location.write(vals)

    def _get_or_create_microsoft_guest(self):
        self.ensure_one()
        Guest = self.env["microsoft.guest"].sudo()
        email = (self.email or "").strip().lower()
        if not _EMAIL_RE.fullmatch(email):
            raise UserError("El usuario %s necesita un correo válido para Microsoft 365." % self.display_name)
        guest = Guest.search([
            ("tenant_id", "=", self.microsoft_tenant_id.id),
            ("email", "=ilike", email),
        ], limit=1)
        if guest and guest.user_id and guest.user_id != self:
            raise ValidationError("El invitado %s ya está vinculado a otro usuario Odoo." % email)
        vals = {"user_id": self.id, "name": self.name, "email": email}
        if guest:
            guest.write(vals)
        else:
            guest = Guest.create(dict(vals, tenant_id=self.microsoft_tenant_id.id))
        guest.write({"group_ids": [(6, 0, self.microsoft_group_ids.ids)]})
        return guest

    def _ensure_microsoft_connection(self):
        self.ensure_one()
        Connection = self.env["microsoft.user.connection"].sudo()
        existing = Connection.search([
            ("tenant_id", "=", self.microsoft_tenant_id.id),
            ("user_id", "=", self.id),
        ], limit=1)
        if not existing:
            Connection.create({
                "tenant_id": self.microsoft_tenant_id.id,
                "user_id": self.id,
                "email": (self.email or "").strip().lower(),
            })
        return True

    def action_provision_microsoft(self):
        """Sincronización Odoo + invitado Entra y grupos solicitados.

        La invitación se procesa mediante MicrosoftGuest.action_invite,
        conservando su lógica de búsqueda, auditoría y membresías.
        """
        self._check_microsoft_admin()
        enabled = self.filtered("microsoft_access_enabled")
        for user in enabled:
            if not user.microsoft_tenant_id:
                raise UserError("Seleccione el tenant Microsoft para %s." % user.display_name)
            if not user.active:
                raise UserError("El usuario %s está archivado." % user.display_name)
            if user.share:
                raise UserError("El aprovisionamiento está reservado a usuarios internos de Odoo.")
            if not user.email:
                raise UserError("El usuario %s no tiene correo electrónico." % user.display_name)
            user._check_microsoft_tenant_assignments()

        for user in enabled:
            guest = user._get_or_create_microsoft_guest()
            user._ensure_microsoft_connection()
            guest.with_user(self.env.user).action_invite()
            _logger.info("Microsoft 365: invitado aprovisionado para usuario Odoo id=%s", user.id)

        # También permite retirar permisos Odoo cuando se deshabilita el acceso.
        self._apply_odoo_document_role()
        self._sync_document_locations()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Microsoft 365",
                "message": "Aprovisionamiento procesado para %s usuario(s)." % len(self),
                "type": "success",
                "sticky": False,
            },
        }

    def action_connect_microsoft(self):
        self.ensure_one()
        if self.id != self.env.user.id:
            raise AccessError("Cada usuario debe conectar personalmente su identidad Microsoft.")
        if not self.microsoft_access_enabled:
            raise AccessError("No tiene habilitado el acceso Microsoft 365.")
        if not self.microsoft_tenant_id or not self.email:
            raise UserError("Falta tenant o correo Microsoft.")
        connection = self.microsoft_connection_ids.filtered(
            lambda record: record.tenant_id == self.microsoft_tenant_id
        )[:1]
        if not connection:
            connection = self.env["microsoft.user.connection"].sudo().create({
                "tenant_id": self.microsoft_tenant_id.id,
                "user_id": self.id,
                "email": self.email.strip().lower(),
            })
        return connection.with_user(self.env.user).action_connect()
