# -*- coding: utf-8 -*-
"""Ubicaciones documentales y autorización de operaciones Microsoft 365.

La interfaz OWL puede ocultar acciones, pero la autorización efectiva se realiza
siempre en el servidor mediante :meth:`check_operation`.
"""
import logging

from odoo import api, fields, models
from odoo.exceptions import AccessError, ValidationError

_logger = logging.getLogger(__name__)


class MicrosoftStorageLocation(models.Model):
    _name = "microsoft.storage.location"
    _description = "Ubicación documental Microsoft"
    _order = "sequence, name, id"

    sequence = fields.Integer(default=10)
    name = fields.Char(required=True)
    tenant_id = fields.Many2one(
        "onedrive.account", required=True, ondelete="cascade", index=True,
    )
    location_type = fields.Selection(
        [("onedrive", "OneDrive"), ("sharepoint", "SharePoint")],
        required=True, default="sharepoint",
    )
    drive_id = fields.Char(string="Drive ID", index=True)
    site_id = fields.Many2one("sharepoint.site", ondelete="cascade")
    library_id = fields.Many2one("sharepoint.library", ondelete="cascade")
    web_url = fields.Char()
    reader_user_ids = fields.Many2many(
        "res.users", "microsoft_location_reader_rel", "location_id", "user_id",
        string="Lectores",
    )
    editor_user_ids = fields.Many2many(
        "res.users", "microsoft_location_editor_rel", "location_id", "user_id",
        string="Editores",
    )
    manager_user_ids = fields.Many2many(
        "res.users", "microsoft_location_manager_rel", "location_id", "user_id",
        string="Gestores",
    )
    active = fields.Boolean(default=True)

    _microsoft_location_drive_tenant_unique = models.Constraint(
        "UNIQUE(tenant_id, drive_id)",
        "Esta unidad ya está configurada como ubicación.",
    )

    # Las claves de las operaciones originales se mantienen para no romper
    # /microsoft/* ni las llamadas de OWL. Las adicionales se usarán en los
    # controladores de versiones, restauración y permisos que se incorporen.
    _READER_OPERATIONS = frozenset({
        "list", "search", "preview", "download", "properties", "versions",
        "list_versions", "version_download", "download_version",
        "thumbnail", "recent", "favorites", "activity",
    })
    _EDITOR_OPERATIONS = _READER_OPERATIONS | frozenset({
        "upload", "create_folder", "rename", "move", "copy",
    })
    _MANAGER_OPERATIONS = _EDITOR_OPERATIONS | frozenset({
        "share", "delete", "restore_version", "version_restore",
        "recycle_bin", "list_recycle_bin", "restore_deleted",
        "restore_item", "delete_permission", "remove_permission",
        "permissions", "list_permissions", "invite", "manage_permissions",
    })

    @api.constrains("tenant_id", "location_type", "site_id", "library_id", "drive_id")
    def _check_location_relations(self):
        for location in self:
            if location.location_type == "onedrive" and (location.site_id or location.library_id):
                raise ValidationError(
                    "Una ubicación OneDrive no puede apuntar a un sitio o biblioteca SharePoint."
                )
            if location.site_id and location.site_id.tenant_id != location.tenant_id:
                raise ValidationError(
                    "El sitio y la ubicación deben pertenecer al mismo tenant."
                )
            if location.library_id:
                if location.location_type != "sharepoint":
                    raise ValidationError("Una biblioteca SharePoint requiere una ubicación SharePoint.")
                if not location.site_id or location.library_id.site_id != location.site_id:
                    raise ValidationError(
                        "La biblioteca debe pertenecer al sitio seleccionado."
                    )
                if (location.drive_id and location.library_id.drive_id
                        and location.drive_id != location.library_id.drive_id):
                    raise ValidationError(
                        "El Drive ID no coincide con el de la biblioteca SharePoint."
                    )

    @api.constrains("reader_user_ids", "editor_user_ids", "manager_user_ids")
    def _check_exclusive_roles(self):
        """Evita asignaciones contradictorias dentro de la misma ubicación."""
        for location in self:
            readers = set(location.reader_user_ids.ids)
            editors = set(location.editor_user_ids.ids)
            managers = set(location.manager_user_ids.ids)
            if readers & editors or readers & managers or editors & managers:
                raise ValidationError(
                    "Un usuario solo puede tener un rol (Lector, Editor o Gestor) por ubicación."
                )

    def _resolve_user(self, user=None):
        """Resolver un usuario sin elevar permisos ni confiar en IDs arbitrarios."""
        if user is None:
            return self.env.user
        if getattr(user, "_name", None) != "res.users":
            raise AccessError("El usuario Microsoft proporcionado no es válido.")
        user.ensure_one()
        return user

    def access_level(self, user=None):
        """Rol real del usuario; `False` si no tiene acceso a esta ubicación."""
        self.ensure_one()
        user = self._resolve_user(user)
        if not self.active or not user.active:
            return False

        # Los administradores Microsoft gestionan cualquier ubicación activa.
        if user.has_group("odoo_onedrive_integration.group_microsoft_admin"):
            return "manager"

        # Para el resto, el permiso requiere pertenencia explícita. Un usuario
        # con rol global de Editor/Gestor NO obtiene acceso a otras bibliotecas.
        if user.id in self.manager_user_ids.ids:
            return "manager"
        if user.id in self.editor_user_ids.ids:
            return "editor"
        if user.id in self.reader_user_ids.ids:
            return "reader"
        return False

    def check_operation(self, operation, user=None):
        """Control único y obligatorio para todos los endpoints documentales.

        Devuelve el rol autorizado; lanza AccessError si la operación está
        prohibida. No depende de botones ocultos ni de reglas con sudo().
        """
        self.ensure_one()
        user = self._resolve_user(user)
        level = self.access_level(user)
        allowed = {
            "reader": self._READER_OPERATIONS,
            "editor": self._EDITOR_OPERATIONS,
            "manager": self._MANAGER_OPERATIONS,
        }
        operation = (operation or "").strip() if isinstance(operation, str) else ""
        if not level or operation not in allowed[level]:
            _logger.warning(
                "[MICROSOFT][ACCESS DENIED] user=%s location=%s operation=%s role=%s",
                user.id, self.id, operation, level or "none",
            )
            raise AccessError(
                "No tiene permiso para realizar esta operación en la ubicación seleccionada."
            )
        return level

    @api.model
    def allowed_for_user(self, user=None):
        """Solo ubicaciones activas asignadas al usuario autenticado.

        Compatible con la firma original sin argumentos. Para consultar accesos
        ajenos es necesario ser administrador Microsoft; los endpoints de usuario
        corriente nunca reciben información de otras bibliotecas.
        """
        current = self.env.user
        target = self._resolve_user(user)
        is_admin = current.has_group("odoo_onedrive_integration.group_microsoft_admin")
        if target.id != current.id and not is_admin:
            raise AccessError("No puede consultar ubicaciones de otros usuarios.")

        if is_admin and target.id == current.id:
            # Mantener la visión global del administrador, pero no incluir
            # ubicaciones archivadas en el explorador.
            return self.search([("active", "=", True)])

        domain = [
            ("active", "=", True),
            "|", "|",
            ("reader_user_ids", "in", [target.id]),
            ("editor_user_ids", "in", [target.id]),
            ("manager_user_ids", "in", [target.id]),
        ]
        # El sudo se aplica solamente a la búsqueda; el dominio delimita al
        # usuario. No concede privilegios sobre archivos ni cambia las reglas
        # de acceso de las operaciones posteriores.
        return self.sudo().search(domain).with_user(current)
