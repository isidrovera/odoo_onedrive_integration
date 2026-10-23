# -*- coding: utf-8 -*-
"""Controlador documental Microsoft 365 para Odoo 19.

Todas las operaciones están autorizadas en el servidor sobre una ubicación
concreta. Nunca se utilizan permisos de aplicación para omitir controles Odoo.
"""
import json
import logging
import hmac
import hashlib
import base64
import time
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

    # ------------------------------------------------------------
    # Crear documentos nuevos directamente en la biblioteca Microsoft 365.
    # Estas rutas se corresponden exactamente con DriveService de fase 1.
    # ------------------------------------------------------------

    @http.route("/microsoft/create_document", type="jsonrpc", auth="user")
    def create_document(self, name, doc_type, parent_id=None, location_id=None, content=""):
        """Crear documento válido en SharePoint/OneDrive, sin adjuntos Odoo.

        Tipos soportados son los declarados por DriveService.DOCUMENT_TYPES.
        La autorización de escritura se comprueba en controlador y servicio.
        """
        location = None
        try:
            location, service = self._service("upload", location_id)
            if not isinstance(doc_type, str) or doc_type not in service.DOCUMENT_TYPES:
                raise UserError("Tipo de documento no admitido.")
            if not isinstance(name, str) or not name.strip():
                raise UserError("Indique un nombre para el documento.")
            if not isinstance(content, str) or len(content.encode("utf-8")) > 2 * 1024 * 1024:
                raise UserError("El contenido de texto supera el límite de 2 MB.")
            _logger.info(
                "[MICROSOFT][CREATE_DOCUMENT][REQUEST] user=%s location=%s parent=%s type=%s",
                request.env.user.id, location.id, parent_id, doc_type,
            )
            result = service.create_document(name, doc_type, parent_id, content)
            self._audit(
                "upload", location, item_id=result.get("id"),
                item_name=result.get("name") or name,
                message="Documento nuevo: %s" % doc_type,
            )
            _logger.info(
                "[MICROSOFT][CREATE_DOCUMENT][OK] user=%s location=%s type=%s item=%s",
                request.env.user.id, location.id, doc_type, result.get("id"),
            )
            return result
        except (AccessError, UserError) as exc:
            _logger.warning(
                "[MICROSOFT][CREATE_DOCUMENT][DENIED] user=%s location=%s type=%s error=%s",
                request.env.user.id, location_id, str(doc_type)[:40], str(exc)[:250],
            )
            if location:
                self._audit("upload", location, status="error", message="Fallo al crear documento")
            raise
        except Exception:
            _logger.exception(
                "[MICROSOFT][CREATE_DOCUMENT][ERROR] user=%s location=%s type=%s",
                request.env.user.id, location_id, str(doc_type)[:40],
            )
            raise

    @http.route("/microsoft/create_link", type="jsonrpc", auth="user")
    def create_link(self, name, target_url, parent_id=None, location_id=None):
        """Crear un acceso directo .url; no equivale a un enlace nativo SharePoint."""
        location = None
        try:
            location, service = self._service("upload", location_id)
            _logger.info(
                "[MICROSOFT][CREATE_LINK][REQUEST] user=%s location=%s parent=%s",
                request.env.user.id, location.id, parent_id,
            )
            result = service.create_link_file(name, target_url, parent_id)
            self._audit(
                "upload", location, item_id=result.get("id"),
                item_name=result.get("name") or name,
                message="Vínculo .url creado",
            )
            _logger.info(
                "[MICROSOFT][CREATE_LINK][OK] user=%s location=%s item=%s",
                request.env.user.id, location.id, result.get("id"),
            )
            return result
        except (AccessError, UserError) as exc:
            _logger.warning(
                "[MICROSOFT][CREATE_LINK][DENIED] user=%s location=%s reason=%s",
                request.env.user.id, location_id, str(exc)[:250],
            )
            if location:
                self._audit("upload", location, status="error", message="Fallo al crear vínculo")
            raise
        except Exception:
            _logger.exception(
                "[MICROSOFT][CREATE_LINK][ERROR] user=%s location=%s",
                request.env.user.id, location_id,
            )
            raise

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

    @http.route("/microsoft/thumbnail/<string:item_id>", type="http", auth="user", methods=["GET"])
    def thumbnail(self, item_id, size="medium", location_id=None):
        """Proxy protegido de miniaturas, con diagnóstico de cada etapa.

        No registra URL firmadas, tokens ni contenido del archivo.
        No utiliza sudo para saltar permisos de la ubicación.
        """
        remote = None
        location = None
        user_id = request.env.user.id
        stage = "validate"
        _logger.info(
            "[MICROSOFT][THUMBNAIL][START] user=%s location=%s item=%s size=%s",
            user_id, location_id, item_id, size,
        )
        try:
            if size not in ("small", "medium", "large"):
                _logger.warning("[MICROSOFT][THUMBNAIL][INVALID_SIZE] size=%s", size)
                return Response(status=400)

            stage = "odoo_access"
            location, service = self._service("preview", location_id)
            _logger.info(
                "[MICROSOFT][THUMBNAIL][ACCESS_OK] user=%s location=%s item=%s",
                user_id, location.id, item_id,
            )

            stage = "graph_thumbnail"
            thumbnail_url = service.get_thumbnail_url(item_id, size)
            if not thumbnail_url:
                _logger.info(
                    "[MICROSOFT][THUMBNAIL][NOT_AVAILABLE] location=%s item=%s size=%s",
                    location.id, item_id, size,
                )
                return Response(status=404)

            stage = "download_thumbnail"
            # streaming evita alojar una respuesta arbitrariamente grande en memoria
            remote = self._temporary_get(thumbnail_url, stream=True, timeout=30)
            if remote.status_code != 200:
                _logger.warning(
                    "[MICROSOFT][THUMBNAIL][REMOTE_STATUS] location=%s item=%s status=%s",
                    location.id, item_id, remote.status_code,
                )
                return Response(status=502)

            mime = remote.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            # Solo formatos de imagen que el navegador puede mostrar sin riesgo de SVG activo.
            if mime not in ("image/jpeg", "image/png", "image/webp", "image/gif", "image/bmp"):
                _logger.warning(
                    "[MICROSOFT][THUMBNAIL][INVALID_MIME] location=%s item=%s mime=%s",
                    location.id, item_id, mime[:100],
                )
                return Response(status=415)

            limit = 10 * 1024 * 1024
            content_length = remote.headers.get("Content-Length")
            if content_length:
                try:
                    if int(content_length) > limit:
                        _logger.warning("[MICROSOFT][THUMBNAIL][TOO_LARGE] item=%s", item_id)
                        return Response(status=413)
                except (ValueError, TypeError):
                    pass
            chunks = []
            total = 0
            for chunk in remote.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > limit:
                    _logger.warning("[MICROSOFT][THUMBNAIL][TOO_LARGE] item=%s", item_id)
                    return Response(status=413)
                chunks.append(chunk)
            if not total:
                _logger.info("[MICROSOFT][THUMBNAIL][EMPTY] item=%s", item_id)
                return Response(status=404)

            _logger.info(
                "[MICROSOFT][THUMBNAIL][SUCCESS] location=%s item=%s bytes=%s mime=%s",
                location.id, item_id, total, mime,
            )
            return request.make_response(b"".join(chunks), headers=[
                ("Content-Type", mime),
                ("Cache-Control", "private, max-age=300"),
                ("X-Content-Type-Options", "nosniff"),
            ])
        except AccessError as exc:
            _logger.warning(
                "[MICROSOFT][THUMBNAIL][ACCESS_DENIED] stage=%s user=%s location=%s item=%s reason=%s",
                stage, user_id, location_id, item_id, str(exc)[:300],
            )
            return Response(status=403)
        except UserError as exc:
            _logger.warning(
                "[MICROSOFT][THUMBNAIL][SERVICE_ERROR] stage=%s user=%s location=%s item=%s reason=%s",
                stage, user_id, location_id, item_id, str(exc)[:300],
            )
            return Response(status=502)
        except http_requests.RequestException:
            _logger.exception(
                "[MICROSOFT][THUMBNAIL][NETWORK_ERROR] stage=%s user=%s location=%s item=%s",
                stage, user_id, location_id, item_id,
            )
            return Response(status=502)
        except Exception:
            _logger.exception(
                "[MICROSOFT][THUMBNAIL][UNEXPECTED_ERROR] stage=%s user=%s location=%s item=%s",
                stage, user_id, location_id, item_id,
            )
            return Response(status=500)
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

    # Sesión temporal firmada: vinculada al usuario, ubicación, URL y tamaño.
    @staticmethod
    def _proof_key():
        secret = request.env["ir.config_parameter"].sudo().get_param("database.secret")
        if not secret:
            raise UserError("La clave de seguridad de la base no está disponible.")
        return hashlib.sha256(secret.encode("utf-8")).digest()

    @classmethod
    def _sign_upload(cls, data):
        payload = base64.urlsafe_b64encode(json.dumps(data, separators=(",", ":")).encode()).decode().rstrip("=")
        signature = hmac.new(cls._proof_key(), payload.encode(), hashlib.sha256).hexdigest()
        return f"{payload}.{signature}"

    @classmethod
    def _check_upload(cls, proof, upload_url, location_id):
        try:
            payload, signature = proof.rsplit(".", 1)
            correct = hmac.new(cls._proof_key(), payload.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, correct):
                raise ValueError("firma")
            data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
            if (data["uid"] != request.env.user.id or data["location"] != int(location_id)
                or data["exp"] < time.time() or not hmac.compare_digest(
                    data["url_sha"], hashlib.sha256(upload_url.encode()).hexdigest())):
                raise ValueError("caducidad")
            return data
        except (KeyError, ValueError, TypeError, AttributeError) as exc:
            raise AccessError("La sesión de subida es inválida o ha caducado.") from exc

    @http.route("/microsoft/ensure_folder", type="jsonrpc", auth="user")
    def ensure_folder(self, name, parent_id=None, location_id=None):
        location, service = self._service("create_folder", location_id)
        result = service.ensure_folder(name, parent_id)
        self._audit("create_folder", location, item_id=result.get("id"), item_name=name)
        return result

    @http.route("/microsoft/upload_session", type="jsonrpc", auth="user")
    def upload_session(self, name, size, parent_id=None, location_id=None):
        location, service = self._service("upload", location_id)
        _logger.info("[MICROSOFT][UPLOAD][SESSION_START] user=%s location=%s name=%s bytes=%s parent=%s",
                     request.env.user.id, location.id, str(name)[:120], size, parent_id)
        session = service.start_chunk_upload(name, size, parent_id)
        _logger.info("[MICROSOFT][UPLOAD][SESSION_CREATED] user=%s location=%s bytes=%s",
                     request.env.user.id, location.id, session["size"])
        proof = self._sign_upload({
            "uid": request.env.user.id, "location": location.id,
            "size": session["size"], "exp": int(time.time()) + 12 * 3600,
            "url_sha": hashlib.sha256(session["upload_url"].encode()).hexdigest(),
        })
        return {"upload_url": session["upload_url"], "proof": proof}

    @http.route("/microsoft/upload_chunk", type="http", auth="user", methods=["POST"], csrf=True)
    def upload_chunk(self, **post):
        remote = None
        try:
            location_id = post.get("location_id")
            self._service("upload", location_id)
            url = DriveService._validate_temporary_url(post.get("upload_url"))
            proof = self._check_upload(post.get("proof"), url, location_id)
            start, end, total = (int(post.get(k)) for k in ("start", "end", "total"))
            uploaded = post.get("chunk")
            if not uploaded or total != proof["size"] or start < 0 or end <= start or end > total:
                raise UserError("Parámetros de fragmento inválidos.")
            size = end - start
            if size > 5 * 1024 * 1024 or (start % (320 * 1024)) or (end != total and size % (320 * 1024)):
                raise UserError("El fragmento incumple los tamaños admitidos por Microsoft Graph.")
            content = uploaded.stream.read(size + 1)
            if len(content) != size:
                raise UserError("El fragmento recibido está incompleto o supera su tamaño declarado.")
            remote = http_requests.put(url, data=content, headers={
                "Content-Length": str(size), "Content-Range": f"bytes {start}-{end-1}/{total}",
            }, timeout=180, allow_redirects=False)
            _logger.debug("[MICROSOFT][UPLOAD][CHUNK_RESPONSE] location=%s range=%s-%s status=%s",
                          location_id, start, end, remote.status_code)
            if remote.status_code not in (200, 201, 202):
                _logger.warning("[MICROSOFT] Fragmento rechazado status=%s", remote.status_code)
                return self._json({"status":"error", "message":f"Microsoft rechazó el fragmento ({remote.status_code})"}, 502)
            payload = remote.json()
            if remote.status_code in (200, 201):
                if not payload.get("id"):
                    raise UserError("Microsoft no confirmó el identificador del archivo.")
                location = self._location(location_id)
                self._audit("upload", location, item_id=payload["id"], item_name=payload.get("name"))
                _logger.info("[MICROSOFT][UPLOAD][COMPLETE] location=%s item=%s bytes=%s",
                             location.id, payload["id"], total)
                return self._json({"status": "ok", "item": payload})
            ranges = payload.get("nextExpectedRanges") or []
            next_offset = int(str(ranges[0]).split("-")[0]) if ranges else end
            return self._json({"status": "ok", "next_offset": next_offset})
        except (UserError, AccessError, TypeError, ValueError) as exc:
            _logger.warning("[MICROSOFT][UPLOAD][CHUNK_FAILED] user=%s location=%s error=%s",
                            request.env.user.id, post.get("location_id"), str(exc)[:300])
            return self._json({"status":"error", "message":str(exc)}, 403 if isinstance(exc, AccessError) else 400)
        except http_requests.RequestException:
            _logger.exception("[MICROSOFT] Error de transferencia de fragmento")
            return self._json({"status":"error", "message":"Error de red con Microsoft 365"}, 502)
        finally:
            if remote is not None:
                remote.close()

    @http.route("/microsoft/upload_status", type="jsonrpc", auth="user")
    def upload_status(self, upload_url, proof, location_id):
        self._service("upload", location_id)
        url = DriveService._validate_temporary_url(upload_url)
        self._check_upload(proof, url, location_id)
        remote = http_requests.get(url, timeout=30, allow_redirects=False)
        try:
            if remote.status_code != 200:
                raise UserError("No fue posible consultar la sesión de carga.")
            data = remote.json()
            ranges = data.get("nextExpectedRanges") or []
            if not ranges:
                raise UserError("La sesión no devolvió su posición actual.")
            return {"next_offset": int(str(ranges[0]).split("-")[0])}
        finally:
            remote.close()

    @http.route("/microsoft/upload_complete", type="jsonrpc", auth="user")
    def upload_complete(self, upload_url, proof, location_id):
        # No inventamos una finalización si Graph no ha devuelto un driveItem.
        self._service("upload", location_id)
        self._check_upload(proof, DriveService._validate_temporary_url(upload_url), location_id)
        raise UserError("Microsoft no confirmó el archivo final. Revise la carpeta antes de reintentar.")

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
