# services/graph_service.py
import requests
import logging
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class GraphService:
    """Servicio cliente para Microsoft Graph API (OneDrive Empresarial)."""

    # Tamaño de página por defecto (Graph permite hasta 999 en algunos endpoints)
    DEFAULT_PAGE_SIZE = 200

    # Timeout por request (segundos)
    DEFAULT_TIMEOUT = 60

    # Máximo de páginas a recorrer (evita loops infinitos en carpetas enormes)
    MAX_PAGES = 50

    def __init__(self, account):
        self.account = account
        self.base_url = account.env['ir.config_parameter'].sudo().get_param(
            'onedrive.graph.base_url',
            default="https://graph.microsoft.com/v1.0"
        )
        self.token = account.get_valid_token()

    # =========================================================
    # CORE
    # =========================================================
    def _headers(self, content_type="application/json"):
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": content_type,
        }

    def _request(self, method, endpoint, timeout=None, **kwargs):
        """Request a Graph API con manejo unificado de errores."""
        url = f"{self.base_url}{endpoint}"
        headers = kwargs.pop("headers", {})
        headers.update(self._headers())
        try:
            response = requests.request(
                method, url,
                headers=headers,
                timeout=timeout or self.DEFAULT_TIMEOUT,
                **kwargs
            )
        except requests.Timeout:
            _logger.error("Graph timeout: %s %s", method, url)
            raise UserError("Microsoft Graph no respondió a tiempo")
        except requests.RequestException as e:
            _logger.error("Graph network error: %s", e)
            raise UserError(f"Error de red: {e}")

        data = {}
        if response.text:
            try:
                data = response.json()
            except Exception:
                data = {"raw": response.text}

        if response.status_code >= 400:
            _logger.error("Graph error %s: %s", response.status_code, data)
            err_msg = data.get("error", {}).get("message") if isinstance(data, dict) else str(data)
            raise UserError(f"Error Microsoft Graph: {err_msg or data}")
        return data

    def _request_absolute_url(self, url):
        """Para nextLinks de paginación que devuelve Graph como URL absoluta."""
        try:
            response = requests.get(
                url,
                headers=self._headers(),
                timeout=self.DEFAULT_TIMEOUT,
            )
        except requests.RequestException as e:
            _logger.error("Graph paginated network error: %s", e)
            raise UserError(f"Error de red en paginación: {e}")

        if response.status_code >= 400:
            _logger.error("Graph paginated error %s: %s", response.status_code, response.text)
            raise UserError(f"Error Microsoft Graph (paginación): {response.status_code}")
        return response.json()

    def _fetch_all_pages(self, endpoint):
        """
        Pagina automáticamente respuestas de Graph siguiendo @odata.nextLink.
        Retorna {"value": [...todos los items...]}.
        """
        all_values = []
        next_url = endpoint
        page = 0

        while next_url and page < self.MAX_PAGES:
            page += 1
            if next_url.startswith("http"):
                data = self._request_absolute_url(next_url)
            else:
                data = self._request("GET", next_url)
            values = data.get("value", []) or []
            all_values.extend(values)
            next_url = data.get("@odata.nextLink")
            if not next_url:
                break

        if page >= self.MAX_PAGES and next_url:
            _logger.warning(
                "Se alcanzó el máximo de páginas (%s). Algunos items pueden no aparecer.",
                self.MAX_PAGES
            )

        return {"value": all_values, "page_count": page}

    # =========================================================
    # LIST (con paginación, sin thumbnails para velocidad)
    # =========================================================
    def list_root(self):
        endpoint = f"/me/drive/root/children?$top={self.DEFAULT_PAGE_SIZE}"
        return self._fetch_all_pages(endpoint)

    def list_children(self, item_id):
        endpoint = f"/me/drive/items/{item_id}/children?$top={self.DEFAULT_PAGE_SIZE}"
        return self._fetch_all_pages(endpoint)

    def get_item(self, item_id):
        return self._request("GET", f"/me/drive/items/{item_id}")

    # =========================================================
    # SEARCH (también con paginación, hasta MAX_PAGES)
    # =========================================================
    def search(self, query):
        # Sanitizar comillas simples: en Graph, q='...' con apóstrofe rompe
        safe_query = (query or "").replace("'", "''")
        endpoint = f"/me/drive/root/search(q='{safe_query}')?$top={self.DEFAULT_PAGE_SIZE}"
        return self._fetch_all_pages(endpoint)

    # =========================================================
    # CREATE / RENAME / MOVE / COPY
    # =========================================================
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
        return self._request(
            "PATCH",
            f"/me/drive/items/{item_id}",
            json={"name": new_name},
        )

    def move_item(self, item_id, target_parent_id):
        payload = {"parentReference": {"id": target_parent_id}}
        return self._request("PATCH", f"/me/drive/items/{item_id}", json=payload)

    def copy_item(self, item_id, target_parent_id, new_name=None):
        payload = {"parentReference": {"id": target_parent_id}}
        if new_name:
            payload["name"] = new_name
        return self._request("POST", f"/me/drive/items/{item_id}/copy", json=payload)

    # =========================================================
    # DELETE
    # =========================================================
    def delete_item(self, item_id):
        return self._request("DELETE", f"/me/drive/items/{item_id}")

    # =========================================================
    # DOWNLOAD
    # =========================================================
    def get_download_url(self, item_id):
        data = self._request("GET", f"/me/drive/items/{item_id}")
        if "folder" in data:
            raise UserError("Usa get_folder_download_url para carpetas")
        url = data.get("@microsoft.graph.downloadUrl")
        if not url:
            raise UserError(f"No se pudo obtener enlace de descarga")
        return url

    def get_folder_download_url(self, item_id):
        """URL del endpoint que devuelve un ZIP de la carpeta."""
        return f"{self.base_url}/me/drive/items/{item_id}/content"

    # =========================================================
    # THUMBNAIL (bajo demanda, no en listados)
    # =========================================================
    def get_thumbnail_url(self, item_id, size='medium'):
        try:
            data = self._request("GET", f"/me/drive/items/{item_id}/thumbnails")
            thumbs = data.get('value', [])
            if thumbs and size in thumbs[0]:
                return thumbs[0][size].get('url')
        except Exception as e:
            _logger.debug("No thumbnail for %s: %s", item_id, e)
            return None
        return None

    # =========================================================
    # PREVIEW
    # =========================================================
    def get_preview_url(self, item_id):
        return self._request("POST", f"/me/drive/items/{item_id}/preview", json={})

    # =========================================================
    # SHARE
    # =========================================================
    def create_share_link(self, item_id, share_type='view', scope='anonymous'):
        payload = {"type": share_type, "scope": scope}
        return self._request("POST", f"/me/drive/items/{item_id}/createLink", json=payload)

    # =========================================================
    # UPLOAD
    #   - <4MB: PUT directo
    #   - >=4MB: upload session con chunks (más robusto para archivos grandes)
    # =========================================================
    UPLOAD_THRESHOLD = 4 * 1024 * 1024  # 4 MB
    UPLOAD_CHUNK_SIZE = 5 * 1024 * 1024  # 5 MB chunks

    def upload_file(self, filename, file_content, parent_id=None):
        size = len(file_content)
        if size < self.UPLOAD_THRESHOLD:
            return self._upload_small(filename, file_content, parent_id)
        return self._upload_large(filename, file_content, parent_id)

    def _upload_small(self, filename, file_content, parent_id=None):
        """PUT directo para archivos pequeños (<4MB)."""
        if parent_id:
            endpoint = f"/me/drive/items/{parent_id}:/{filename}:/content"
        else:
            endpoint = f"/me/drive/root:/{filename}:/content"
        url = f"{self.base_url}{endpoint}"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/octet-stream",
        }
        try:
            response = requests.put(
                url,
                headers=headers,
                data=file_content,
                timeout=self.DEFAULT_TIMEOUT,
            )
        except requests.RequestException as e:
            raise UserError(f"Error red al subir: {e}")

        if response.status_code >= 400:
            raise UserError(f"Error subiendo: {response.text}")
        return response.json()

    def _upload_large(self, filename, file_content, parent_id=None):
        """Upload session con chunks para archivos grandes (>=4MB)."""
        # 1. Crear sesión
        if parent_id:
            session_endpoint = f"/me/drive/items/{parent_id}:/{filename}:/createUploadSession"
        else:
            session_endpoint = f"/me/drive/root:/{filename}:/createUploadSession"
        session = self._request(
            "POST",
            session_endpoint,
            json={
                "item": {
                    "@microsoft.graph.conflictBehavior": "rename",
                    "name": filename,
                }
            },
        )
        upload_url = session.get("uploadUrl")
        if not upload_url:
            raise UserError("No se pudo crear sesión de upload")

        # 2. Subir en chunks
        total_size = len(file_content)
        chunk_size = self.UPLOAD_CHUNK_SIZE
        offset = 0
        last_response = None

        while offset < total_size:
            end = min(offset + chunk_size, total_size) - 1
            chunk = file_content[offset:end + 1]
            content_range = f"bytes {offset}-{end}/{total_size}"
            headers = {
                "Content-Length": str(len(chunk)),
                "Content-Range": content_range,
            }
            try:
                response = requests.put(
                    upload_url,
                    headers=headers,
                    data=chunk,
                    timeout=self.DEFAULT_TIMEOUT * 2,
                )
            except requests.RequestException as e:
                raise UserError(f"Error red en chunk {offset}: {e}")

            if response.status_code >= 400:
                # Limpiar sesión si falla
                try:
                    requests.delete(upload_url, timeout=10)
                except Exception:
                    pass
                raise UserError(f"Error subiendo chunk: {response.text}")

            last_response = response
            offset = end + 1

        # 3. Última respuesta tiene los metadatos del archivo final
        try:
            return last_response.json()
        except Exception:
            return {"status": "uploaded", "name": filename}