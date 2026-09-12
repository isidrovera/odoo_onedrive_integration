# -*- coding: utf-8 -*-
from urllib.parse import urlparse

from odoo import fields
from odoo.exceptions import UserError

from .graph_client import GraphClient


class SharePointService:
    def __init__(self, tenant):
        self.tenant = tenant
        self.env = tenant.env
        self.graph = GraphClient(token_provider=lambda: tenant.get_app_token("graph"), env=self.env)

    def get_site(self, site_id):
        return self.graph.request("GET", f"/sites/{site_id}")

    def get_group_site(self, group_id):
        return self.graph.request("GET", f"/groups/{group_id}/sites/root", expected=(404,))

    def list_sites(self):
        return self.graph.fetch_all("/sites", params={"search": "*", "$top": "200"})

    def sync_sites(self):
        Site = self.env["sharepoint.site"].sudo()
        for data in self.list_sites():
            values = {
                "tenant_id": self.tenant.id,
                "graph_id": data.get("id"),
                "name": data.get("displayName") or data.get("name") or data.get("webUrl"),
                "web_url": data.get("webUrl"),
                "status": "ready",
                "last_sync": fields.Datetime.now(),
            }
            site = Site.search([("tenant_id", "=", self.tenant.id), ("graph_id", "=", data.get("id"))], limit=1)
            if site:
                site.write(values)
            else:
                values["site_type"] = "existing"
                Site.create(values)
        return True

    def create_modern_site(self, site):
        if not self.tenant.sharepoint_root_url:
            raise UserError("Configure la URL raíz de SharePoint en el tenant.")
        root = self.tenant.sharepoint_root_url.rstrip("/")
        parsed = urlparse(root)
        slug = site.url_slug.strip("/")
        site_url = f"{parsed.scheme}://{parsed.netloc}/sites/{slug}"
        template = "SITEPAGEPUBLISHING#0" if site.site_type == "communication" else "STS#3"
        client = GraphClient(
            token_provider=lambda: self.tenant.get_app_token("sharepoint"),
            env=self.env,
            base_url=root,
        )
        return client.request("POST", "/_api/SPSiteManager/create", headers={"OData-Version": "4.0"}, json={"request": {
            "Title": site.name,
            "Url": site_url,
            "Lcid": site.locale_id or 3082,
            "ShareByEmailEnabled": False,
            "Description": site.description or "",
            "WebTemplate": template,
            "Owner": site.owner_email,
        }})

    def list_libraries(self, site_id):
        return self.graph.fetch_all(
            f"/sites/{site_id}/drives",
            params={"$top": "200", "$select": "id,name,description,webUrl,driveType,sharepointIds"},
        )

    def create_library(self, site, name, description=None):
        return self.graph.request("POST", f"/sites/{site.graph_id}/lists", json={
            "displayName": name,
            "description": description or "",
            "list": {"template": "documentLibrary"},
        })

    def sync_libraries(self, site):
        Library = self.env["sharepoint.library"].sudo()
        Location = self.env["microsoft.storage.location"].sudo()
        for data in self.list_libraries(site.graph_id):
            values = {
                "site_id": site.id,
                "drive_id": data.get("id"),
                "name": data.get("name"),
                "description": data.get("description"),
                "list_id": (data.get("sharepointIds") or {}).get("listId"),
                "web_url": data.get("webUrl"),
                "last_sync": fields.Datetime.now(),
            }
            library = Library.search([("site_id", "=", site.id), ("drive_id", "=", data.get("id"))], limit=1)
            if library:
                library.write(values)
            else:
                library = Library.create(values)
            location = Location.search([("library_id", "=", library.id)], limit=1)
            location_values = {
                "name": f"{site.name} / {library.name}",
                "tenant_id": self.tenant.id,
                "location_type": "sharepoint",
                "site_id": site.id,
                "library_id": library.id,
                "drive_id": library.drive_id,
            }
            if location:
                location.write(location_values)
            else:
                Location.create(location_values)
        return True
