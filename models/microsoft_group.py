# -*- coding: utf-8 -*-
import re

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError


class MicrosoftGroup(models.Model):
    _name = "microsoft.group"
    _description = "Grupo Microsoft 365"
    _order = "name"
    _inherit = ["mail.thread"]

    tenant_id = fields.Many2one("onedrive.account", required=True, ondelete="cascade", index=True)
    graph_id = fields.Char(string="ID Microsoft", copy=False, readonly=True, index=True)
    name = fields.Char(required=True, tracking=True)
    description = fields.Text()
    mail = fields.Char(readonly=True)
    mail_nickname = fields.Char(string="Alias", required=True)
    group_type = fields.Selection([
        ("security", "Grupo de seguridad"),
        ("microsoft365", "Grupo Microsoft 365"),
        ("readonly", "No administrable por Graph"),
    ], default="security", required=True, tracking=True)
    visibility = fields.Selection([("private", "Privado"), ("public", "Público")], default="private", required=True)
    member_ids = fields.Many2many(
        "microsoft.guest", "microsoft_group_guest_rel", "group_id", "guest_id", string="Miembros",
    )
    owner_ids = fields.Many2many(
        "microsoft.guest", "microsoft_group_owner_guest_rel", "group_id", "guest_id", string="Propietarios",
    )
    authoritative_membership = fields.Boolean(
        string="Odoo controla toda la membresía",
        help="Si se activa, Aplicar membresía también retirará de Microsoft los usuarios que ya no estén seleccionados en Odoo.",
    )
    member_count = fields.Integer(compute="_compute_counts")
    owner_count = fields.Integer(compute="_compute_counts")
    last_sync = fields.Datetime(readonly=True)
    last_error = fields.Text(readonly=True)
    active = fields.Boolean(default=True)

    _sql_constraints = [
        ("microsoft_group_graph_tenant_unique", "unique(tenant_id, graph_id)", "El grupo Microsoft ya está sincronizado."),
    ]

    @api.depends("member_ids", "owner_ids")
    def _compute_counts(self):
        for group in self:
            group.member_count = len(group.member_ids)
            group.owner_count = len(group.owner_ids)

    @api.constrains("mail_nickname")
    def _check_alias(self):
        for group in self:
            if not re.fullmatch(r"[A-Za-z0-9._-]+", group.mail_nickname or ""):
                raise ValidationError("El alias solo puede contener letras, números, punto, guion y guion bajo.")

    @api.constrains("tenant_id", "member_ids", "owner_ids")
    def _check_guest_tenants(self):
        for group in self:
            if any(guest.tenant_id != group.tenant_id for guest in group.member_ids | group.owner_ids):
                raise ValidationError("Todos los miembros y propietarios deben pertenecer al mismo tenant del grupo.")

    def _check_admin(self):
        if not self.env.user.has_group("odoo_onedrive_integration.group_microsoft_admin"):
            raise AccessError("Solo el administrador Microsoft 365 puede administrar grupos.")

    def action_create_remote(self):
        self._check_admin()
        from ..services.directory_service import DirectoryService
        for group in self:
            if group.graph_id:
                raise UserError(f"El grupo {group.name} ya existe en Microsoft.")
            if group.group_type == "readonly":
                raise UserError("Este tipo de grupo solo puede sincronizarse en modo lectura.")
            if group.group_type == "microsoft365" and not group.owner_ids:
                raise UserError("Un grupo Microsoft 365 debe tener al menos un propietario invitado y registrado.")
            if any(not guest.entra_user_id for guest in group.owner_ids | group.member_ids):
                raise UserError("Todos los propietarios y miembros deben estar registrados en Microsoft.")
            data = DirectoryService(group.tenant_id).create_group(group)
            group.sudo().write({"graph_id": data.get("id"), "mail": data.get("mail"), "last_sync": fields.Datetime.now()})
            group.action_apply_members()
        return True

    def action_update_remote(self):
        self._check_admin()
        from ..services.directory_service import DirectoryService
        for group in self:
            if not group.graph_id or group.group_type == "readonly":
                continue
            DirectoryService(group.tenant_id).update_group(group)
            group.sudo().write({"last_sync": fields.Datetime.now(), "last_error": False})
        return True

    def action_apply_members(self):
        self._check_admin()
        from ..services.directory_service import DirectoryService
        Audit = self.env["microsoft.audit.log"]
        for group in self:
            if not group.graph_id:
                continue
            service = DirectoryService(group.tenant_id)
            remote_members = {item.get("id") for item in service.list_group_members(group.graph_id)}
            remote_owners = {item.get("id") for item in service.list_group_members(group.graph_id, owner=True)}
            desired_members = {guest.entra_user_id for guest in group.member_ids if guest.entra_user_id}
            desired_owners = {guest.entra_user_id for guest in group.owner_ids if guest.entra_user_id}
            for user_id in desired_members - remote_members:
                service.add_group_member(group.graph_id, user_id)
                Audit._log("group_add", tenant_id=group.tenant_id.id, item_id=user_id, item_name=group.name)
            for user_id in desired_owners - remote_owners:
                service.add_group_member(group.graph_id, user_id, owner=True)
                Audit._log("group_add", tenant_id=group.tenant_id.id, item_id=user_id, item_name=f"{group.name} (propietario)")
            if group.authoritative_membership:
                for user_id in remote_members - desired_members - desired_owners:
                    service.remove_group_member(group.graph_id, user_id)
                    Audit._log("group_remove", tenant_id=group.tenant_id.id, item_id=user_id, item_name=group.name)
            group.sudo().write({"last_sync": fields.Datetime.now(), "last_error": False})
        return True

    def action_sync_members(self):
        self._check_admin()
        from ..services.directory_service import DirectoryService
        Guest = self.env["microsoft.guest"].sudo()
        for group in self:
            if not group.graph_id:
                continue
            service = DirectoryService(group.tenant_id)
            members = service.list_group_members(group.graph_id)
            owners = service.list_group_members(group.graph_id, owner=True)
            member_records = self.env["microsoft.guest"]
            owner_records = self.env["microsoft.guest"]
            for data, owner in [(x, False) for x in members] + [(x, True) for x in owners]:
                email = data.get("mail") or data.get("userPrincipalName")
                if not email:
                    continue
                guest = Guest.search([("tenant_id", "=", group.tenant_id.id), ("entra_user_id", "=", data.get("id"))], limit=1)
                if not guest:
                    guest = Guest.create({
                        "tenant_id": group.tenant_id.id,
                        "display_name": data.get("displayName") or email,
                        "email": email,
                        "entra_user_id": data.get("id"),
                        "user_principal_name": data.get("userPrincipalName"),
                        "state": "accepted",
                    })
                if owner:
                    owner_records |= guest
                else:
                    member_records |= guest
            group.sudo().write({
                "member_ids": [(6, 0, member_records.ids)],
                "owner_ids": [(6, 0, owner_records.ids)],
                "last_sync": fields.Datetime.now(),
            })
        return True

    def action_delete_remote(self):
        self._check_admin()
        from ..services.directory_service import DirectoryService
        for group in self:
            if group.graph_id and group.group_type != "readonly":
                DirectoryService(group.tenant_id).delete_group(group.graph_id)
                group.sudo().write({"graph_id": False, "mail": False, "active": False})
        return True
