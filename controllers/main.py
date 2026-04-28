import logging
from odoo import http
from odoo.http import request
from odoo.exceptions import UserError

from odoo.addons.odoo_onedrive_integration.services.graph_service import GraphService

_logger = logging.getLogger(__name__)


class OneDriveController(http.Controller):

    # ---------------------------------------
    # LOGIN (redirige a Microsoft)
    # ---------------------------------------
    @http.route('/onedrive/login/<int:account_id>', type='http', auth='user')
    def onedrive_login(self, account_id):
        account = request.env['onedrive.account'].sudo().browse(account_id)

        if not account.exists():
            return request.not_found()

        return request.redirect(account.get_auth_url())

    # ---------------------------------------
    # CALLBACK (OAuth)
    # ---------------------------------------
    @http.route('/onedrive/callback', type='http', auth='user')
    def onedrive_callback(self, **kwargs):

        code = kwargs.get("code")

        if not code:
            return "Error: no se recibió código"

        account = request.env['onedrive.account'].sudo().search([], limit=1)

        if not account:
            return "No hay cuenta configurada"

        try:
            account.exchange_code_for_token(code)
        except Exception as e:
            _logger.exception("OAuth error")
            return f"Error autenticando: {str(e)}"

        return """
        <h2>✔ OneDrive conectado correctamente</h2>
        <p>Ya puedes cerrar esta ventana.</p>
        """

    # ---------------------------------------
    # LISTAR ARCHIVOS
    # ---------------------------------------
    @http.route('/onedrive/list', type='json', auth='user')
    def list_files(self, account_id=None, parent_id=None):

        account = self._get_account(account_id)
        service = GraphService(account)

        if parent_id:
            return service.list_children(parent_id)

        return service.list_root()

    # ---------------------------------------
    # CREAR CARPETA
    # ---------------------------------------
    @http.route('/onedrive/create_folder', type='json', auth='user')
    def create_folder(self, name, parent_id=None, account_id=None):

        account = self._get_account(account_id)
        service = GraphService(account)

        return service.create_folder(name, parent_id)

    # ---------------------------------------
    # ELIMINAR
    # ---------------------------------------
    @http.route('/onedrive/delete', type='json', auth='user')
    def delete_item(self, item_id, account_id=None):

        account = self._get_account(account_id)
        service = GraphService(account)

        service.delete_item(item_id)

        return {"status": "deleted"}

    # ---------------------------------------
    # DESCARGAR
    # ---------------------------------------
    @http.route('/onedrive/download/<string:item_id>', type='http', auth='user')
    def download_file(self, item_id, account_id=None):

        account = self._get_account(account_id)
        service = GraphService(account)

        download_url = service.get_download_url(item_id)

        if not download_url:
            return "No se pudo obtener enlace"

        return request.redirect(download_url)

    # ---------------------------------------
    # SUBIR ARCHIVO
    # ---------------------------------------
    @http.route('/onedrive/upload', type='http', auth='user', methods=['POST'], csrf=False)
    def upload_file(self, **post):

        file = post.get('file')
        account_id = post.get('account_id')

        if not file:
            return "No se envió archivo"

        account = self._get_account(account_id)
        service = GraphService(account)

        service.upload_file(file.filename, file.read())

        return request.make_response("OK")

    # ---------------------------------------
    # HELPER
    # ---------------------------------------
    def _get_account(self, account_id):

        if account_id:
            account = request.env['onedrive.account'].sudo().browse(int(account_id))
        else:
            account = request.env['onedrive.account'].sudo().search([], limit=1)

        if not account:
            raise UserError("No hay cuenta configurada")

        return account