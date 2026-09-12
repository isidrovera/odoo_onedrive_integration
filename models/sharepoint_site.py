# -*- coding: utf-8 -*-
from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError


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
    locale_id = fields.Integer(string="LCID", default=3082, help="3082 corresponde a español de España; puede ajustarse según el tenant.")
    status = fields.Selection([
        ("draft", "Borrador"), ("provisioning", "Creando"), ("ready", "Disponible"), ("error", "Error")
    ], default="draft", required=True, tracking=True)
    reader_group_ids = fields.Many2many("microsoft.group", "sharepoint_site_reader_group_rel", "site_id", "group_id", string="Grupos lectores")
    editor_group_ids = fields.Many2many("microsoft.group", "sharepoint_site_editor_group_rel", "site_id", "group_id", string="Grupos editores")
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

    _sql_constraints = [
        ("sharepoint_site_graph_tenant_unique", "unique(tenant_id, graph_id)", "El sitio ya está sincronizado."),
    ]

    @api.constrains("tenant_id", "primary_group_id", "reader_group_ids", "editor_group_ids")
    def _check_group_configuration(self):
        for site in self:
            groups = site.primary_group_id | site.reader_group_ids | site.editor_group_ids
            if any(group.tenant_id != site.tenant_id for group in groups):
                raise ValidationError("Todos los grupos del sitio deben pertenecer al mismo tenant.")
            if site.reader_group_ids & site.editor_group_ids:
                raise ValidationError("Un grupo no puede ser lector y editor al mismo tiempo.")

    def _check_admin(self):
        if not self.env.user.has_group("odoo_onedrive_integration.group_microsoft_admin"):
            raise AccessError("Solo el administrador Microsoft 365 puede administrar sitios.")

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
                    if not data or not data.get("id"):
                        site.sudo().write({"status": "provisioning", "last_error": "Microsoft todavía está aprovisionando el sitio."})
                        continue
                else:
                    if not site.url_slug or not site.owner_email:
                        raise UserError("Indique la ruta del sitio y el correo del propietario.")
                    response = service.create_modern_site(site)
                    data = {
                        "id": response.get("SiteId"),
                        "webUrl": response.get("SiteUrl"),
                    }
                site.sudo().write({
                    "graph_id": data.get("id"), "web_url": data.get("webUrl"),
                    "status": "ready", "last_sync": fields.Datetime.now(), "last_error": False,
                })
                site.action_sync_libraries()
                self.env["microsoft.audit.log"]._log("site_create", tenant_id=site.tenant_id.id, item_id=site.graph_id, item_name=site.name)
            except Exception as error:
                site.sudo().write({"status": "error", "last_error": str(error)})
                raise
        return True

    def action_sync(self):
        self._check_admin()
        from ..services.sharepoint_service import SharePointService
        for site in self:
            service = SharePointService(site.tenant_id)
            data = None
            if site.primary_group_id and site.primary_group_id.graph_id:
                data = service.get_group_site(site.primary_group_id.graph_id)
            elif site.graph_id:
                data = service.get_site(site.graph_id)
            if data and data.get("id"):
                site.sudo().write({
                    "graph_id": data.get("id"), "web_url": data.get("webUrl"),
                    "status": "ready", "last_sync": fields.Datetime.now(), "last_error": False,
                })
                site.action_sync_libraries()
        return True

    def action_sync_libraries(self):
        self._check_admin()
        from ..services.sharepoint_service import SharePointService
        for site in self:
            if site.graph_id:
                SharePointService(site.tenant_id).sync_libraries(site)
                site.sudo().write({"last_sync": fields.Datetime.now()})
        return True

    def action_apply_group_access(self):
        self._check_admin()
        from ..services.drive_service import DriveService
        for site in self:
            if not site.library_ids:
                site.action_sync_libraries()
            desired_groups = (site.reader_group_ids | site.editor_group_ids).filtered("graph_id")
            previously_applied = site.applied_reader_group_ids | site.applied_editor_group_ids
            remove_graph_ids = set((previously_applied - desired_groups).mapped("graph_id")) - {False}
            locations = self.env["microsoft.storage.location"].sudo().search([("site_id", "=", site.id)])
            for location in locations:
                drive = DriveService(location, self.env.user)
                for group in site.reader_group_ids.filtered("graph_id"):
                    drive.invite_recipient({"objectId": group.graph_id}, "read")
                for group in site.editor_group_ids.filtered("graph_id"):
                    drive.invite_recipient({"objectId": group.graph_id}, "write")
                if remove_graph_ids:
                    root_id = drive.get_root().get("id")
                    for permission in drive.list_permissions(root_id):
                        if drive.permission_group_ids(permission) & remove_graph_ids:
                            permission_id = permission.get("id")
                            if permission_id:
                                drive.remove_permission(root_id, permission_id)
            site.sudo().write({
                "applied_reader_group_ids": [(6, 0, site.reader_group_ids.ids)],
                "applied_editor_group_ids": [(6, 0, site.editor_group_ids.ids)],
            })
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

    def action_create_remote(self):
        if not self.env.user.has_group("odoo_onedrive_integration.group_microsoft_admin"):
            raise AccessError("Solo el administrador Microsoft 365 puede crear bibliotecas.")
        from ..services.sharepoint_service import SharePointService
        for library in self:
            if library.drive_id:
                continue
            if not library.site_id.graph_id:
                raise UserError("Primero cree o sincronice el sitio.")
            SharePointService(library.tenant_id).create_library(library.site_id, library.name, library.description)
            library.site_id.action_sync_libraries()
        return True
