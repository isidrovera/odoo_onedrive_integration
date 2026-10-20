# -*- coding: utf-8 -*-
"""Trazabilidad de operaciones realizadas desde Odoo sobre Microsoft 365.

Este registro es una auditoría de acciones del módulo, no sustituye los registros
nativos de Microsoft Purview / Entra / SharePoint. Las entradas se guardan en la
transacción de Odoo: si esta hace rollback, la entrada también puede revertirse.
"""
import logging
import re

from odoo import api, fields, models
from odoo.exceptions import AccessError, ValidationError

_logger = logging.getLogger(__name__)


class MicrosoftAuditLog(models.Model):
    _name = "microsoft.audit.log"
    _description = "Auditoría Microsoft 365"
    _order = "create_date desc, id desc"

    user_id = fields.Many2one(
        "res.users", string="Usuario Odoo", required=True, index=True,
        ondelete="restrict", readonly=True,
    )
    tenant_id = fields.Many2one(
        "onedrive.account", string="Tenant", index=True,
        ondelete="set null", readonly=True,
    )
    location_id = fields.Many2one(
        "microsoft.storage.location", string="Ubicación", index=True,
        ondelete="set null", readonly=True,
    )
    operation = fields.Selection(
        [
            ("list", "Listar"),
            ("search", "Buscar"),
            ("upload", "Subir"),
            ("create_folder", "Crear carpeta"),
            ("rename", "Renombrar"),
            ("move", "Mover"),
            ("copy", "Copiar"),
            ("download", "Descargar"),
            ("preview", "Vista previa"),
            ("properties", "Consultar propiedades"),
            ("share", "Compartir"),
            ("delete", "Enviar a papelera"),
            ("version_list", "Consultar versiones"),
            ("versions", "Consultar versiones (compatibilidad)"),
            ("version_restore", "Restaurar versión"),
            ("restore", "Restaurar"),
            ("recycle_list", "Consultar papelera"),
            ("recycle_restore", "Recuperar de papelera"),
            ("permission_list", "Consultar permisos"),
            ("permission_add", "Conceder permiso"),
            ("permission_remove", "Revocar permiso"),
            ("invite", "Invitar usuario"),
            ("group_add", "Agregar a grupo"),
            ("group_remove", "Retirar de grupo"),
            ("group_create", "Crear grupo"),
            ("group_update", "Actualizar grupo"),
            ("group_sync", "Sincronizar grupos"),
            ("site_create", "Crear sitio"),
            ("library_create", "Crear biblioteca"),
            ("sync", "Sincronizar"),
            ("connect", "Conectar cuenta"),
            ("disconnect", "Desconectar cuenta"),
            ("access_denied", "Acceso denegado"),
        ],
        string="Operación", required=True, index=True, readonly=True,
    )
    item_id = fields.Char(string="ID del elemento", index=True, readonly=True)
    item_name = fields.Char(string="Nombre del elemento", readonly=True)
    status = fields.Selection(
        [("success", "Correcto"), ("error", "Error")],
        string="Resultado", required=True, default="success", index=True,
        readonly=True,
    )
    message = fields.Text(string="Detalle", readonly=True)
    microsoft_request_id = fields.Char(
        string="Request ID Microsoft", index=True, readonly=True,
    )

    # Previene filtraciones accidentales de tokens si se registra una excepción
    # HTTP completa. No sustituye la obligación de no registrar credenciales.
    _SECRET_PATTERNS = (
        re.compile(r"(?i)(bearer\s+)\S+"),
        re.compile(r"(?i)((?:access_token|refresh_token|client_secret|id_token)\s*[:=]\s*)[^\s&,;]+"),
    )

    def init(self):
        self.env.cr.execute(
            "CREATE INDEX IF NOT EXISTS microsoft_audit_create_date_idx "
            "ON microsoft_audit_log (create_date DESC)"
        )
        self.env.cr.execute(
            "CREATE INDEX IF NOT EXISTS microsoft_audit_location_date_idx "
            "ON microsoft_audit_log (location_id, create_date DESC)"
        )

    @api.model
    def _clean_message(self, message):
        if not message:
            return False
        value = str(message)
        for pattern in self._SECRET_PATTERNS:
            value = pattern.sub(lambda m: m.group(1) + "[OCULTO]", value)
        return value[:8000]

    @api.model
    def _log(self, operation, status="success", **values):
        """Crea una entrada con la identidad del usuario real de la llamada.

        Mantiene la API usada por controladores y modelos existentes. Nunca
        acepta `user_id` desde `values`, evitando atribuciones arbitrarias.
        El `sudo()` permite guardar el evento a usuarios sin ACL de creación
        sobre auditorías, pero solo se debe llamar desde código de confianza.
        """
        operation = str(operation or "").strip()
        valid_operations = dict(self._fields["operation"].selection)
        if operation not in valid_operations:
            _logger.warning("[MICROSOFT][AUDIT] Operación no registrada: %r", operation)
            raise ValidationError("Operación de auditoría Microsoft desconocida: %s" % operation)
        if status not in ("success", "error"):
            raise ValidationError("Resultado de auditoría Microsoft desconocido.")

        allowed_fields = {
            "tenant_id", "location_id", "item_id", "item_name",
            "message", "microsoft_request_id",
        }
        unexpected = set(values) - allowed_fields
        if unexpected:
            raise ValidationError(
                "Campos de auditoría Microsoft no admitidos: %s" % ", ".join(sorted(unexpected))
            )

        record_values = {k: v for k, v in values.items() if k in allowed_fields}
        record_values["user_id"] = self.env.user.id
        record_values["operation"] = operation
        record_values["status"] = status
        if "message" in record_values:
            record_values["message"] = self._clean_message(record_values["message"])

        # La entrada participa en la misma transacción; no hacer commit aquí.
        return self.sudo().create(record_values)

    @api.model
    def _log_error(self, operation, error, **values):
        """Atajo para errores de procesos internos; conserva `_log`."""
        values["message"] = self._clean_message(error)
        return self._log(operation, status="error", **values)

    def write(self, values):
        # Inmutabilidad de evidencias: permitir únicamente al superusuario
        # técnico corregir entradas si fuera estrictamente necesario.
        if not self.env.is_superuser():
            raise AccessError("Los registros de auditoría Microsoft no se pueden modificar.")
        return super().write(values)

    def unlink(self):
        if not self.env.is_superuser():
            raise AccessError("Los registros de auditoría Microsoft no se pueden eliminar.")
        return super().unlink()
