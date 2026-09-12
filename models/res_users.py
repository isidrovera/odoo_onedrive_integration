# -*- coding: utf-8 -*-
from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError


class ResUsers(models.Model):
    _inherit = "res.users"

    microsoft_access_enabled = fields.Boolean(string="Acceso Microsoft 365")
    microsoft_tenant_id = fields.Many2one("onedrive.account", string="Tenant Microsoft")
    microsoft_access_role = fields.Selection([
        ("reader", "Lector"), ("editor", "Editor"), ("manager", "Gestor")
    ], string="Rol documental", default="reader")
    microsoft_group_ids = fields.Many2many(
        "microsoft.group", "res_users_microsoft_group_rel", "user_id", "group_id", string="Grupos Microsoft solicitados",
    )
    microsoft_location_ids = fields.Many2many(
        "microsoft.storage.location", "res_users_microsoft_location_rel", "user_id", "location_id", string="Ubicaciones Microsoft",
    )
    microsoft_guest_ids = fields.One2many("microsoft.guest", "user_id", string="Identidades invitadas")
    microsoft_connection_ids = fields.One2many("microsoft.user.connection", "user_id", string="Conexiones personales")

    @api.constrains("microsoft_tenant_id", "microsoft_group_ids", "microsoft_location_ids")
    def _check_microsoft_tenant_assignments(self):
        for user in self:
            if any(group.tenant_id != user.microsoft_tenant_id for group in user.microsoft_group_ids):
                raise ValidationError("Los grupos asignados deben pertenecer al tenant Microsoft del usuario.")
            if any(location.tenant_id != user.microsoft_tenant_id for location in user.microsoft_location_ids):
                raise ValidationError("Las ubicaciones asignadas deben pertenecer al tenant Microsoft del usuario.")

    def _check_microsoft_admin(self):
        if not self.env.user.has_group("odoo_onedrive_integration.group_microsoft_admin"):
            raise AccessError("Solo el administrador Microsoft 365 puede aprovisionar usuarios.")

    def _apply_odoo_document_role(self):
        role_xml = {
            "reader": "odoo_onedrive_integration.group_microsoft_document_user",
            "editor": "odoo_onedrive_integration.group_microsoft_document_editor",
            "manager": "odoo_onedrive_integration.group_microsoft_document_manager",
        }
        document_groups = self.env["res.groups"].browse([self.env.ref(xmlid).id for xmlid in role_xml.values()])
        for user in self:
            selected = self.env.ref(role_xml[user.microsoft_access_role or "reader"])
            user.sudo().write({"group_ids": [(3, group.id) for group in document_groups] + [(4, selected.id)]})

    def action_provision_microsoft(self):
        self._check_microsoft_admin()
        Guest = self.env["microsoft.guest"].sudo()
        Connection = self.env["microsoft.user.connection"].sudo()
        for user in self:
            if not user.microsoft_access_enabled:
                continue
            if not user.microsoft_tenant_id:
                raise UserError(f"Seleccione el tenant Microsoft para {user.name}.")
            if not user.email:
                raise UserError(f"El usuario {user.name} no tiene correo.")
            guest = Guest.search([("tenant_id", "=", user.microsoft_tenant_id.id), ("email", "=ilike", user.email)], limit=1)
            values = {"user_id": user.id, "display_name": user.name, "email": user.email}
            if guest:
                guest.write(values)
            else:
                values["tenant_id"] = user.microsoft_tenant_id.id
                guest = Guest.create(values)
            guest.write({"group_ids": [(6, 0, user.microsoft_group_ids.ids)]})
            guest.with_user(self.env.user).action_invite()
            connection = Connection.search([("tenant_id", "=", user.microsoft_tenant_id.id), ("user_id", "=", user.id)], limit=1)
            if not connection:
                Connection.create({"tenant_id": user.microsoft_tenant_id.id, "user_id": user.id, "email": user.email})
            user._apply_odoo_document_role()
            for location in user.microsoft_location_ids:
                location.sudo().write({
                    "reader_user_ids": [(3, user.id)], "editor_user_ids": [(3, user.id)], "manager_user_ids": [(3, user.id)],
                })
                target = {"reader": "reader_user_ids", "editor": "editor_user_ids", "manager": "manager_user_ids"}[user.microsoft_access_role]
                location.sudo().write({target: [(4, user.id)]})
        return {
            "type": "ir.actions.client", "tag": "display_notification",
            "params": {"title": "Microsoft 365", "message": "Usuarios aprovisionados e invitaciones procesadas.", "type": "success"},
        }

    def action_connect_microsoft(self):
        self.ensure_one()
        if self != self.env.user:
            raise AccessError("Cada usuario debe conectar personalmente su identidad Microsoft.")
        connection = self.microsoft_connection_ids.filtered(lambda c: c.tenant_id == self.microsoft_tenant_id)[:1]
        if not connection:
            if not self.microsoft_tenant_id or not self.email:
                raise UserError("Falta tenant o correo Microsoft.")
            connection = self.env["microsoft.user.connection"].sudo().create({
                "tenant_id": self.microsoft_tenant_id.id, "user_id": self.id, "email": self.email,
            })
        return connection.with_user(self.env.user).action_connect()
