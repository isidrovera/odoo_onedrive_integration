# controllers/onedrive_controller.py
import logging
import json
import requests as http_requests
from werkzeug.urls import url_quote

from odoo import http
from odoo.http import request, Response
from odoo.exceptions import UserError
from odoo.addons.odoo_onedrive_integration.services.graph_service import GraphService

_logger = logging.getLogger(__name__)


class OneDriveController(http.Controller):

    # ---------------------------------------
    # AUTH
    # ---------------------------------------
    @http.route('/onedrive/login/<int:account_id>', type='http', auth='user')
    def onedrive_login(self, account_id):
        account = request.env['onedrive.account'].sudo().browse(account_id)
        return request.redirect(account.get_auth_url())

    @http.route('/onedrive/callback', type='http', auth='user')
    def onedrive_callback(self, **kwargs):
        code = kwargs.get("code")
        account = request.env['onedrive.account'].sudo().search([], limit=1)
        try:
            account.exchange_code_for_token(code)
        except Exception as e:
            return f"<h2>❌ Error: {str(e)}</h2>"
        return """
        <html><body style="font-family:sans-serif;text-align:center;padding:50px;">
            <h1 style="color:#28a745;">✔ Conectado correctamente</h1>
            <p>Ya puedes cerrar esta ventana y volver a Odoo.</p>
            <script>setTimeout(()=>window.close(), 2000);</script>
        </body></html>
        """

    # ---------------------------------------
    # ACCOUNTS
    # ---------------------------------------
    @http.route('/onedrive/accounts', type='json', auth='user')
    def get_accounts(self):
        accounts = request.env['onedrive.account'].sudo().search([('active', '=', True)])
        return [{
            'id': a.id,
            'name': a.name,
            'connected': bool(a.access_token),
        } for a in accounts]

    # ---------------------------------------
    # LIST
    # ---------------------------------------
    @http.route('/onedrive/list', type='json', auth='user')
    def list_files(self, parent_id=None, account_id=None):
        account = self._get_account(account_id)
        service = GraphService(account)
        if parent_id:
            return service.list_children(parent_id)
        return service.list_root()

    # ---------------------------------------
    # SEARCH
    # ---------------------------------------
    @http.route('/onedrive/search', type='json', auth='user')
    def search_files(self, query, account_id=None):
        account = self._get_account(account_id)
        service = GraphService(account)
        return service.search(query)

    # ---------------------------------------
    # GET ITEM
    # ---------------------------------------
    @http.route('/onedrive/item', type='json', auth='user')
    def get_item(self, item_id, account_id=None):
        account = self._get_account(account_id)
        service = GraphService(account)
        return service.get_item(item_id)

    # ---------------------------------------
    # CREATE FOLDER
    # ---------------------------------------
    @http.route('/onedrive/create_folder', type='json', auth='user')
    def create_folder(self, name, parent_id=None, account_id=None):
        account = self._get_account(account_id)
        service = GraphService(account)
        return service.create_folder(name, parent_id)

    # ---------------------------------------
    # RENAME
    # ---------------------------------------
    @http.route('/onedrive/rename', type='json', auth='user')
    def rename_item(self, item_id, new_name, account_id=None):
        account = self._get_account(account_id)
        service = GraphService(account)
        return service.rename_item(item_id, new_name)

    # ---------------------------------------
    # MOVE
    # ---------------------------------------
    @http.route('/onedrive/move', type='json', auth='user')
    def move_item(self, item_id, target_parent_id, account_id=None):
        account = self._get_account(account_id)
        service = GraphService(account)
        return service.move_item(item_id, target_parent_id)

    # ---------------------------------------
    # COPY
    # ---------------------------------------
    @http.route('/onedrive/copy', type='json', auth='user')
    def copy_item(self, item_id, target_parent_id, new_name=None, account_id=None):
        account = self._get_account(account_id)
        service = GraphService(account)
        return service.copy_item(item_id, target_parent_id, new_name)

    # ---------------------------------------
    # DELETE
    # ---------------------------------------
    @http.route('/onedrive/delete', type='json', auth='user')
    def delete_item(self, item_id, account_id=None):
        account = self._get_account(account_id)
        service = GraphService(account)
        service.delete_item(item_id)
        return {"status": "deleted"}

    # ---------------------------------------
    # SHARE
    # ---------------------------------------
    @http.route('/onedrive/share', type='json', auth='user')
    def share_item(self, item_id, share_type='view', scope='anonymous', account_id=None):
        account = self._get_account(account_id)
        service = GraphService(account)
        return service.create_share_link(item_id, share_type, scope)

    # ---------------------------------------
    # THUMBNAIL (streaming en vez de redirect)
    # ---------------------------------------
    @http.route('/onedrive/thumbnail/<string:item_id>', type='http', auth='user')
    def get_thumbnail(self, item_id, size='medium', account_id=None):
        account = self._get_account(account_id)
        service = GraphService(account)
        try:
            url = service.get_thumbnail_url(item_id, size)
            if not url:
                return Response(status=404)
            r = http_requests.get(url, stream=True, timeout=30)
            if r.status_code >= 400:
                return Response(status=r.status_code)
            return request.make_response(
                r.content,
                headers=[
                    ('Content-Type', r.headers.get('Content-Type', 'image/jpeg')),
                    ('Cache-Control', 'public, max-age=3600'),
                ],
            )
        except Exception as e:
            _logger.warning("Thumbnail error: %s", e)
            return Response(status=404)

    # ---------------------------------------
    # PREVIEW (embed)
    # ---------------------------------------
    @http.route('/onedrive/preview', type='json', auth='user')
    def preview_item(self, item_id, account_id=None):
        account = self._get_account(account_id)
        service = GraphService(account)
        return service.get_preview_url(item_id)

    # ---------------------------------------
    # DOWNLOAD FILE (streaming directo, sin redirect)
    # ---------------------------------------
    @http.route('/onedrive/download/<string:item_id>', type='http', auth='user')
    def download_file(self, item_id, account_id=None):
        account = self._get_account(account_id)
        service = GraphService(account)
        try:
            item = service.get_item(item_id)
            if "folder" in item:
                return request.make_response("Use download_folder para carpetas", status=400)

            download_url = item.get("@microsoft.graph.downloadUrl")
            filename = item.get("name", "download")
            if not download_url:
                return request.make_response("Archivo no descargable", status=404)

            r = http_requests.get(download_url, stream=True, timeout=120)
            if r.status_code >= 400:
                return request.make_response(f"Error Microsoft: {r.status_code}", status=r.status_code)

            def generate():
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        yield chunk

            safe_filename = url_quote(filename)
            headers = [
                ('Content-Type', r.headers.get('Content-Type', 'application/octet-stream')),
                ('Content-Disposition', f"attachment; filename*=UTF-8''{safe_filename}"),
                ('Cache-Control', 'no-cache'),
            ]
            cl = r.headers.get('Content-Length')
            if cl:
                headers.append(('Content-Length', cl))

            return request.make_response(generate(), headers=headers)

        except Exception as e:
            _logger.exception("Download error")
            return request.make_response(f"Error: {str(e)}", status=500)

    # ---------------------------------------
    # DOWNLOAD FOLDER (streaming zip desde Graph)
    # ---------------------------------------
    @http.route('/onedrive/download_folder/<string:item_id>', type='http', auth='user')
    def download_folder(self, item_id, account_id=None):
        account = self._get_account(account_id)
        service = GraphService(account)
        try:
            item = service.get_item(item_id)
            folder_name = item.get("name", "folder")
            token = account.get_valid_token()
            url = f"{service.base_url}/me/drive/items/{item_id}/content"

            r = http_requests.get(
                url,
                headers={"Authorization": f"Bearer {token}"},
                stream=True,
                timeout=300,
                allow_redirects=True,
            )
            if r.status_code >= 400:
                return request.make_response(f"Error: {r.status_code}", status=r.status_code)

            def generate():
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        yield chunk

            safe_filename = url_quote(f"{folder_name}.zip")
            headers = [
                ('Content-Type', 'application/zip'),
                ('Content-Disposition', f"attachment; filename*=UTF-8''{safe_filename}"),
                ('Cache-Control', 'no-cache'),
            ]
            return request.make_response(generate(), headers=headers)

        except Exception as e:
            _logger.exception("Download folder error")
            return request.make_response(f"Error: {str(e)}", status=500)

    # ---------------------------------------
    # UPLOAD
    # ---------------------------------------
    @http.route('/onedrive/upload', type='http', auth='user', methods=['POST'], csrf=False)
    def upload_file(self, **post):
        try:
            file = post.get('file')
            account_id = post.get('account_id')
            parent_id = post.get('parent_id') or None
            account = self._get_account(account_id)
            service = GraphService(account)
            result = service.upload_file(file.filename, file.read(), parent_id)
            return Response(json.dumps({"status": "ok", "item": result}),
                            content_type='application/json')
        except Exception as e:
            _logger.exception("Upload error")
            return Response(json.dumps({"status": "error", "message": str(e)}),
                            content_type='application/json', status=500)

    # ---------------------------------------
    # HELPER
    # ---------------------------------------
    def _get_account(self, account_id):
        if account_id:
            return request.env['onedrive.account'].sudo().browse(int(account_id))
        return request.env['onedrive.account'].sudo().search([], limit=1)