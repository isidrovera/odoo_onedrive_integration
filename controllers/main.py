# -*- coding: utf-8 -*-
import json
import logging
from urllib.parse import quote as url_quote

import requests as http_requests

from odoo import http
from odoo.exceptions import UserError
from odoo.http import Response, request

from ..services.drive_service import DriveService

_logger = logging.getLogger(__name__)


class MicrosoftDocumentsController(http.Controller):

    def _location(self, location_id=None):
        if not location_id:
            location = request.env["microsoft.storage.location"].search([], limit=1)
        else:
            try:
                location = request.env["microsoft.storage.location"].browse(int(location_id)).exists()
            except (TypeError, ValueError):
                location = request.env["microsoft.storage.location"]
        if not location:
            raise UserError("No existe una ubicación documental autorizada.")
        return location

    def _service(self, operation, location_id=None):
        location = self._location(location_id)
        location.check_operation(operation, request.env.user)
        return location, DriveService(location, request.env.user)

    def _audit(self, operation, location, status="success", **values):
        request.env["microsoft.audit.log"]._log(
            operation, status=status, tenant_id=location.tenant_id.id, location_id=location.id, **values
        )

    @http.route("/microsoft/locations", type="jsonrpc", auth="user")
    def locations(self):
        result = []
        for location in request.env["microsoft.storage.location"].search([("active", "=", True)]):
            role = location.access_level(request.env.user)
            result.append({
                "id": location.id,
                "name": location.name,
                "type": location.location_type,
                "site_name": location.site_id.name or "",
                "library_name": location.library_id.name or "",
                "web_url": location.web_url or location.library_id.web_url or location.site_id.web_url or "",
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
        self._audit("search", location, message=query)
        return result

    @http.route("/microsoft/item", type="jsonrpc", auth="user")
    def get_item(self, item_id, location_id=None):
        _location, service = self._service("properties", location_id)
        return service.get_item(item_id)

    @http.route("/microsoft/create_folder", type="jsonrpc", auth="user")
    def create_folder(self, name, parent_id=None, location_id=None):
        location, service = self._service("create_folder", location_id)
        result = service.create_folder(name, parent_id)
        self._audit("create_folder", location, item_id=result.get("id"), item_name=result.get("name"))
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
        _location, service = self._service("preview", location_id)
        return service.get_preview_url(item_id)

    @http.route("/microsoft/thumbnail/<string:item_id>", type="http", auth="user")
    def thumbnail(self, item_id, size="medium", location_id=None):
        try:
            _location, service = self._service("preview", location_id)
            url = service.get_thumbnail_url(item_id, size)
            if not url:
                return Response(status=404)
            remote = http_requests.get(url, timeout=30)
            if remote.status_code >= 400:
                return Response(status=remote.status_code)
            return request.make_response(remote.content, headers=[
                ("Content-Type", remote.headers.get("Content-Type", "image/jpeg")),
                ("Cache-Control", "private, max-age=900"),
            ])
        except Exception:
            return Response(status=404)

    @http.route("/microsoft/download/<string:item_id>", type="http", auth="user")
    def download_file(self, item_id, location_id=None):
        try:
            location, service = self._service("download", location_id)
            download_url, item = service.get_download_url(item_id)
            remote = http_requests.get(download_url, stream=True, timeout=120)
            if remote.status_code >= 400:
                return request.make_response("Microsoft rechazó la descarga.", status=remote.status_code)

            def generate():
                try:
                    for chunk in remote.iter_content(chunk_size=64 * 1024):
                        if chunk:
                            yield chunk
                finally:
                    remote.close()

            self._audit("download", location, item_id=item_id, item_name=item.get("name"))
            headers = [
                ("Content-Type", remote.headers.get("Content-Type", "application/octet-stream")),
                ("Content-Disposition", f"attachment; filename*=UTF-8''{url_quote(item.get('name', 'download'))}"),
                ("Cache-Control", "no-store"),
            ]
            return request.make_response(generate(), headers=headers)
        except Exception as error:
            _logger.exception("Error descargando documento Microsoft")
            return request.make_response(str(error), status=500)

    @http.route("/microsoft/upload", type="http", auth="user", methods=["POST"], csrf=True)
    def upload_file(self, **post):
        location = None
        try:
            uploaded = post.get("file")
            if not uploaded or not uploaded.filename:
                return Response(json.dumps({"status": "error", "message": "No se recibió archivo."}), status=400, content_type="application/json")
            location, service = self._service("upload", post.get("location_id"))
            parent_id = post.get("parent_id") or None
            stream = uploaded.stream
            stream.seek(0, 2)
            size = stream.tell()
            stream.seek(0)
            result = service.upload_stream(uploaded.filename, stream, size, parent_id)
            self._audit("upload", location, item_id=result.get("id"), item_name=result.get("name") or uploaded.filename)
            return Response(json.dumps({"status": "ok", "item": result}), content_type="application/json")
        except Exception as error:
            _logger.exception("Error subiendo documento Microsoft")
            if location:
                self._audit("upload", location, status="error", message=str(error))
            return Response(json.dumps({"status": "error", "message": str(error)}), status=500, content_type="application/json")
