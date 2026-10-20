# -*- coding: utf-8 -*-
"""Administración de grupos Microsoft Entra desde Odoo 19.

Sincronizar no modifica por sí solo membresías remotas. La eliminación
remota es explícita y las revocaciones de miembros requieren modo autoritativo.
"""
import logging
import re

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

_logger = logging.getLogger(__name__)
_ADMIN = "odoo_onedrive_integration.group_microsoft_admin"
_ALIAS = re.compile(r"^[A-Za-z0-9._-]+$")


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
    visibility = fields.Selection([
        ("private", "Privado"), ("public", "Público"),
    ], default="private", required=True)
    member_ids = fields.Many2many(
        "microsoft.guest", "microsoft_group_guest_rel", "group_id", "guest_id", string="Miembros",
    )
    owner_ids = fields.Many2many(
        "microsoft.guest", "microsoft_group_owner_guest_rel", "group_id", "guest_id", string="Propietarios",
    )
    authoritative_membership = fields.Boolean(
        string="Odoo controla toda la membresía",
        help="Al aplicar membresía, también se retiran los miembros remotos no seleccionados. "
             "No se retiran propietarios automáticamente.",
    )
    member_count = fields.Integer(compute="_compute_counts")
    owner_count = fields.Integer(compute="_compute_counts")
    last_sync = fields.Datetime(readonly=True)
    last_error = fields.Text(readonly=True)
    active = fields.Boolean(default=True)

    _microsoft_group_graph_tenant_unique = models.Constraint(
        "UNIQUE(tenant_id, graph_id)", "El grupo Microsoft ya está sincronizado.",
    )

    @api.depends("member_ids", "owner_ids")
    def _compute_counts(self):
        for group in self:
            group.member_count = len(group.member_ids)
            group.owner_count = len(group.owner_ids)

    @api.constrains("mail_nickname")
    def _check_alias(self):
        for group in self:
            if not _ALIAS.fullmatch(group.mail_nickname or ""):
                raise ValidationError(
                    "El alias solo puede contener letras, números, punto, guion y guion bajo."
                )

    @api.constrains("tenant_id", "member_ids", "owner_ids")
    def _check_guest_tenants(self):
        for group in self:
            if any(guest.tenant_id != group.tenant_id for guest in group.member_ids | group.owner_ids):
                raise ValidationError(
                    "Todos los miembros y propietarios deben pertenecer al mismo tenant del grupo."
                )

    def _check_admin(self):
        if not self.env.user.has_group(_ADMIN):
            raise AccessError("Solo el administrador Microsoft 365 puede administrar grupos.")

    def _validate_remote(self, require_existing=True):
        self.ensure_one()
        if not self.tenant_id:
            raise UserError("Seleccione el tenant Microsoft del grupo.")
        if self.group_type == "readonly":
            raise UserError("Este grupo está configurado como solo lectura.")
        if require_existing and not self.graph_id:
            raise UserError("Primero registre este grupo en Microsoft.")

    def _audit(self, operation, item_id=False, item_name=False, message=False, status="success"):
        self.ensure_one()
        self.env["microsoft.audit.log"]._log(
            operation, status=status, tenant_id=self.tenant_id.id,
            item_id=item_id or False, item_name=item_name or self.name,
            message=message or False,
        )

    def action_create_remote(self):
        self._check_admin()
        from ..services.directory_service import DirectoryService

        for group in self:
            group._validate_remote(require_existing=False)
            if group.graph_id:
                raise UserError("El grupo %s ya existe en Microsoft." % group.name)
            if group.group_type == "microsoft365" and not group.owner_ids:
                raise UserError("El grupo Microsoft 365 requiere al menos un propietario registrado.")
            if any(not guest.entra_user_id for guest in group.member_ids | group.owner_ids):
                raise UserError("Todos los miembros y propietarios deben tener una identidad Microsoft.")
            try:
                data = DirectoryService(group.tenant_id).create_group(group)
                graph_id = data.get("id") if isinstance(data, dict) else False
                if not graph_id:
                    raise UserError("Microsoft no devolvió el identificador del grupo creado.")
                group.sudo().write({
                    "graph_id": graph_id,
                    "mail": data.get("mail") or False,
                    "last_sync": fields.Datetime.now(),
                    "last_error": False,
                })
                # Graph puede haber añadido propietarios/miembros en la creación;
                # la siguiente operación es idempotente para las membresías existentes.
                group.action_apply_members()
            except Exception as exc:
                _logger.exception("Error creando grupo Microsoft group_id=%s", group.id)
                group.sudo().write({"last_error": str(exc)[:2000]})
                raise
        return True

    def action_update_remote(self):
        self._check_admin()
        from ..services.directory_service import DirectoryService

        for group in self:
            group._validate_remote()
            try:
                DirectoryService(group.tenant_id).update_group(group)
                group.sudo().write({"last_sync": fields.Datetime.now(), "last_error": False})
            except Exception as exc:
                _logger.exception("Error actualizando grupo Microsoft group_id=%s", group.id)
                group.sudo().write({"last_error": str(exc)[:2000]})
                raise
        return True

    def action_apply_members(self):
        """Añade miembros/propietarios. Solo revoca miembros en modo autoritativo.

        Los propietarios remotos no se eliminan aquí para evitar dejar sin
        administración a un grupo por una selección incompleta en Odoo.
        """
        self._check_admin()
        from ..services.directory_service import DirectoryService

        for group in self:
            group._validate_remote()
            if any(not guest.entra_user_id for guest in group.member_ids | group.owner_ids):
                raise UserError("Invite y sincronice los usuarios antes de aplicar la membresía.")
            service = DirectoryService(group.tenant_id)
            try:
                remote_members = {
                    str(x["id"]) for x in service.list_group_members(group.graph_id)
                    if isinstance(x, dict) and x.get("id")
                }
                remote_owners = {
                    str(x["id"]) for x in service.list_group_members(group.graph_id, owner=True)
                    if isinstance(x, dict) and x.get("id")
                }
                desired_members = {str(g.entra_user_id) for g in group.member_ids}
                desired_owners = {str(g.entra_user_id) for g in group.owner_ids}

                for user_id in sorted(desired_members - remote_members):
                    service.add_group_member(group.graph_id, user_id)
                    group._audit("group_add", item_id=user_id)
                for user_id in sorted(desired_owners - remote_owners):
                    service.add_group_member(group.graph_id, user_id, owner=True)
                    group._audit("group_add", item_id=user_id, item_name="%s (propietario)" % group.name)

                if group.authoritative_membership:
                    for user_id in sorted(remote_members - desired_members - desired_owners):
                        service.remove_group_member(group.graph_id, user_id)
                        group._audit("group_remove", item_id=user_id)

                group.sudo().write({"last_sync": fields.Datetime.now(), "last_error": False})
            except Exception as exc:
                _logger.exception("Error aplicando membresía de grupo group_id=%s", group.id)
                group.sudo().write({"last_error": str(exc)[:2000]})
                raise
        return True

    @staticmethod
    def _remote_email(data):
        """Resolver el correo original sin confundir UPN de invitados #EXT#."""
        candidates = [data.get("mail")]
        candidates.extend(data.get("otherMails") or [])
        candidates.append(data.get("userPrincipalName"))
        for item in candidates:
            value = str(item or "").strip().lower()
            if "@" in value and "#ext#" not in value and " " not in value:
                return value
        return False

    def action_sync_members(self):
        """Leer membresías remotas y actualizar enlaces Odoo sin escribir en Graph."""
        self._check_admin()
        from ..services.directory_service import DirectoryService

        Guest = self.env["microsoft.guest"].sudo()
        for group in self:
            if not group.graph_id:
                continue
            service = DirectoryService(group.tenant_id)
            try:
                members = service.list_group_members(group.graph_id)
                owners = service.list_group_members(group.graph_id, owner=True)
                member_records = Guest.browse()
                owner_records = Guest.browse()
                cache = {}

                for entries, is_owner in ((members, False), (owners, True)):
                    for data in entries:
                        if not isinstance(data, dict) or not data.get("id"):
                            continue
                        remote_id = str(data["id"])
                        guest = cache.get(remote_id)
                        if guest is None:
                            guest = Guest.search([
                                ("tenant_id", "=", group.tenant_id.id),
                                ("entra_user_id", "=", remote_id),
                            ], limit=1)
                            if not guest:
                                email = self._remote_email(data)
                                if not email:
                                    _logger.info(
                                        "Grupo %s: omitiendo objeto sin correo verificable (%s)",
                                        group.id, remote_id,
                                    )
                                    continue
                                guest = Guest.search([
                                    ("tenant_id", "=", group.tenant_id.id),
                                    ("email", "=ilike", email),
                                ], limit=1)
                                if guest and guest.entra_user_id and guest.entra_user_id != remote_id:
                                    _logger.warning(
                                        "Grupo %s: correo con otra identidad, omitido (%s)", group.id, remote_id
                                    )
                                    continue
                                if guest:
                                    guest.write({
                                        "entra_user_id": remote_id,
                                        "user_principal_name": data.get("userPrincipalName") or False,
                                    })
                                else:
                                    guest = Guest.create({
                                        "tenant_id": group.tenant_id.id,
                                        "name": data.get("displayName") or email,
                                        "email": email,
                                        "entra_user_id": remote_id,
                                        "user_principal_name": data.get("userPrincipalName") or False,
                                        "state": "pending",
                                    })
                            cache[remote_id] = guest
                        if not guest:
                            continue
                        # Si Graph devolvió datos suficientes, sincronizar el estado.
                        if data.get("userType") or data.get("externalUserState") or "accountEnabled" in data:
                            guest._apply_remote_user(data)
                        if is_owner:
                            owner_records |= guest
                        else:
                            member_records |= guest

                group.sudo().write({
                    "member_ids": [(6, 0, member_records.ids)],
                    "owner_ids": [(6, 0, owner_records.ids)],
                    "last_sync": fields.Datetime.now(),
                    "last_error": False,
                })
            except Exception as exc:
                _logger.exception("Error sincronizando miembros group_id=%s", group.id)
                group.sudo().write({"last_error": str(exc)[:2000]})
                raise
        return True

    def action_delete_remote(self):
        """Operación destructiva explícita: nunca se ejecuta al sincronizar."""
        self._check_admin()
        from ..services.directory_service import DirectoryService

        for group in self:
            group._validate_remote()
            try:
                DirectoryService(group.tenant_id).delete_group(group.graph_id)
                group.sudo().write({
                    "graph_id": False, "mail": False, "active": False,
                    "last_sync": fields.Datetime.now(), "last_error": False,
                })
            except Exception as exc:
                _logger.exception("Error eliminando grupo Microsoft group_id=%s", group.id)
                group.sudo().write({"last_error": str(exc)[:2000]})
                raise
        return True
