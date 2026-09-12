# -*- coding: utf-8 -*-
import logging

from odoo import http
from odoo.http import request
from werkzeug.utils import escape

_logger = logging.getLogger(__name__)


class MicrosoftAuthController(http.Controller):

    @http.route("/onedrive/login/<int:account_id>", type="http", auth="user")
    def legacy_login(self, account_id, **kwargs):
        account = request.env["onedrive.account"].browse(account_id).exists()
        return request.redirect(account.get_auth_url())

    @http.route("/microsoft/connect/<int:connection_id>", type="http", auth="user")
    def connect_user(self, connection_id, **kwargs):
        connection = request.env["microsoft.user.connection"].browse(connection_id).exists()
        if not connection or connection.user_id != request.env.user:
            return request.not_found()
        return request.redirect(connection.get_auth_url())

    @http.route(["/microsoft/oauth/callback", "/onedrive/callback"], type="http", auth="user", csrf=False)
    def oauth_callback(self, **kwargs):
        state = kwargs.get("state")
        code = kwargs.get("code")
        error = kwargs.get("error_description") or kwargs.get("error")
        if error:
            return request.make_response(
                f"<h2>Microsoft rechazó la conexión</h2><p>{escape(error)}</p>",
                headers=[("Content-Type", "text/html; charset=utf-8")], status=400,
            )
        try:
            if not state or not code:
                raise ValueError("Microsoft no devolvió el código o estado OAuth.")
            if state.startswith("t."):
                record = request.env["onedrive.account"].sudo().search([("oauth_state", "=", state)], limit=1)
                if not record:
                    raise ValueError("La solicitud administrativa no existe o ya fue utilizada.")
                record.with_user(request.env.user).exchange_code_for_token(code, state)
            elif state.startswith("u."):
                record = request.env["microsoft.user.connection"].sudo().search([("oauth_state", "=", state)], limit=1)
                if not record or record.user_id != request.env.user:
                    raise ValueError("La solicitud personal no corresponde al usuario conectado.")
                record.with_user(request.env.user).exchange_code_for_token(code, state)
            else:
                raise ValueError("Estado OAuth no reconocido.")
        except Exception as exc:
            _logger.exception("Error finalizando OAuth Microsoft")
            return request.make_response(
                f"<h2>No se pudo conectar Microsoft</h2><p>{escape(str(exc))}</p>",
                headers=[("Content-Type", "text/html; charset=utf-8")], status=400,
            )
        return request.redirect("/web")
