# -*- coding: utf-8 -*-
"""Sitios y bibliotecas SharePoint para Odoo 19.

Sincroniza metadatos sin revocar accesos. Las concesiones y retiradas
remotas se realizan exclusivamente con la acción administrativa explícita.
"""
import logging
import re

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

_logger = logging.getLogger(__name__)
_ADMIN = "odoo_onedrive_integration.group_microsoft_admin"
_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$")


class SharePointSite(models.Model):
    _name = "sharepoint.site"
    _description = "Sitio SharePoint"
    _order = "name"
    _inherit = ["mail.thread"]

    tenant_id = fields.Many2one("onedrive.account", required=True, ondelete="cascade", index=True)
    graph_id = fields.Char(string="Site ID", readonly=True, copy=False, index=True)
    name = fields.Char(required=True, tracking=True)
    description = fields.Text()
    site_type = fields.Selection([
        ("existing", "Sitio existente"),
        ("team_group", "Equipo con Microsoft 365"),
        ("team_nogroup", "Equipo sin grupo"),
        ("communication", "Comunicación"),
    ], default="team_group", required=True)
    primary_group_id = fields.Many2one("microsoft.group", string="Grupo Microsoft 365 principal")
    url_slug = fields.Char(string="Ruta del sitio", help="Ejemplo: servicio-tecnico")
    web_url = fields.Char(readonly=True)
    owner_email = fields.Char()
    locale_id = fields.Integer(string="LCID", default=3082,
                               help="3082 corresponde a español de España; puede ajustarse según el tenant.")
    status = fields.Selection([
        ("draft", "Borrador"), ("provisioning", "Creando"),
        ("ready", "Disponible"), ("error", "Error"),
    ], default="draft", required=True, tracking=True)
    reader_group_ids = fields.Many2many(
        "microsoft.group", "sharepoint_site_reader_group_rel", "site_id", "group_id",
        string="Grupos lectores",
    )
    editor_group_ids = fields.Many2many(
        "microsoft.group", "sharepoint_site_editor_group_rel", "site_id", "group_id",
        string="Grupos editores",
    )
    applied_reader_group_ids = fields.Many2many(
        "microsoft.group", "sharepoint_site_applied_reader_rel", "site_id", "group_id",
        string="Grupos lectores aplicados", copy=False, readonly=True,
    )
    applied_editor_group_ids = fields.Many2many(
        "microsoft.group", "sharepoint_site_applied_editor_rel", "site_id", "group_id",
        string="Grupos editores aplicados", copy=False, readonly=True,
    )
    library_ids = fields.One2many("sharepoint.library", "site_id", string="Bibliotecas")
    last_sync = fields.Datetime(readonly=True)
    last_error = fields.Text(readonly=True)
    active = fields.Boolean(default=True)

    _sharepoint_site_graph_tenant_unique = models.Constraint(
        "UNIQUE(tenant_id, graph_id)", "El sitio ya está sincronizado.",
    )

    @api.constrains("tenant_id", "primary_group_id", "reader_group_ids", "editor_group_ids")
    def _check_group_configuration(self):
        for site in self:
            groups = site.primary_group_id | site.reader_group_ids | site.editor_group_ids
            if any(group.tenant_id != site.tenant_id for group in groups):
                raise ValidationError("Todos los grupos del sitio deben pertenecer al mismo tenant.")
            if site.reader_group_ids & site.editor_group_ids:
                raise ValidationError("Un grupo no puede ser lector y editor al mismo tiempo.")
            if site.site_type == "team_group" and site.primary_group_id and site.primary_group_id.group_type != "microsoft365":
                raise ValidationError("El grupo principal debe ser un grupo Microsoft 365.")

    @api.constrains("url_slug")
    def _check_url_slug(self):
        for site in self:
            if site.url_slug and not _SLUG.fullmatch(site.url_slug.strip("/")):
                raise ValidationError("La ruta solo admite letras, números, guiones y guiones bajos.")

    def _check_admin(self):
        if not self.env.user.has_group(_ADMIN):
            raise AccessError("Solo el administrador Microsoft 365 puede administrar sitios.")

    def _audit(self, operation, message=False, status="success"):
        self.ensure_one()
        self.env["microsoft.audit.log"]._log(
            operation, tenant_id=self.tenant_id.id, status=status,
            item_id=self.graph_id or False, item_name=self.name,
            message=message or False,
        )

    def action_create_remote(self):
        self._check_admin()
        from ..services.sharepoint_service import SharePointService

        for site in self:
            if site.graph_id:
                raise UserError("El sitio ya existe en Microsoft.")
            if site.site_type == "existing":
                raise UserError("Use Sincronizar tenant para incorporar sitios existentes.")
            service = SharePointService(site.tenant_id)
            try:
                if site.site_type == "team_group":
                    group = site.primary_group_id
                    if not group or group.group_type != "microsoft365":
                        raise UserError("Seleccione un grupo Microsoft 365 como grupo principal.")
                    if not group.graph_id:
                        group.action_create_remote()
                    data = service.get_group_site(group.graph_id)
                    if not isinstance(data, dict) or not data.get("id"):
                        site.sudo().write({
                            "status": "provisioning",
                            "last_error": "Microsoft todavía está aprovisionando el sitio.",
                        })
                        _logger.info("Sitio SharePoint aún no disponible: site=%s group=%s", site.id, group.graph_id)
                        continue
                else:
                    if not site.url_slug or not site.owner_email:
                        raise UserError("Indique la ruta del sitio y el correo del propietario.")
                    response = service.create_modern_site(site)
                    data = {"id": response.get("SiteId"), "webUrl": response.get("SiteUrl")}
                    if not data.get("id"):
                        # SharePoint puede aprovisionar de forma asíncrona.
                        site.sudo().write({
                            "status": "provisioning",
                            "web_url": data.get("webUrl") or False,
                            "last_error": "Sitio solicitado; pendiente de identificador Microsoft.",
                        })
                        continue
                site.sudo().write({
                    "graph_id": data["id"], "web_url": data.get("webUrl") or False,
                    "status": "ready", "last_sync": fields.Datetime.now(), "last_error": False,
                })
                site.action_sync_libraries()
                site._audit("site_create")
            except Exception:
                _logger.exception("Error creando sitio SharePoint site_id=%s", site.id)
                raise
        return True

    def action_sync(self):
        self._check_admin()
        from ..services.sharepoint_service import SharePointService

        for site in self:
            service = SharePointService(site.tenant_id)
            if site.primary_group_id and site.primary_group_id.graph_id:
                data = service.get_group_site(site.primary_group_id.graph_id)
            elif site.graph_id:
                data = service.get_site(site.graph_id)
            else:
                raise UserError("El sitio necesita un ID Microsoft o un grupo Microsoft 365 vinculado.")
            if not isinstance(data, dict) or not data.get("id"):
                site.sudo().write({
                    "status": "provisioning",
                    "last_error": "Microsoft todavía no devuelve el sitio solicitado.",
                })
                continue
            site.sudo().write({
                "graph_id": data["id"], "web_url": data.get("webUrl") or site.web_url,
                "status": "ready", "last_sync": fields.Datetime.now(), "last_error": False,
            })
            site.action_sync_libraries()
            site._audit("sync", message="Metadatos de sitio y bibliotecas sincronizados")
        return True

    def action_sync_libraries(self):
        self._check_admin()
        from ..services.sharepoint_service import SharePointService
        for site in self:
            if not site.graph_id:
                raise UserError("Primero cree o sincronice el sitio en Microsoft.")
            count = SharePointService(site.tenant_id).sync_libraries(site)
            site.sudo().write({"last_sync": fields.Datetime.now(), "last_error": False})
            _logger.info("Bibliotecas sincronizadas site_id=%s count=%s", site.id, count)
        return True

    def action_apply_group_access(self):
        """Otorga acceso por biblioteca y revoca solo grupos antes aplicados por esta acción.

        ADVERTENCIA: Graph puede devolver permisos heredados o compartidos por
        otros administradores; estos nunca deben revocarse por coincidencia
        de una identidad no registrada previamente en este modelo.
        """
        self._check_admin()
        from ..services.drive_service import DriveService

        for site in self:
            if not site.graph_id:
                raise UserError("Primero sincronice el sitio en Microsoft.")
            if not site.library_ids.filtered(lambda lib: lib.active and lib.drive_id):
                site.action_sync_libraries()
            readers = site.reader_group_ids
            editors = site.editor_group_ids
            if (readers | editors).filtered(lambda group: not group.graph_id):
                raise UserError("Todos los grupos de acceso deben estar sincronizados con Microsoft.")
            before = site.applied_reader_group_ids | site.applied_editor_group_ids
            desired = readers | editors
            revoke_ids = set((before - desired).mapped("graph_id")) - {False}
            locations = self.env["microsoft.storage.location"].sudo().search([
                ("site_id", "=", site.id), ("active", "=", True),
                ("drive_id", "!=", False),
            ])
            if not locations:
                raise UserError("El sitio no tiene bibliotecas activas configuradas.")
            for location in locations:
                drive = DriveService(location, self.env.user)
                root_id = drive.get_root().get("id")
                if not root_id:
                    raise UserError("No se pudo identificar la raíz de la biblioteca.")
                for group in readers:
                    drive.invite_recipient({"objectId": group.graph_id}, "read", item_id=root_id)
                for group in editors:
                    drive.invite_recipient({"objectId": group.graph_id}, "write", item_id=root_id)
                if revoke_ids:
                    for permission in drive.list_permissions(root_id):
                        if drive.permission_group_ids(permission) & revoke_ids:
                            permission_id = permission.get("id")
                            if permission_id and not permission.get("inheritedFrom"):
                                drive.remove_permission(root_id, permission_id)
            site.sudo().write({
                "applied_reader_group_ids": [(6, 0, readers.ids)],
                "applied_editor_group_ids": [(6, 0, editors.ids)],
            })
            site._audit("share", message="Acceso de grupos aplicado en bibliotecas")
        return True


