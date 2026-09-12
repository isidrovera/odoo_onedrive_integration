# -*- coding: utf-8 -*-
from odoo import api, fields, models
from odoo.exceptions import AccessError, ValidationError


class MicrosoftStorageLocation(models.Model):
    _name = "microsoft.storage.location"
    _description = "Ubicación documental Microsoft"
    _order = "sequence, name"

    sequence = fields.Integer(default=10)
    name = fields.Char(required=True)
    tenant_id = fields.Many2one("onedrive.account", required=True, ondelete="cascade", index=True)
    location_type = fields.Selection([("onedrive", "OneDrive"), ("sharepoint", "SharePoint")], required=True, default="sharepoint")
    drive_id = fields.Char(string="Drive ID", index=True)
    site_id = fields.Many2one("sharepoint.site", ondelete="cascade")
    library_id = fields.Many2one("sharepoint.library", ondelete="cascade")
    web_url = fields.Char()
    reader_user_ids = fields.Many2many("res.users", "microsoft_location_reader_rel", "location_id", "user_id", string="Lectores")
    editor_user_ids = fields.Many2many("res.users", "microsoft_location_editor_rel", "location_id", "user_id", string="Editores")
    manager_user_ids = fields.Many2many("res.users", "microsoft_location_manager_rel", "location_id", "user_id", string="Gestores")
    active = fields.Boolean(default=True)

    _sql_constraints = [
        ("microsoft_location_drive_tenant_unique", "unique(tenant_id, drive_id)", "Esta unidad ya está configurada como ubicación."),
    ]

    @api.constrains("tenant_id", "location_type", "site_id", "library_id")
    def _check_location_relations(self):
        for location in self:
            if location.location_type == "onedrive" and (location.site_id or location.library_id):
                raise ValidationError("Una ubicación OneDrive no puede apuntar a un sitio o biblioteca SharePoint.")
            if location.site_id and location.site_id.tenant_id != location.tenant_id:
                raise ValidationError("El sitio y la ubicación deben pertenecer al mismo tenant.")
            if location.library_id and location.library_id.site_id != location.site_id:
                raise ValidationError("La biblioteca debe pertenecer al sitio seleccionado.")

    def access_level(self, user=None):
        self.ensure_one()
        user = user or self.env.user
        if user.has_group("odoo_onedrive_integration.group_microsoft_admin") or user in self.manager_user_ids:
            return "manager"
        if user in self.editor_user_ids:
            return "editor"
        if user in self.reader_user_ids:
            return "reader"
        return False

    def check_operation(self, operation, user=None):
        level = self.access_level(user)
        allowed = {
            "reader": {"list", "search", "preview", "download", "properties"},
            "editor": {"list", "search", "preview", "download", "properties", "upload", "create_folder", "rename", "move", "copy"},
            "manager": {"list", "search", "preview", "download", "properties", "upload", "create_folder", "rename", "move", "copy", "share", "delete"},
        }
        if not level or operation not in allowed[level]:
            raise AccessError("No tiene permiso para realizar esta operación en la ubicación seleccionada.")
        return level

    @api.model
    def allowed_for_user(self):
        return self.search([])
