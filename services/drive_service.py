# -*- coding: utf-8 -*-

"""Operaciones documentales Microsoft Graph para ubicaciones autorizadas Odoo 19.



Este servicio opera únicamente dentro del drive de la ubicación recibida.

Las autorizaciones de operaciones se comprueban también en el servidor.

"""

import io

import logging

import re

import time

from urllib.parse import quote, urlsplit



import requests



from odoo.exceptions import AccessError, UserError



from .graph_client import GraphClient



_logger = logging.getLogger(__name__)





class DriveService:

    SMALL_UPLOAD_LIMIT = 4 * 1024 * 1024

    CHUNK_SIZE = 5 * 1024 * 1024  # Múltiplo de 320 KiB obligatorio en Graph.

    UPLOAD_TIMEOUT = 180

    MAX_CHUNK_RETRIES = 4

    MAX_FILE_NAME = 255

    _INVALID_FILE_CHARS = re.compile(r'["*:<>?/\\|\x00-\x1f]')

    _ALLOWED_DOWNLOAD_DOMAINS = (

        "sharepoint.com", "sharepoint-df.com", "1drv.com",

        "onedrive.com", "onedrive.live.com",

    )



    def __init__(self, location, user=None):

        location.ensure_one()

        self.location = location

        self.env = location.env

        self.user = user or self.env.user

        self.tenant = location.tenant_id

        if not location.active or not self.tenant:

            raise AccessError("La ubicación documental o la conexión Microsoft no están activas.")



        self.connection = self.env["microsoft.user.connection"].sudo().search([

            ("tenant_id", "=", self.tenant.id),

            ("user_id", "=", self.user.id),

            ("active", "=", True),

        ], limit=1)



        # SharePoint/drive explícito: preferir identidad delegada conectada,

        # conservando la autenticación de aplicación que ya usa el módulo.

        if self.connection and self.connection.state == "connected":

            token_provider = lambda: self.connection.with_user(self.user).get_valid_token()

        elif (

            location.location_type == "onedrive"

            and self.user.has_group("odoo_onedrive_integration.group_microsoft_admin")

            and bool(self.tenant.sudo().refresh_token)

        ):

            # El refresh token es un campo protegido. Solo se consulta con sudo

            # despues de confirmar el rol administrador, nunca desde el cliente.

            token_provider = lambda: self.tenant.get_valid_token()

        elif location.location_type == "sharepoint" or location.drive_id:

            token_provider = lambda: self.tenant.get_app_token("graph")

        else:

            raise UserError("Conecte su identidad Microsoft desde Mi conexión antes de abrir OneDrive.")



        self.client = GraphClient(token_provider=token_provider, env=self.env)

        self.drive_base = (

            f"/drives/{self._segment(location.drive_id)}"

            if location.drive_id else "/me/drive"

        )



    @staticmethod

    def _segment(value):

        if not isinstance(value, str) or not value.strip():

            raise UserError("El identificador documental es obligatorio.")

        value = value.strip()

        if len(value) > 512 or any(ord(c) < 32 for c in value):

            raise UserError("El identificador documental no es válido.")

        return quote(value, safe="")



    @classmethod

    def _filename(cls, name):

        if not isinstance(name, str):

            raise UserError("Debe indicar un nombre de documento válido.")

        name = name.strip()

        if (

            not name or name in (".", "..")

            or len(name) > cls.MAX_FILE_NAME

            or cls._INVALID_FILE_CHARS.search(name)

            or name.endswith(".")

        ):

            raise UserError("El nombre contiene caracteres no permitidos por Microsoft 365.")

        return name



    def _check(self, operation):

        # La verificación en el controlador no sustituye a la de servicio.

        return self.location.check_operation(operation, self.user)



    def _item(self, item_id):

        return f"{self.drive_base}/items/{self._segment(item_id)}"



    def _children(self, parent_id):

        return (f"{self._item(parent_id)}/children" if parent_id

                else f"{self.drive_base}/root/children")



    @classmethod

    def _validate_temporary_url(cls, url):

        """Prohíbe HTTP, credenciales en URL y hosts fuera de Microsoft.



        Las URL temporales de carga/descarga deben consumirse sin agregar el

        token Graph; la propia URL ya es una credencial temporal.

        """

        if not isinstance(url, str) or len(url) > 10000:

            raise UserError("Microsoft no devolvió una URL temporal válida.")

        parts = urlsplit(url)

        host = (parts.hostname or "").lower()

        if (

            parts.scheme.lower() != "https" or not host or

            parts.username is not None or parts.password is not None or

            parts.port not in (None, 443) or parts.fragment or

            not any(host == domain or host.endswith("." + domain)

                    for domain in cls._ALLOWED_DOWNLOAD_DOMAINS)

        ):

            raise UserError("La dirección temporal no pertenece a un dominio autorizado de Microsoft.")

        return url



    # ------------------------------------------------------------

    # Navegación y búsquedas

    # ------------------------------------------------------------

    def list_root(self):

        self._check("list")

        return {"value": self.client.fetch_all(self._children(None), params={"$top": "200"})}



    def list_children(self, item_id):

        self._check("list")

        return {"value": self.client.fetch_all(self._children(item_id), params={"$top": "200"})}



    def get_item(self, item_id):

        self._check("properties")

        return self.client.request("GET", self._item(item_id))



    def get_root(self):

        self._check("properties")

        return self.client.request("GET", f"{self.drive_base}/root")



    def search(self, query):

        self._check("search")

        query = (query or "").strip()

        if not query:

            return {"value": []}

        if len(query) > 256:

            raise UserError("La búsqueda es demasiado extensa.")

        safe = quote(query.replace("'", "''"), safe="")

        endpoint = f"{self.drive_base}/root/search(q='{safe}')"

        return {"value": self.client.fetch_all(endpoint, params={"$top": "200"})}



    # ------------------------------------------------------------

    # Edición

    # ------------------------------------------------------------

    def ensure_folder(self, name, parent_id=None):
        """Reutiliza una carpeta existente; nunca duplica una por nombre."""
        self._check("create_folder")
        name = self._filename(name)
        children = self.client.fetch_all(self._children(parent_id), params={"$top": "200"})
        for child in children:
            if child.get("name", "").casefold() == name.casefold():
                if not child.get("folder"):
                    raise UserError("Existe un archivo con el nombre de la carpeta solicitada.")
                return child
        try:
            return self.client.request("POST", self._children(parent_id), json={
                "name": name, "folder": {}, "@microsoft.graph.conflictBehavior": "fail",
            })
        except UserError:
            # Puede haberla creado simultáneamente otra carga.
            children = self.client.fetch_all(self._children(parent_id), params={"$top": "200"})
            for child in children:
                if child.get("name", "").casefold() == name.casefold() and child.get("folder"):
                    return child
            raise

    def start_chunk_upload(self, name, size, parent_id=None):
        """Crear una sesión Graph sin recibir el contenido completo en Odoo."""
        self._check("upload")
        name = self._filename(name)
        try:
            size = int(size)
        except (TypeError, ValueError) as exc:
            raise UserError("Tamaño de archivo no válido.") from exc
        # Límite externo Graph: 250 GB por archivo; no imponer 100 MB.
        if size <= 0 or size > 250 * 1000**3:
            raise UserError("El tamaño debe ser positivo y no superar el máximo de Microsoft 365 (250 GB).")
        if parent_id:
            folder = self.client.request("GET", self._item(parent_id))
            if not folder.get("folder"):
                raise UserError("El destino debe ser una carpeta.")
        session = self.client.request(
            "POST", self._item_path(name, parent_id, "createUploadSession"),
            json={"item": {"@microsoft.graph.conflictBehavior": "rename", "name": name}},
        )
        url = self._validate_temporary_url(session.get("uploadUrl"))
        return {"upload_url": url, "size": size, "name": name, "parent_id": parent_id}

    def create_folder(self, name, parent_id=None):

        self._check("create_folder")

        name = self._filename(name)

        return self.client.request("POST", self._children(parent_id), json={

            "name": name, "folder": {}, "@microsoft.graph.conflictBehavior": "rename",

        })



    def rename_item(self, item_id, new_name):

        self._check("rename")

        return self.client.request("PATCH", self._item(item_id), json={

            "name": self._filename(new_name),

        })



    def move_item(self, item_id, target_parent_id):

        self._check("move")

        if str(item_id) == str(target_parent_id):

            raise UserError("No puede mover una carpeta dentro de sí misma.")

        return self.client.request("PATCH", self._item(item_id), json={

            "parentReference": {"id": str(target_parent_id)},

        })



    def copy_item(self, item_id, target_parent_id, new_name=None):

        self._check("copy")

        payload = {"parentReference": {"id": str(target_parent_id)}}

        self._segment(target_parent_id)

        if new_name:

            payload["name"] = self._filename(new_name)

        # Graph devuelve habitualmente 202 (operación asíncrona).

        return self.client.request("POST", f"{self._item(item_id)}/copy", json=payload)



    def delete_item(self, item_id):

        self._check("delete")

        return self.client.request("DELETE", self._item(item_id))



    # ------------------------------------------------------------

    # Lectura, previsualización y descargas

    # ------------------------------------------------------------

    def get_download_url(self, item_id):

        """Retorna (url, item), como exige controllers/main.py."""

        self._check("download")

        item = self.client.request("GET", self._item(item_id))

        if item.get("folder") is not None:

            raise UserError("La descarga directa de carpetas no está disponible.")

        url = item.get("@microsoft.graph.downloadUrl")

        if not url:

            raise UserError("Microsoft no devolvió un enlace temporal de descarga.")

        return self._validate_temporary_url(url), item



    def get_thumbnail_url(self, item_id, size="medium"):

        self._check("preview")

        if size not in ("small", "medium", "large"):

            raise UserError("Tamaño de miniatura no permitido.")

        data = self.client.request("GET", f"{self._item(item_id)}/thumbnails")

        for entry in data.get("value") or []:

            thumbnail = (entry.get(size) or {}) if isinstance(entry, dict) else {}

            if thumbnail.get("url"):

                return self._validate_temporary_url(thumbnail["url"])

        return None



    def get_preview_url(self, item_id):

        self._check("preview")

        return self.client.request("POST", f"{self._item(item_id)}/preview", json={})



    # ------------------------------------------------------------

    # Compartición y permisos Graph

    # ------------------------------------------------------------

    def create_share_link(self, item_id, share_type="view", scope="organization"):

        self._check("share")

        if share_type not in ("view", "edit"):

            raise UserError("Tipo de enlace no admitido.")

        if scope not in ("organization", "anonymous"):

            raise UserError("Ámbito de compartición no admitido.")

        # La compartición anónima requiere activación administrativa expresa.

        if scope == "anonymous":

            enabled = self.env["ir.config_parameter"].sudo().get_param(

                "microsoft365.allow_anonymous_links", "False"

            )

            if str(enabled).lower() not in ("true", "1", "yes"):

                raise AccessError("Los enlaces anónimos están deshabilitados por política.")

        return self.client.request("POST", f"{self._item(item_id)}/createLink", json={

            "type": share_type, "scope": scope,

        })



    def invite_user(self, email, role="read", item_id=None, send_invitation=False):

        return self.invite_recipient({"email": email}, role, item_id, send_invitation)



    def invite_recipient(self, recipient, role="read", item_id=None, send_invitation=False):

        self._check("share")

        if not isinstance(recipient, dict) or len(recipient) != 1 or not set(recipient) <= {

            "email", "alias", "objectId"

        }:

            raise UserError("Debe indicar un único destinatario Microsoft válido.")

        key, value = next(iter(recipient.items()))

        if not isinstance(value, str) or not value.strip() or len(value) > 320:

            raise UserError("El destinatario no es válido.")

        if role not in ("read", "write"):

            raise UserError("Rol de invitación no válido.")

        if not item_id:

            item_id = self.get_root().get("id")

        return self.client.request("POST", f"{self._item(item_id)}/invite", json={

            "recipients": [{key: value.strip()}],

            "message": "Acceso documental asignado desde Odoo",

            "requireSignIn": True,

            "sendInvitation": bool(send_invitation),

            "roles": [role],

        })



    def remove_permission(self, item_id, permission_id):

        self._check("share")

        return self.client.request(

            "DELETE", f"{self._item(item_id)}/permissions/{self._segment(permission_id)}",

            expected=(404,),

        )



    def list_permissions(self, item_id=None):

        self._check("share")

        item_id = item_id or self.get_root().get("id")

        return self.client.fetch_all(

            f"{self._item(item_id)}/permissions", params={"$top": "200"}

        )



    @staticmethod

    def permission_group_ids(permission):

        if not isinstance(permission, dict):

            return set()

        identities = []

        if permission.get("grantedToV2"):

            identities.append(permission["grantedToV2"])

        identities.extend(permission.get("grantedToIdentitiesV2") or [])

        return {

            identity["group"]["id"]

            for identity in identities

            if isinstance(identity, dict)

            and isinstance(identity.get("group"), dict)

            and identity["group"].get("id")

        }



    # ------------------------------------------------------------

    # Versiones de archivos (Graph driveItem versions)

    # ------------------------------------------------------------

    def list_versions(self, item_id):

        self._check("properties")

        return {"value": self.client.fetch_all(

            f"{self._item(item_id)}/versions", params={"$top": "200"}

        )}



    def get_version(self, item_id, version_id):

        self._check("properties")

        return self.client.request(

            "GET", f"{self._item(item_id)}/versions/{self._segment(version_id)}"

        )



    def restore_version(self, item_id, version_id):

        self._check("rename")  # Derecho de modificación, no solo lectura.

        return self.client.request(

            "POST", f"{self._item(item_id)}/versions/{self._segment(version_id)}/restoreVersion",

            json={},

        )



    # ------------------------------------------------------------

    # Subidas pequeñas y grandes, sin guardar originales en Odoo

    # ------------------------------------------------------------

    def upload_file(self, filename, file_content, parent_id=None):

        self._check("upload")

        if not isinstance(file_content, (bytes, bytearray)):

            raise UserError("El contenido del documento debe ser binario.")

        return self.upload_stream(filename, io.BytesIO(file_content), len(file_content), parent_id)



    def upload_stream(self, filename, stream, size, parent_id=None):

        self._check("upload")

        filename = self._filename(filename)

        try:

            size = int(size)

        except (TypeError, ValueError) as error:

            raise UserError("Tamaño de archivo inválido.") from error

        if size < 0:

            raise UserError("El tamaño de archivo es inválido.")

        if not hasattr(stream, "read"):

            raise UserError("No se recibió un flujo de archivo válido.")

        if size < self.SMALL_UPLOAD_LIMIT:

            content = stream.read(size)

            if len(content) != size:

                raise UserError("No se recibió el archivo completo.")

            return self._upload_small(filename, content, parent_id)

        return self._upload_large(filename, stream, size, parent_id)



    def _item_path(self, filename, parent_id=None, suffix="content"):

        filename = quote(self._filename(filename), safe="")

        if suffix not in ("content", "createUploadSession"):

            raise UserError("Operación de subida inválida.")

        base = self._item(parent_id) if parent_id else f"{self.drive_base}/root"

        return f"{base}:/{filename}:/{suffix}"



    def _upload_small(self, filename, content, parent_id=None):

        self._check("upload")

        return self.client.request(

            "PUT", self._item_path(filename, parent_id),

            data=content,

            headers={"Content-Type": "application/octet-stream"},

            timeout=120,

        )



    @staticmethod

    def _json_response(response):

        try:

            payload = response.json()

        except ValueError:

            return {}

        return payload if isinstance(payload, dict) else {}



    def _upload_large(self, filename, stream, total_size, parent_id=None):

        self._check("upload")

        session = self.client.request(

            "POST", self._item_path(filename, parent_id, "createUploadSession"),

            json={"item": {

                "@microsoft.graph.conflictBehavior": "rename",

                "name": filename,

            }},

        )

        upload_url = self._validate_temporary_url(session.get("uploadUrl"))

        offset = 0

        last_payload = {}

        try:

            while offset < total_size:

                chunk = stream.read(min(self.CHUNK_SIZE, total_size - offset))

                if not chunk:

                    raise UserError("El archivo terminó antes del tamaño declarado.")

                end = offset + len(chunk) - 1

                headers = {

                    "Content-Length": str(len(chunk)),

                    "Content-Range": f"bytes {offset}-{end}/{total_size}",

                }

                response = None

                for attempt in range(self.MAX_CHUNK_RETRIES):

                    try:

                        response = requests.put(

                            upload_url, headers=headers, data=chunk,

                            timeout=self.UPLOAD_TIMEOUT, allow_redirects=False,

                        )

                    except (requests.Timeout, requests.ConnectionError) as error:

                        if attempt + 1 == self.MAX_CHUNK_RETRIES:

                            raise UserError("Se interrumpió la subida a Microsoft 365.") from error

                        # No repetir ciegamente un fragmento: consultar primero el estado.

                        next_offset = self._session_offset(upload_url, offset)

                        if next_offset != offset:

                            if next_offset == end + 1:

                                offset = next_offset

                                break

                            raise UserError("Microsoft cambió el desplazamiento de la subida.")

                        time.sleep(min(2 ** attempt, 8))

                        continue

                    try:

                        if response.status_code in (200, 201):

                            last_payload = self._json_response(response)

                            if not last_payload.get("id"):

                                raise UserError("Microsoft no confirmó el archivo finalizado.")

                            return last_payload

                        if response.status_code == 202:

                            payload = self._json_response(response)

                            ranges = payload.get("nextExpectedRanges") or []

                            if ranges:

                                try:

                                    next_offset = int(str(ranges[0]).split("-")[0])

                                except (ValueError, IndexError):

                                    next_offset = end + 1

                            else:

                                next_offset = end + 1

                            if next_offset != end + 1:

                                raise UserError("Microsoft devolvió una posición de subida inesperada.")

                            offset = next_offset

                            break

                        if response.status_code in (429, 500, 502, 503, 504):

                            if attempt + 1 == self.MAX_CHUNK_RETRIES:

                                raise UserError(f"Microsoft rechazó la subida ({response.status_code}).")

                            next_offset = self._session_offset(upload_url, offset)

                            if next_offset == end + 1:

                                offset = next_offset

                                break

                            if next_offset != offset:

                                raise UserError("Microsoft devolvió un desplazamiento incompatible.")

                            time.sleep(min(2 ** attempt, 8))

                            continue

                        raise UserError(f"Microsoft rechazó un fragmento ({response.status_code}).")

                    finally:

                        response.close()

                else:

                    raise UserError("No se pudo completar la subida del fragmento.")

            # La confirmación final debe incluir driveItem.id; no simular éxito.

            raise UserError("Microsoft no confirmó que se haya completado la subida.")

        except Exception:

            # El servidor debe intentar cerrar una sesión abandonada.

            try:

                requests.delete(upload_url, timeout=10, allow_redirects=False)

            except requests.RequestException:

                _logger.debug("No se pudo cancelar una sesión de subida Microsoft.")

            raise



    def _session_offset(self, upload_url, default):

        self._validate_temporary_url(upload_url)

        try:

            response = requests.get(upload_url, timeout=30, allow_redirects=False)

        except requests.RequestException:

            return default

        try:

            if response.status_code != 200:

                return default

            ranges = self._json_response(response).get("nextExpectedRanges") or []

            if not ranges:

                return default

            return int(str(ranges[0]).split("-")[0])

        except (ValueError, IndexError):

            return default

        finally:

            response.close()
