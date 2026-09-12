# -*- coding: utf-8 -*-
import os
from urllib.parse import quote

import requests

from odoo.exceptions import UserError

from .graph_client import GraphClient


class DriveService:
    SMALL_UPLOAD_LIMIT = 4 * 1024 * 1024
    CHUNK_SIZE = 5 * 1024 * 1024  # múltiplo de 320 KiB

    def __init__(self, location, user=None):
        self.location = location
        self.env = location.env
        self.user = user or self.env.user
        self.tenant = location.tenant_id
        self.connection = self.env["microsoft.user.connection"].sudo().search([
            ("tenant_id", "=", self.tenant.id), ("user_id", "=", self.user.id), ("active", "=", True)
        ], limit=1)

        if self.connection and self.connection.state == "connected":
            token_provider = lambda: self.connection.with_user(self.user).get_valid_token()
        elif (
            location.location_type == "onedrive"
            and self.tenant.refresh_token
            and self.user.has_group("odoo_onedrive_integration.group_microsoft_admin")
        ):
            token_provider = lambda: self.tenant.get_valid_token()
        elif location.location_type == "sharepoint" or location.drive_id:
            token_provider = lambda: self.tenant.get_app_token("graph")
        else:
            raise UserError(
                "Conecte su identidad Microsoft desde Mi conexión antes de abrir OneDrive."
            )
        self.client = GraphClient(token_provider=token_provider, env=self.env)
        self.drive_base = f"/drives/{location.drive_id}" if location.drive_id else "/me/drive"

    def list_root(self):
        return {"value": self.client.fetch_all(f"{self.drive_base}/root/children", params={"$top": "200"})}

    def list_children(self, item_id):
        return {"value": self.client.fetch_all(f"{self.drive_base}/items/{item_id}/children", params={"$top": "200"})}

    def get_item(self, item_id):
        return self.client.request("GET", f"{self.drive_base}/items/{item_id}")

    def get_root(self):
        return self.client.request("GET", f"{self.drive_base}/root")

    def search(self, query):
        safe = (query or "").replace("'", "''")
        return {"value": self.client.fetch_all(f"{self.drive_base}/root/search(q='{quote(safe, safe='')}')", params={"$top": "200"})}

    def create_folder(self, name, parent_id=None):
        endpoint = f"{self.drive_base}/root/children" if not parent_id else f"{self.drive_base}/items/{parent_id}/children"
        return self.client.request("POST", endpoint, json={
            "name": name, "folder": {}, "@microsoft.graph.conflictBehavior": "rename",
        })

    def rename_item(self, item_id, new_name):
        return self.client.request("PATCH", f"{self.drive_base}/items/{item_id}", json={"name": new_name})

    def move_item(self, item_id, target_parent_id):
        return self.client.request("PATCH", f"{self.drive_base}/items/{item_id}", json={
            "parentReference": {"id": target_parent_id},
        })

    def copy_item(self, item_id, target_parent_id, new_name=None):
        payload = {"parentReference": {"id": target_parent_id}}
        if new_name:
            payload["name"] = new_name
        return self.client.request("POST", f"{self.drive_base}/items/{item_id}/copy", json=payload)

    def delete_item(self, item_id):
        return self.client.request("DELETE", f"{self.drive_base}/items/{item_id}")

    def get_download_url(self, item_id):
        data = self.get_item(item_id)
        url = data.get("@microsoft.graph.downloadUrl")
        if not url:
            raise UserError("Microsoft no devolvió un enlace de descarga.")
        return url, data

    def get_thumbnail_url(self, item_id, size="medium"):
        data = self.client.request("GET", f"{self.drive_base}/items/{item_id}/thumbnails")
        values = data.get("value") or []
        return values[0].get(size, {}).get("url") if values else None

    def get_preview_url(self, item_id):
        return self.client.request("POST", f"{self.drive_base}/items/{item_id}/preview", json={})

    def create_share_link(self, item_id, share_type="view", scope="organization"):
        if scope not in ("organization", "anonymous"):
            raise UserError("Tipo de enlace no permitido.")
        return self.client.request("POST", f"{self.drive_base}/items/{item_id}/createLink", json={
            "type": share_type, "scope": scope,
        })

    def invite_user(self, email, role="read", item_id=None, send_invitation=False):
        return self.invite_recipient(
            {"email": email}, role=role, item_id=item_id, send_invitation=send_invitation
        )

    def invite_recipient(self, recipient, role="read", item_id=None, send_invitation=False):
        """Concede acceso a un usuario o grupo mediante email, alias u objectId."""
        if set(recipient) - {"email", "alias", "objectId"} or len(recipient) != 1:
            raise UserError("El destinatario Microsoft no es válido.")
        item_id = item_id or self.get_root().get("id")
        return self.client.request("POST", f"{self.drive_base}/items/{item_id}/invite", json={
            "recipients": [recipient],
            "message": "Acceso documental asignado desde Odoo",
            "requireSignIn": True,
            "sendInvitation": bool(send_invitation),
            "roles": ["write" if role == "write" else "read"],
        })

    def remove_permission(self, item_id, permission_id):
        return self.client.request("DELETE", f"{self.drive_base}/items/{item_id}/permissions/{permission_id}", expected=(404,))

    def list_permissions(self, item_id=None):
        item_id = item_id or self.get_root().get("id")
        return self.client.fetch_all(f"{self.drive_base}/items/{item_id}/permissions", params={"$top": "200"})

    @staticmethod
    def permission_group_ids(permission):
        identities = []
        if permission.get("grantedToV2"):
            identities.append(permission["grantedToV2"])
        identities.extend(permission.get("grantedToIdentitiesV2") or [])
        return {
            identity.get("group", {}).get("id")
            for identity in identities
            if identity.get("group", {}).get("id")
        }

    def upload_file(self, filename, file_content, parent_id=None):
        from io import BytesIO
        return self.upload_stream(filename, BytesIO(file_content), len(file_content), parent_id)

    def upload_stream(self, filename, stream, size, parent_id=None):
        if size < self.SMALL_UPLOAD_LIMIT:
            return self._upload_small(filename, stream.read(), parent_id)
        return self._upload_large(filename, stream, size, parent_id)

    def _item_path(self, filename, parent_id=None, suffix="content"):
        safe_name = quote(os.path.basename(filename), safe="")
        if parent_id:
            return f"{self.drive_base}/items/{parent_id}:/{safe_name}:/{suffix}"
        return f"{self.drive_base}/root:/{safe_name}:/{suffix}"

    def _upload_small(self, filename, content, parent_id=None):
        return self.client.request(
            "PUT", self._item_path(filename, parent_id), data=content,
            headers={"Content-Type": "application/octet-stream"}, timeout=120,
        )

    def _upload_large(self, filename, stream, total_size, parent_id=None):
        session = self.client.request("POST", self._item_path(filename, parent_id, "createUploadSession"), json={
            "item": {"@microsoft.graph.conflictBehavior": "rename", "name": os.path.basename(filename)},
        })
        upload_url = session.get("uploadUrl")
        if not upload_url:
            raise UserError("Microsoft no creó la sesión de carga.")
        offset = 0
        last = None
        while offset < total_size:
            chunk = stream.read(self.CHUNK_SIZE)
            if not chunk:
                raise UserError("El archivo terminó antes del tamaño informado.")
            end = offset + len(chunk) - 1
            try:
                response = requests.put(upload_url, headers={
                    "Content-Length": str(len(chunk)),
                    "Content-Range": f"bytes {offset}-{end}/{total_size}",
                }, data=chunk, timeout=180)
            except requests.RequestException as error:
                raise UserError(f"La carga fue interrumpida: {error}") from error
            if response.status_code >= 400:
                raise UserError(f"Microsoft rechazó un bloque del archivo ({response.status_code}).")
            last = response
            offset = end + 1
        try:
            return last.json()
        except ValueError:
            return {"status": "uploaded", "name": filename}
