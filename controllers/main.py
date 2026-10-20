# -*- coding: utf-8 -*-
"""Controlador documental Microsoft 365 para Odoo 19.

Todas las operaciones están autorizadas en el servidor sobre una ubicación
concreta. Nunca se utilizan permisos de aplicación para omitir controles Odoo.
"""
import json
import logging
from urllib.parse import quote as url_quote

import requests as http_requests

from odoo import http
from odoo.exceptions import AccessError, UserError
from odoo.http import Response, request

from ..services.drive_service import DriveService

_logger = logging.getLogger(__name__)


class MicrosoftDocumentsController(http.Controller):

    @staticmethod
    def _json(data, status=200):
        return Response(json.dumps(data, ensure_ascii=False), status=status,
                        content_type="application/json; charset=utf-8")

    def _location(self, location_id=None):
        """Resolver exclusivamente una ubicación accesible al usuario actual."""
        Location = request.env["microsoft.storage.location"]
        if location_id in (None, False, ""):
            location = Location.allowed_for_user()[:1]
        else:
            try:
                key = int(location_id)
            except (ValueError, TypeError, OverflowError):
                raise UserError("El identificador de ubicación no es válido.")
            if key <= 0:
                raise UserError("El identificador de ubicación no es válido.")
            location = Location.browse(key).exists()
        if not location or not location.active or not location.access_level(request.env.user):
            raise AccessError("No existe una ubicación documental autorizada.")
        return location

    def _service(self, operation, location_id=None):
        location = self._location(location_id)
        location.check_operation(operation, request.env.user)
        return location, DriveService(location, request.env.user)

    @staticmethod
    def _audit(operation, location, status="success", **values):
        try:
            request.env["microsoft.audit.log"]._log(
                operation, status=status, tenant_id=location.tenant_id.id,
                location_id=location.id, **values,
            )
        except Exception:
            # No falsear la operación documental por un error secundario de auditoría.
            _logger.exception("[MICROSOFT] No se pudo registrar auditoría %s", operation)

    @staticmethod
    def _temporary_get(url, *, stream=False, timeout=60):
        """Descargar exclusivamente URLs temporales verificadas por DriveService.

        No reenviar un token Graph a servidores de archivos ni seguir redirecciones
        HTTP que pudieran sacar la solicitud fuera de dominios Microsoft.
        """
        url = DriveService._validate_temporary_url(url)
        response = http_requests.get(url, timeout=timeout, stream=stream,
                                     allow_redirects=False)
        if 300 <= response.status_code < 400:
            response.close()
            raise UserError("Microsoft devolvió una redirección no admitida.")
        return response

    @http.route("/microsoft/locations", type="jsonrpc", auth="user")
    def locations(self):
        result = []
        for location in request.env["microsoft.storage.location"].allowed_for_user():
            role = location.access_level(request.env.user)
            if not role:
                continue
            result.append({
                "id": location.id,
                "name": location.name,
                "type": location.location_type,
                "site_name": location.site_id.name or "",
                "library_name": location.library_id.name or "",
                "web_url": location.web_url or location.library_id.web_url
                           or location.site_id.web_url or "",
                "role": role,
                "can_edit": role in ("editor", "manager"),
                "can_manage": role == "manager",
            })
        return result

    @http.route("/microsoft/list", type="jsonrpc", auth="user")
    def list_files(self, parent_id=None, location_id=None):
        location, service = self._service("list", location_id)
        result = service.list_children(parent_id) if parent_id else service.list_root()
        self._audit("list", location, item_id=parent_id)
        return result

    @http.route("/microsoft/search", type="jsonrpc", auth="user")
    def search_files(self, query, location_id=None):
        location, service = self._service("search", location_id)
        result = service.search(query)
        self._audit("search", location, message=str(query or "")[:200])
        return result

    @http.route("/microsoft/item", type="jsonrpc", auth="user")
    def get_item(self, item_id, location_id=None):
        location, service = self._service("properties", location_id)
        result = service.get_item(item_id)
        self._audit("properties", location, item_id=item_id)
        return result

    @http.route("/microsoft/create_folder", type="jsonrpc", auth="user")
    def create_folder(self, name, parent_id=None, location_id=None):
        location, service = self._service("create_folder", location_id)
        result = service.create_folder(name, parent_id)
        self._audit("create_folder", location, item_id=result.get("id"),
                    item_name=result.get("name"))
        return result

    @http.route("/microsoft/rename", type="jsonrpc", auth="user")
    def rename_item(self, item_id, new_name, location_id=None):
        location, service = self._service("rename", location_id)
        result = service.rename_item(item_id, new_name)
        self._audit("rename", location, item_id=item_id, item_name=new_name)
        return result

    @http.route("/microsoft/move", type="jsonrpc", auth="user")
    def move_item(self, item_id, target_parent_id, location_id=None):
        location, service = self._service("move", location_id)
        result = service.move_item(item_id, target_parent_id)
        self._audit("move", location, item_id=item_id)
        return result

    @http.route("/microsoft/copy", type="jsonrpc", auth="user")
    def copy_item(self, item_id, target_parent_id, new_name=None, location_id=None):
        location, service = self._service("copy", location_id)
        result = service.copy_item(item_id, target_parent_id, new_name)
        self._audit("copy", location, item_id=item_id, item_name=new_name)
        return result

    @http.route("/microsoft/delete", type="jsonrpc", auth="user")
    def delete_item(self, item_id, location_id=None):
        location, service = self._service("delete", location_id)
        item = service.get_item(item_id)
        service.delete_item(item_id)
        self._audit("delete", location, item_id=item_id, item_name=item.get("name"))
        return {"status": "deleted"}

    @http.route("/microsoft/share", type="jsonrpc", auth="user")
    def share_item(self, item_id, share_type="view", scope="organization", location_id=None):
        location, service = self._service("share", location_id)
        result = service.create_share_link(item_id, share_type, scope)
        self._audit("share", location, item_id=item_id, message=f"{share_type}/{scope}")
        return result

    @http.route("/microsoft/preview", type="jsonrpc", auth="user")
    def preview_item(self, item_id, location_id=None):
        location, service = self._service("preview", location_id)
        result = service.get_preview_url(item_id)
        self._audit("preview", location, item_id=item_id)
        return result

    @http.route("/microsoft/versions", type="jsonrpc", auth="user")
    def list_versions(self, item_id, location_id=None):
        location, service = self._service("versions", location_id)
        result = service.list_versions(item_id)
        self._audit("version_list", location, item_id=item_id)
        return result

    @http.route("/microsoft/version", type="jsonrpc", auth="user")
    def version_properties(self, item_id, version_id, location_id=None):
        location, service = self._service("versions", location_id)
        return service.get_version(item_id, version_id)

    @http.route("/microsoft/restore_version", type="jsonrpc", auth="user")
    def restore_version(self, item_id, version_id, location_id=None):
        location, service = self._service("restore_version", location_id)
        result = service.restore_version(item_id, version_id)
        self._audit("version_restore", location, item_id=item_id,
                    message=f"Versión restaurada: {version_id}")
        return result or {"status": "restored"}

    @http.route("/microsoft/permissions", type="jsonrpc", auth="user")
    def list_permissions(self, item_id=None, location_id=None):
        location, service = self._service("permissions", location_id)
        result = service.list_permissions(item_id)
        self._audit("permission_list", location, item_id=item_id)
        return result

    @http.route("/microsoft/invite_permission", type="jsonrpc", auth="user")
    def invite_permission(self, item_id, email, role="read", location_id=None):
        location, service = self._service("invite", location_id)
        result = service.invite_user(email, role=role, item_id=item_id)
        self._audit("permission_add", location, item_id=item_id,
                    message="Acceso documental concedido")
        return result

    @http.route("/microsoft/remove_permission", type="jsonrpc", auth="user")
    def remove_permission(self, item_id, permission_id, location_id=None):
        location, service = self._service("remove_permission", location_id)
        result = service.remove_permission(item_id, permission_id)
        self._audit("permission_remove", location, item_id=item_id)
        return result or {"status": "removed"}

    @http.route("/microsoft/thumbnail/<string:item_id>", type="http", auth="user")
    def thumbnail(self, item_id, size="medium", location_id=None):
        remote = None
        try:
            location, service = self._service("preview", location_id)
            url = service.get_thumbnail_url(item_id, size)
            if not url:
                return Response(status=404)
            remote = self._temporary_get(url, timeout=30)
            if remote.status_code >= 400:
                return Response(status=404)
            if len(remote.content) > 10 * 1024 * 1024:
                return Response(status=413)
            content_type = remote.headers.get("Content-Type", "image/jpeg").split(";")[0]
            if not content_type.startswith("image/"):
                return Response(status=415)
            return request.make_response(remote.content, headers=[
                ("Content-Type", content_type),
                ("Cache-Control", "private, max-age=300"),
                ("X-Content-Type-Options", "nosniff"),
            ])
        except (AccessError, UserError):
            return Response(status=403)
        except http_requests.RequestException:
            _logger.exception("Microsoft thumbnail network error")
            return Response(status=502)
        finally:
            if remote is not None:
                remote.close()

    @http.route("/microsoft/download/<string:item_id>", type="http", auth="user")
    def download_file(self, item_id, location_id=None):
        remote = None
        try:
            location, service = self._service("download", location_id)
            download_url, item = service.get_download_url(item_id)
            remote = self._temporary_get(download_url, stream=True, timeout=120)
            if remote.status_code >= 400:
                status = remote.status_code
                remote.close()
                return request.make_response("Microsoft rechazó la descarga.", status=status)

            def generate():
                try:
                    for chunk in remote.iter_content(chunk_size=64 * 1024):
                        if chunk:
                            yield chunk
                finally:
                    remote.close()

            filename = url_quote(item.get("name") or "download", safe="")
            headers = [
                ("Content-Type", "application/octet-stream"),
                ("Content-Disposition", f"attachment; filename*=UTF-8''{filename}"),
                ("Cache-Control", "no-store"),
                ("X-Content-Type-Options", "nosniff"),
            ]
            self._audit("download", location, item_id=item_id, item_name=item.get("name"))
            return request.make_response(generate(), headers=headers)
        except (AccessError, UserError):
            if remote is not None:
                remote.close()
            return request.make_response("Descarga no autorizada.", status=403)
        except http_requests.RequestException:
            if remote is not None:
                remote.close()
            _logger.exception("Microsoft download network error")
            return request.make_response("No se pudo descargar el documento.", status=502)

    @http.route("/microsoft/upload", type="http", auth="user", methods=["POST"], csrf=True)
    def upload_file(self, **post):
        location = None
        try:
            uploaded = post.get("file")
            if not uploaded or not uploaded.filename:
                return self._json({"status": "error", "message": "No se recibió archivo."}, 400)
            location, service = self._service("upload", post.get("location_id"))
            parent_id = post.get("parent_id") or None
            stream = uploaded.stream
            stream.seek(0, 2)
            size = stream.tell()
            stream.seek(0)
            if size <= 0:
                raise UserError("No se puede subir un archivo vacío.")
            result = service.upload_stream(uploaded.filename, stream, size, parent_id)
            self._audit("upload", location, item_id=result.get("id"),
                        item_name=result.get("name") or uploaded.filename)
            return self._json({"status": "ok", "item": result})
        except (AccessError, UserError) as exc:
            if location:
                self._audit("upload", location, status="error", message=str(exc))
            return self._json({"status": "error", "message": str(exc)}, 403 if isinstance(exc, AccessError) else 400)
        except Exception:
            _logger.exception("Error subiendo documento Microsoft")
            if location:
                self._audit("upload", location, status="error", message="Error interno")
            return self._json({"status": "error", "message": "Error interno durante la subida."}, 500)
