import requests
import logging
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class GraphService:

    def __init__(self, account):
        self.account = account

        # 🔥 CONFIG DINÁMICA
        self.base_url = account.env['ir.config_parameter'].sudo().get_param(
            'onedrive.graph.base_url',
            default="https://graph.microsoft.com/v1.0"
        )

        self.token = account.get_valid_token()

    # ---------------------------------------
    # HEADERS
    # ---------------------------------------
    def _headers(self, content_type="application/json"):
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": content_type
        }

    # ---------------------------------------
    # REQUEST WRAPPER
    # ---------------------------------------
    def _request(self, method, endpoint, **kwargs):

        url = f"{self.base_url}{endpoint}"

        headers = kwargs.pop("headers", {})
        headers.update(self._headers())

        try:
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

        except Exception as e:
            _logger.exception("Graph request failed")
            raise UserError(str(e))

    # ---------------------------------------
    # LIST FILES
    # ---------------------------------------
    def list_root(self):
        return self._request("GET", "/me/drive/root/children")

    def list_children(self, item_id):
        return self._request("GET", f"/me/drive/items/{item_id}/children")

    # ---------------------------------------
    # CREATE FOLDER
    # ---------------------------------------
    def create_folder(self, name, parent_id=None):

        endpoint = "/me/drive/root/children"

        if parent_id:
            endpoint = f"/me/drive/items/{parent_id}/children"

        payload = {
            "name": name,
            "folder": {},
            "@microsoft.graph.conflictBehavior": "rename"
        }

        return self._request("POST", endpoint, json=payload)

    # ---------------------------------------
    # DELETE
    # ---------------------------------------
    def delete_item(self, item_id):
        return self._request("DELETE", f"/me/drive/items/{item_id}")

    # ---------------------------------------
    # DOWNLOAD URL
    # ---------------------------------------
    def get_download_url(self, item_id):
        data = self._request("GET", f"/me/drive/items/{item_id}")
        return data.get("@microsoft.graph.downloadUrl")

    # ---------------------------------------
    # UPLOAD SIMPLE
    # ---------------------------------------
    def upload_file(self, filename, file_content, parent_path="/"):

        endpoint = f"/me/drive/root:{parent_path}{filename}:/content"
        url = f"{self.base_url}{endpoint}"

        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/octet-stream"
        }

        response = requests.put(url, headers=headers, data=file_content)

        if response.status_code >= 400:
            raise UserError(response.text)

        return response.json()