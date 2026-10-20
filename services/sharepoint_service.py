# -*- coding: utf-8 -*-
"""Gestión de sitios y bibliotecas SharePoint en Microsoft 365 (Odoo 19).

Mantiene las firmas consumidas por sharepoint.site, sharepoint.library y
onedrive.account. La sincronización nunca elimina registros ni sus ACL locales.
"""
import logging
import re
from urllib.parse import urlsplit, quote

from odoo import fields
from odoo.exceptions import AccessError, UserError

from .graph_client import GraphClient

_logger = logging.getLogger(__name__)


class SharePointService:
    PAGE_SIZE = 200
    MAX_SITES = 5000
    MAX_LIBRARIES_PER_SITE = 2000
    _SLUG_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,99}$")

    def __init__(self, tenant):
        tenant.ensure_one()
        self.tenant = tenant
        self.env = tenant.env
        self.graph = GraphClient(
            token_provider=lambda: self.tenant.get_app_token("graph"),
            env=self.env,
        )

    @staticmethod
    def _segment(value):
        if not isinstance(value, str) or not value.strip():
            raise UserError("El identificador de Microsoft es obligatorio.")
        value = value.strip()
        if len(value) > 512 or any(ord(ch) < 32 for ch in value):
            raise UserError("El identificador de Microsoft es inválido.")
        return quote(value, safe=",")  # Los IDs de sitios Graph contienen comas.

    def _check_admin(self):
        if not self.env.user.has_group("odoo_onedrive_integration.group_microsoft_admin"):
            raise AccessError("Solo el administrador Microsoft 365 puede administrar SharePoint.")

    @staticmethod
    def _require_dict(value, operation):
        if not isinstance(value, dict):
            raise UserError("Microsoft devolvió un resultado inválido al %s." % operation)
        return value

    @staticmethod
    def _require_list(value, operation):
        if not isinstance(value, list):
            raise UserError("Microsoft devolvió una lista inválida al %s." % operation)
        return value

    def get_site(self, site_id):
        return self.graph.request("GET", "/sites/%s" % self._segment(site_id))

    def get_group_site(self, group_id):
        # Graph puede devolver 404 mientras se aprovisiona un grupo M365.
        return self.graph.request(
            "GET", "/groups/%s/sites/root" % self._segment(group_id),
            expected=(404,),
        )

    def list_sites(self):
        result = self.graph.fetch_all(
            "/sites", params={"search": "*", "$top": str(self.PAGE_SIZE)},
            max_pages=100,
        )
        result = self._require_list(result, "consultar sitios")
        if len(result) > self.MAX_SITES:
            raise UserError("La sincronización supera el máximo permitido de sitios.")
        return result

    def sync_sites(self):
        """Crea/actualiza sitios existentes sin tocar grupos, permisos ni sitios ausentes."""
        self._check_admin()
        Site = self.env["sharepoint.site"].sudo()
        count = 0
        for data in self.list_sites():
            if not isinstance(data, dict) or not data.get("id"):
                _logger.warning("SharePoint: sitio remoto sin id; omitido")
                continue
            site_id = data["id"]
            vals = {
                "name": data.get("displayName") or data.get("name") or site_id,
                "web_url": data.get("webUrl") or False,
                "status": "ready",
                "last_sync": fields.Datetime.now(),
                "last_error": False,
            }
            site = Site.search([
                ("tenant_id", "=", self.tenant.id),
                ("graph_id", "=", site_id),
            ], limit=1)
            if site:
                site.write(vals)
            else:
                Site.create({
                    **vals,
                    "tenant_id": self.tenant.id,
                    "graph_id": site_id,
                    "site_type": "existing",
                })
            count += 1
        _logger.info("SharePoint sites sync tenant_id=%s count=%s", self.tenant.id, count)
        return count

    def _sharepoint_root(self):
        root = (self.tenant.sharepoint_root_url or "").strip().rstrip("/")
        parts = urlsplit(root)
        if (
            parts.scheme.lower() != "https"
            or not parts.hostname
            or not parts.hostname.lower().endswith(".sharepoint.com")
            or parts.username is not None or parts.password is not None
            or parts.port not in (None, 443)
            or parts.query or parts.fragment or parts.path not in ("", "/")
        ):
            raise UserError("Configure la URL raíz HTTPS del tenant SharePoint (sin ruta adicional).")
        return "https://%s" % parts.hostname.lower()

    def create_modern_site(self, site):
        """Crear sitios modernos sin grupo; los de grupo los aprovisiona Entra.

        La respuesta de SPSiteManager conserva las claves SiteId y SiteUrl
        utilizadas por sharepoint.site.action_create_remote().
        """
        self._check_admin()
        site.ensure_one()
        if site.tenant_id != self.tenant:
            raise UserError("El sitio no pertenece al tenant seleccionado.")
        if site.graph_id:
            raise UserError("El sitio ya está vinculado a Microsoft.")
        if site.site_type not in ("communication", "team_nogroup"):
            raise UserError("Este tipo de sitio se crea mediante su grupo Microsoft 365.")
        slug = (site.url_slug or "").strip("/")
        if not self._SLUG_RE.fullmatch(slug):
            raise UserError("Indique una ruta de sitio válida (letras, números, guion o guion bajo).")
        if not site.owner_email or "@" not in site.owner_email:
            raise UserError("Indique el correo Microsoft del propietario.")
        root = self._sharepoint_root()
        site_url = "%s/sites/%s" % (root, slug)
        template = "SITEPAGEPUBLISHING#0" if site.site_type == "communication" else "STS#3"
        payload = {
            "request": {
                "Title": site.name,
                "Url": site_url,
                "Lcid": int(site.locale_id or 1033),
                "ShareByEmailEnabled": False,
                "Description": site.description or "",
                "WebTemplate": template,
                "Owner": site.owner_email.strip(),
            }
        }
        client = GraphClient(
            token_provider=lambda: self.tenant.get_app_token("sharepoint"),
            env=self.env, base_url=root,
        )
        response = self._require_dict(client.request(
            "POST", "/_api/SPSiteManager/create",
            headers={"Accept": "application/json;odata.metadata=none", "OData-Version": "4.0"},
            json=payload, timeout=120,
        ), "crear sitio SharePoint")
        # Algunas versiones devuelven el objeto envuelto en d / Create.
        result = response.get("d") or response
        result = result.get("Create") or result.get("create") or result
        result = self._require_dict(result, "interpretar creación de sitio")
        if not result.get("SiteId") and not result.get("SiteUrl"):
            _logger.warning("SharePoint create response without site ID tenant_id=%s", self.tenant.id)
        return result

    def list_libraries(self, site_id):
        result = self.graph.fetch_all(
            "/sites/%s/drives" % self._segment(site_id),
            params={
                "$select": "id,name,description,webUrl,driveType,sharepointIds",
                "$top": str(self.PAGE_SIZE),
            }, max_pages=100,
        )
        result = self._require_list(result, "consultar bibliotecas")
        if len(result) > self.MAX_LIBRARIES_PER_SITE:
            raise UserError("La sincronización supera el máximo permitido de bibliotecas.")
        return result

    def create_library(self, site, name, description=None):
        self._check_admin()
        site.ensure_one()
        if site.tenant_id != self.tenant or not site.graph_id:
            raise UserError("El sitio debe existir en el tenant Microsoft seleccionado.")
        name = (name or "").strip()
        if not name or len(name) > 255 or any(ord(ch) < 32 for ch in name):
            raise UserError("Indique un nombre válido para la biblioteca.")
        data = self.graph.request(
            "POST", "/sites/%s/lists" % self._segment(site.graph_id),
            json={
                "displayName": name,
                "description": description or "",
                "list": {"template": "documentLibrary"},
            },
        )
        return self._require_dict(data, "crear biblioteca")

    def sync_libraries(self, site):
        """Sincroniza metadatos/ubicaciones conservando roles asignados.

        No elimina bibliotecas desaparecidas, no sobrescribe nombres locales
        de ubicaciones ya configuradas y no modifica sus M2M de acceso.
        """
        self._check_admin()
        site.ensure_one()
        if site.tenant_id != self.tenant or not site.graph_id:
            raise UserError("Debe sincronizar primero el sitio del tenant seleccionado.")
        Library = self.env["sharepoint.library"].sudo()
        Location = self.env["microsoft.storage.location"].sudo()
        count = 0
        for data in self.list_libraries(site.graph_id):
            if not isinstance(data, dict) or not data.get("id"):
                _logger.warning("SharePoint: biblioteca sin drive_id site_id=%s", site.id)
                continue
            drive_id = data["id"]
            library = Library.search([
                ("site_id", "=", site.id),
                ("drive_id", "=", drive_id),
            ], limit=1)
            values = {
                "name": data.get("name") or drive_id,
                "description": data.get("description") or False,
                "web_url": data.get("webUrl") or False,
                "last_sync": fields.Datetime.now(),
            }
            # No convertir bibliotecas personalizadas en default: Graph no
            # garantiza esa señal en todos los drives.
            if library:
                library.write(values)
            else:
                ids = data.get("sharepointIds") or {}
                library = Library.create({
                    **values,
                    "site_id": site.id,
                    "drive_id": drive_id,
                    "list_id": ids.get("listId") or False,
                })
            location = Location.search([
                ("tenant_id", "=", self.tenant.id),
                ("drive_id", "=", drive_id),
            ], limit=1)
            if location:
                if location.location_type != "sharepoint" and (location.reader_user_ids or location.editor_user_ids or location.manager_user_ids):
                    raise UserError("La unidad %s está asignada como OneDrive; revise la configuración antes de sincronizar." % drive_id)
                location.write({
                    "location_type": "sharepoint",
                    "site_id": site.id,
                    "library_id": library.id,
                    "web_url": values["web_url"],
                })
            else:
                Location.create({
                    "name": values["name"],
                    "tenant_id": self.tenant.id,
                    "location_type": "sharepoint",
                    "site_id": site.id,
                    "library_id": library.id,
                    "drive_id": drive_id,
                    "web_url": values["web_url"],
                })
            count += 1
        _logger.info("SharePoint libraries sync tenant_id=%s site_id=%s count=%s", self.tenant.id, site.id, count)
        return count
