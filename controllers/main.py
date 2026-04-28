import logging
from odoo import http
from odoo.http import request
from odoo.exceptions import UserError

from odoo.addons.odoo_onedrive_integration.services.graph_service import GraphService

_logger = logging.getLogger(__name__)


class OneDriveController(http.Controller):

    # ---------------------------------------
    # LOGIN
    # ---------------------------------------
    @http.route('/onedrive/login/<int:account_id>', type='http', auth='user')
    def onedrive_login(self, account_id):
        account = request.env['onedrive.account'].sudo().browse(account_id)
        return request.redirect(account.get_auth_url())

    # ---------------------------------------
    # CALLBACK
    # ---------------------------------------
    @http.route('/onedrive/callback', type='http', auth='user')
    def onedrive_callback(self, **kwargs):

        code = kwargs.get("code")

        account = request.env['onedrive.account'].sudo().search([], limit=1)

        try:
            account.exchange_code_for_token(code)
        except Exception as e:
            return f"Error autenticando: {str(e)}"

        return "<h2>✔ Conectado correctamente</h2>"

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
    # CREATE FOLDER
    # ---------------------------------------
    @http.route('/onedrive/create_folder', type='json', auth='user')
    def create_folder(self, name, parent_id=None, account_id=None):

        account = self._get_account(account_id)
        service = GraphService(account)

        return service.create_folder(name, parent_id)

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
    # DOWNLOAD (CORREGIDO)
    # ---------------------------------------
    @http.route('/onedrive/download/<string:item_id>', type='http', auth='user')
    def download_file(self, item_id, account_id=None):

        account = self._get_account(account_id)
        service = GraphService(account)

        try:
            download_url = service.get_download_url(item_id)
        except Exception as e:
            return f"Error: {str(e)}"

        return request.redirect(download_url)

    # ---------------------------------------
    # UPLOAD
    # ---------------------------------------
    @http.route('/onedrive/upload', type='http', auth='user', methods=['POST'], csrf=False)
    def upload_file(self, **post):

        file = post.get('file')
        account_id = post.get('account_id')

        account = self._get_account(account_id)
        service = GraphService(account)

        service.upload_file(file.filename, file.read())

        return request.make_response("OK")

    # ---------------------------------------
    # HELPER
    # ---------------------------------------
    def _get_account(self, account_id):

        if account_id:
            return request.env['onedrive.account'].sudo().browse(int(account_id))

        return request.env['onedrive.account'].sudo().search([], limit=1)