class SharePointLibrary(models.Model):
    _name = "sharepoint.library"
    _description = "Biblioteca SharePoint"
    _order = "name"

    site_id = fields.Many2one("sharepoint.site", required=True, ondelete="cascade", index=True)
    tenant_id = fields.Many2one(related="site_id.tenant_id", store=True, index=True)
    name = fields.Char(required=True)
    description = fields.Text()
    drive_id = fields.Char(string="Drive ID", readonly=True, copy=False, index=True)
    list_id = fields.Char(string="List ID", readonly=True, copy=False)
    web_url = fields.Char(readonly=True)
    is_default = fields.Boolean()
    last_sync = fields.Datetime(readonly=True)
    active = fields.Boolean(default=True)

    @api.constrains("name")
    def _check_name(self):
        for library in self:
            name = (library.name or "").strip()
            if not name or len(name) > 255 or any(ord(c) < 32 for c in name):
                raise ValidationError("El nombre de la biblioteca no es válido.")

    def action_create_remote(self):
        if not self.env.user.has_group(_ADMIN):
            raise AccessError("Solo el administrador Microsoft 365 puede crear bibliotecas.")
        from ..services.sharepoint_service import SharePointService

        for library in self:
            if library.drive_id:
                continue
            if not library.site_id.graph_id:
                raise UserError("Primero cree o sincronice el sitio.")
            service = SharePointService(library.tenant_id)
            service.create_library(library.site_id, library.name, library.description)
            library.site_id.action_sync_libraries()
            # La sincronización encuentra la biblioteca nueva por drive_id.
            # No inventar un ID si Microsoft todavía no publica la biblioteca.
        return True
