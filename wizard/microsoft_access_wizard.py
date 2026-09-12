# -*- coding: utf-8 -*-
from odoo import fields, models
from odoo.exceptions import UserError


class MicrosoftAccessWizard(models.TransientModel):
    _name = "microsoft.access.wizard"
    _description = "Aprovisionar accesos Microsoft 365"

    tenant_id = fields.Many2one("onedrive.account", required=True)
    user_ids = fields.Many2many("res.users", string="Usuarios", required=True, domain=[("share", "=", False)])
    access_role = fields.Selection([("reader", "Lector"), ("editor", "Editor"), ("manager", "Gestor")], default="reader", required=True)
    group_ids = fields.Many2many("microsoft.group", string="Grupos Microsoft")
    location_ids = fields.Many2many("microsoft.storage.location", string="Ubicaciones documentales")

    def action_apply(self):
        self.ensure_one()
        if not self.user_ids:
            raise UserError("Seleccione al menos un usuario.")
        for user in self.user_ids:
            user.sudo().write({
                "microsoft_access_enabled": True,
                "microsoft_tenant_id": self.tenant_id.id,
                "microsoft_access_role": self.access_role,
                "microsoft_group_ids": [(6, 0, self.group_ids.ids)],
                "microsoft_location_ids": [(6, 0, self.location_ids.ids)],
            })
        return self.user_ids.with_user(self.env.user).action_provision_microsoft()
