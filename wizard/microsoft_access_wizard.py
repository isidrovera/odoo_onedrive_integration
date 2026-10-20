# -*- coding: utf-8 -*-
"""Asistente de aprovisionamiento de accesos Microsoft 365 (Odoo 19).

Usa exclusivamente los modelos res.users, microsoft.group y
microsoft.storage.location existentes. El aprovisionamiento remoto lo realiza
res.users.action_provision_microsoft(), evitando duplicar esa lógica.
"""
import logging

from odoo import api, fields, models, _
from odoo.exceptions import AccessError, UserError, ValidationError

_logger = logging.getLogger(__name__)
_ADMIN_GROUP = "odoo_onedrive_integration.group_microsoft_admin"


class MicrosoftAccessWizard(models.TransientModel):
    _name = "microsoft.access.wizard"
    _description = "Aprovisionar accesos Microsoft 365"

    tenant_id = fields.Many2one(
        "onedrive.account", string="Tenant Microsoft", required=True,
    )
    user_ids = fields.Many2many(
        "res.users", string="Usuarios", required=True,
        domain=[("share", "=", False)],
    )
    access_role = fields.Selection(
        [("reader", "Lector"), ("editor", "Editor"), ("manager", "Gestor")],
        string="Rol documental", default="reader", required=True,
    )
    group_ids = fields.Many2many(
        "microsoft.group", string="Grupos Microsoft",
    )
    location_ids = fields.Many2many(
        "microsoft.storage.location", string="Ubicaciones documentales",
    )
    apply_mode = fields.Selection(
        [("replace", "Reemplazar asignaciones"), ("add", "Agregar asignaciones")],
        string="Modo de aplicación", required=True, default="replace",
        help=(
            "Reemplazar utiliza exactamente los grupos y ubicaciones indicados; "
            "Agregar conserva los existentes que pertenezcan al mismo tenant. "
            "Los cambios de grupos remotos se procesan mediante el aprovisionamiento."
        ),
    )

    def _check_admin(self):
        if not self.env.user.has_group(_ADMIN_GROUP):
            raise AccessError(_("Solo un administrador Microsoft 365 puede aprovisionar accesos."))

    @api.constrains("tenant_id", "group_ids", "location_ids")
    def _check_tenant_consistency(self):
        for wizard in self:
            if not wizard.tenant_id:
                continue
            if any(group.tenant_id != wizard.tenant_id for group in wizard.group_ids):
                raise ValidationError(_("Todos los grupos seleccionados deben pertenecer al tenant elegido."))
            if any(location.tenant_id != wizard.tenant_id for location in wizard.location_ids):
                raise ValidationError(_("Todas las ubicaciones deben pertenecer al tenant elegido."))

    @api.onchange("tenant_id")
    def _onchange_tenant_id(self):
        """Limpia asignaciones de otro tenant al cambiar de empresa Microsoft."""
        if self.tenant_id:
            self.group_ids = self.group_ids.filtered(
                lambda group: group.tenant_id == self.tenant_id
            )
            self.location_ids = self.location_ids.filtered(
                lambda location: location.tenant_id == self.tenant_id
            )
        else:
            self.group_ids = [(5, 0, 0)]
            self.location_ids = [(5, 0, 0)]

    def _validate_before_apply(self):
        """Valida todo el lote antes de realizar escrituras o llamadas a Graph."""
        self.ensure_one()
        self._check_admin()
        self._check_tenant_consistency()
        if not self.tenant_id:
            raise UserError(_("Seleccione un tenant Microsoft."))
        if not self.user_ids:
            raise UserError(_("Seleccione al menos un usuario."))
        if self.access_role not in ("reader", "editor", "manager"):
            raise ValidationError(_("El rol documental seleccionado no es válido."))
        if self.apply_mode not in ("replace", "add"):
            raise ValidationError(_("El modo de aplicación no es válido."))

        for user in self.user_ids:
            if user.share:
                raise UserError(_("El usuario %s no es un usuario interno de Odoo.") % user.display_name)
            if not user.active:
                raise UserError(_("El usuario %s está archivado.") % user.display_name)
            if not (user.email or "").strip():
                raise UserError(_("El usuario %s no tiene correo electrónico.") % user.display_name)
            if self.apply_mode == "add" and user.microsoft_access_enabled:
                if user.microsoft_tenant_id and user.microsoft_tenant_id != self.tenant_id:
                    raise ValidationError(_(
                        "No se pueden agregar accesos al usuario %s porque pertenece "
                        "a otro tenant. Use Reemplazar asignaciones si desea cambiarlo."
                    ) % user.display_name)

    def _assignment_values_for(self, user):
        """Calcula asignaciones sin alterar records existentes."""
        self.ensure_one()
        if self.apply_mode == "add" and user.microsoft_tenant_id == self.tenant_id:
            groups = user.microsoft_group_ids | self.group_ids
            locations = user.microsoft_location_ids | self.location_ids
        else:
            groups = self.group_ids
            locations = self.location_ids
        return {
            "microsoft_access_enabled": True,
            "microsoft_tenant_id": self.tenant_id.id,
            "microsoft_access_role": self.access_role,
            "microsoft_group_ids": [(6, 0, groups.ids)],
            "microsoft_location_ids": [(6, 0, locations.ids)],
        }

    def action_apply(self):
        self.ensure_one()
        self._validate_before_apply()
        # Aplicamos asignaciones de todos los usuarios tras validar el lote.
        # action_provision_microsoft gestiona las invitaciones, las conexiones,
        # los grupos y la sincronización de roles/ubicaciones en Odoo.
        for user in self.user_ids:
            user.sudo().write(self._assignment_values_for(user))

        _logger.info(
            "[MICROSOFT][PROVISION] Solicitud de aprovisionamiento "
            "wizard=%s tenant=%s mode=%s role=%s usuarios=%s grupos=%s ubicaciones=%s",
            self.id, self.tenant_id.id, self.apply_mode, self.access_role,
            self.user_ids.ids, self.group_ids.ids, self.location_ids.ids,
        )
        return self.user_ids.with_user(self.env.user).action_provision_microsoft()
