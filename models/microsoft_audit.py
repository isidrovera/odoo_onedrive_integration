# -*- coding: utf-8 -*-
from odoo import api, fields, models


class MicrosoftAuditLog(models.Model):
    _name = "microsoft.audit.log"
    _description = "Auditoría Microsoft 365"
    _order = "create_date desc, id desc"

    user_id = fields.Many2one("res.users", required=True, index=True, ondelete="restrict")
    tenant_id = fields.Many2one("onedrive.account", index=True, ondelete="set null")
    location_id = fields.Many2one("microsoft.storage.location", index=True, ondelete="set null")
    operation = fields.Selection([
        ("list", "Listar"), ("search", "Buscar"), ("upload", "Subir"),
        ("create_folder", "Crear carpeta"), ("rename", "Renombrar"),
        ("move", "Mover"), ("copy", "Copiar"), ("download", "Descargar"),
        ("share", "Compartir"), ("delete", "Enviar a papelera"),
        ("invite", "Invitar usuario"), ("group_add", "Agregar a grupo"),
        ("group_remove", "Retirar de grupo"), ("site_create", "Crear sitio"),
        ("sync", "Sincronizar"),
    ], required=True, index=True)
    item_id = fields.Char(index=True)
    item_name = fields.Char()
    status = fields.Selection([("success", "Correcto"), ("error", "Error")], required=True, default="success", index=True)
    message = fields.Text()
    microsoft_request_id = fields.Char(string="Request ID Microsoft")

    def init(self):
        self.env.cr.execute("CREATE INDEX IF NOT EXISTS microsoft_audit_create_date_idx ON microsoft_audit_log (create_date DESC)")

    @api.model
    def _log(self, operation, status="success", **values):
        """Registra al usuario Odoo que ejecutó la operación, aun si el alta usa sudo."""
        values.update({"user_id": self.env.user.id, "operation": operation, "status": status})
        return self.sudo().create(values)
