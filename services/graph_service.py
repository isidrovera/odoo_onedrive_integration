# services/graph_service.py
import requests
import logging
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class GraphService:
    def __init__(self, account):
        self.account = account
        self.base_url = account.env['ir.config_parameter'].sudo().get_param(
            'onedrive.graph.base_url',
            default="https://graph.microsoft.com/v1.0"
        )
        self.token = account.get_valid_token()

    def _headers(self, content_type="application/json"):
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": content_type,
        }

    def _request(self, method, endpoint, **kwargs):
        url = f"{self.base_url}{endpoint}"
        headers = kwargs.pop("headers", {})
        headers.update(self._headers())
        response = requests.request(method, url, headers=headers, **kwargs)
        data = {}
        if response.text:
            try:
                data = response.json()
            except Exception:
                data = {"raw": response.text}
        if response.status_code >= 400:
            _logger.error("Graph error: %s", data)
            raise UserError(f"Error Microsoft Graph: {data}")
        return data

    # ---------- LIST ----------
    def list_root(self):
        return self._request("GET", "/me/drive/root/children?$top=200&$expand=thumbnails")

    def list_children(self, item_id):
        return self._request("GET", f"/me/drive/items/{item_id}/children?$top=200&$expand=thumbnails")

    def get_item(self, item_id):
        return self._request("GET", f"/me/drive/items/{item_id}?$expand=thumbnails")

    # ---------- SEARCH ----------
    def search(self, query):
        return self._request("GET", f"/me/drive/root/search(q='{query}')?$top=100")

    # ---------- CREATE / RENAME / MOVE / COPY ----------
    def create_folder(self, name, parent_id=None):
        endpoint = "/me/drive/root/children" if not parent_id \
            else f"/me/drive/items/{parent_id}/children"
        payload = {
            "name": name,
            "folder": {},
            "@microsoft.graph.conflictBehavior": "rename",
        }
        return self._request("POST", endpoint, json=payload)

    def rename_item(self, item_id, new_name):
        return self._request("PATCH", f"/me/drive/items/{item_id}", json={"name": new_name})

    def move_item(self, item_id, target_parent_id):
        payload = {"parentReference": {"id": target_parent_id}}
        return self._request("PATCH", f"/me/drive/items/{item_id}", json=payload)

    def copy_item(self, item_id, target_parent_id, new_name=None):
        payload = {"parentReference": {"id": target_parent_id}}
        if new_name:
            payload["name"] = new_name
        return self._request("POST", f"/me/drive/items/{item_id}/copy", json=payload)

    # ---------- DELETE ----------
    def delete_item(self, item_id):
        return self._request("DELETE", f"/me/drive/items/{item_id}")

    # ---------- DOWNLOAD ----------
    def get_download_url(self, item_id):
        data = self._request("GET", f"/me/drive/items/{item_id}")
        if "folder" in data:
            raise UserError("Usa get_folder_download_url para carpetas")
        url = data.get("@microsoft.graph.downloadUrl")
        if not url:
            raise UserError(f"No se pudo obtener enlace: {data}")
        return url

    def get_folder_download_url(self, item_id):
        # Microsoft Graph permite descargar carpetas como zip vía endpoint /content
        # con un truco: usar el webUrl + ?download=1 no siempre funciona,
        # usamos el endpoint estándar que retorna zip cuando el item es carpeta
        return f"{self.base_url}/me/drive/items/{item_id}/content"

    # ---------- THUMBNAIL ----------
    def get_thumbnail_url(self, item_id, size='medium'):
        try:
            data = self._request("GET", f"/me/drive/items/{item_id}/thumbnails")
            thumbs = data.get('value', [])
            if thumbs and size in thumbs[0]:
                return thumbs[0][size]['url']
        except Exception:
            return None
        return None

    # ---------- PREVIEW ----------
    def get_preview_url(self, item_id):
        return self._request("POST", f"/me/drive/items/{item_id}/preview", json={})

    # ---------- SHARE ----------
    def create_share_link(self, item_id, share_type='view', scope='anonymous'):
        payload = {"type": share_type, "scope": scope}
        return self._request("POST", f"/me/drive/items/{item_id}/createLink", json=payload)

    # ---------- UPLOAD ----------
    def upload_file(self, filename, file_content, parent_id=None):
        if parent_id:
            endpoint = f"/me/drive/items/{parent_id}:/{filename}:/content"
        else:
            endpoint = f"/me/drive/root:/{filename}:/content"
        url = f"{self.base_url}{endpoint}"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/octet-stream",
        }
        # Para archivos > 4MB se debería usar upload session. Aquí simple PUT.
        response = requests.put(url, headers=headers, data=file_content)
        if response.status_code >= 400:
            raise UserError(response.text)
        return response.json()