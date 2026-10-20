# -*- coding: utf-8 -*-
"""Rutas OAuth Microsoft 365 para Odoo 19.

Compatibilidad con las rutas históricas de OneDrive y con las conexiones
individuales. Los estados, tiempos de expiración y tokens se gestionan en
los modelos correspondientes, nunca en parámetros de la URL de retorno.
"""
import logging

from odoo import http
from odoo.exceptions import AccessError, UserError
from odoo.http import request
from werkzeug.exceptions import NotFound
from werkzeug.utils import escape

_logger = logging.getLogger(__name__)


class MicrosoftAuthController(http.Controller):
    """Autenticación delegada del administrador y de los usuarios Odoo."""

    @staticmethod
    def _error_page(message, status=400):
        # No reflejar HTML ni códigos/tokens recibidos como parámetros OAuth.
        safe_message = escape(str(message or "Error de autenticación Microsoft.")[:600])
        html = (
            '<!doctype html><html lang="es"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>Conexión Microsoft 365</title></head><body>'
            '<main style="max-width:620px;margin:65px auto;font-family:system-ui,'
            'sans-serif;padding:20px"><h2>No se pudo conectar Microsoft 365</h2>'
            f'<p>{safe_message}</p><p>Regrese a Odoo e intente la conexión nuevamente.</p>'
            '<a href="/web">Volver a Odoo</a></main></body></html>'
        )
        return request.make_response(
            html,
            headers=[
                ("Content-Type", "text/html; charset=utf-8"),
                ("Cache-Control", "no-store, no-cache, must-revalidate"),
                ("Pragma", "no-cache"),
                ("X-Content-Type-Options", "nosniff"),
                ("Referrer-Policy", "no-referrer"),
            ],
            status=status,
        )

    @staticmethod
    def _require_admin():
        if not request.env.user.has_group(
            "odoo_onedrive_integration.group_microsoft_admin"
        ):
            raise AccessError("Solo un administrador Microsoft 365 puede conectar el tenant.")

    @staticmethod
    def _validate_state(value):
        # Un estado es una cadena aleatoria opaca: no usar contenido recibido
        # para armar dominios dinámicos, redirecciones ni mensajes de diagnóstico.
        return (
            isinstance(value, str)
            and 24 <= len(value) <= 256
            and value.startswith(("t.", "u."))
            and all(ch.isascii() and (ch.isalnum() or ch in "-_.~") for ch in value)
        )

    @http.route(
        "/onedrive/login/<int:account_id>",
        type="http", auth="user", methods=["GET"], website=False,
    )
    def legacy_login(self, account_id, **kwargs):
        """Conservar el acceso OAuth antiguo del administrador."""
        try:
            self._require_admin()
            account = request.env["onedrive.account"].browse(account_id).exists()
            if not account:
                return request.not_found()
            url = account.get_auth_url()
            return request.redirect(url, code=302)
        except (AccessError, UserError) as error:
            _logger.warning(
                "OAuth Microsoft: inicio administrativo rechazado user=%s tenant=%s",
                request.env.uid, account_id,
            )
            return self._error_page(error, status=403)
        except Exception:
            _logger.exception("OAuth Microsoft: error en inicio administrativo")
            return self._error_page("No se pudo iniciar la conexión administrativa.", status=500)

    @http.route(
        "/microsoft/connect/<int:connection_id>",
        type="http", auth="user", methods=["GET"], website=False,
    )
    def connect_user(self, connection_id, **kwargs):
        """El usuario solo puede iniciar OAuth sobre su propia conexión."""
        try:
            connection = request.env["microsoft.user.connection"].sudo().browse(
                connection_id
            ).exists()
            if not connection or connection.user_id.id != request.env.uid:
                return request.not_found()
            if not connection.tenant_id or not connection.tenant_id.active:
                return self._error_page("El tenant Microsoft está desactivado.", status=403)
            # with_user desactiva superusuario para el usuario ordinario.
            url = connection.with_user(request.env.user).get_auth_url()
            return request.redirect(url, code=302)
        except (AccessError, UserError) as error:
            _logger.warning(
                "OAuth Microsoft: inicio personal rechazado user=%s connection=%s",
                request.env.uid, connection_id,
            )
            return self._error_page(error, status=403)
        except Exception:
            _logger.exception("OAuth Microsoft: error en inicio personal")
            return self._error_page("No se pudo iniciar la conexión personal.", status=500)

    @http.route(
        ["/microsoft/oauth/callback", "/onedrive/callback"],
        type="http", auth="user", methods=["GET"], csrf=False, website=False,
    )
    def oauth_callback(self, **kwargs):
        """Canjea el código exclusivamente para la sesión que inició OAuth.

        El modelo comprueba el estado aleatorio, su expiración y, para el
        administrador, el propietario original de la operación.
        """
        microsoft_error = kwargs.get("error")
        if microsoft_error:
            # Evitar reflejar error_description: proveedores o intermediarios
            # podrían insertar contenido sensible en esa cadena.
            _logger.warning(
                "OAuth Microsoft rechazado por proveedor: user=%s error=%s",
                request.env.uid, str(microsoft_error)[:80],
            )
            return self._error_page(
                "Microsoft rechazó o canceló la autorización. Reinicie la conexión desde Odoo."
            )

        state = kwargs.get("state")
        code = kwargs.get("code")
        if not self._validate_state(state) or not isinstance(code, str) or not (1 <= len(code) <= 8192):
            return self._error_page("La respuesta de Microsoft está incompleta o es inválida.")

        try:
            if state.startswith("t."):
                self._require_admin()
                account = request.env["onedrive.account"].sudo().search(
                    [("oauth_state", "=", state)], limit=1
                )
                if not account or account.oauth_state_user_id.id != request.env.uid:
                    raise AccessError("La solicitud administrativa ya no es válida para esta sesión.")
                account.with_user(request.env.user).exchange_code_for_token(code, state)
                _logger.info("OAuth Microsoft: tenant conectado user=%s tenant=%s", request.env.uid, account.id)
            else:
                connection = request.env["microsoft.user.connection"].sudo().search(
                    [("oauth_state", "=", state), ("user_id", "=", request.env.uid)],
                    limit=1,
                )
                if not connection:
                    raise AccessError("La solicitud personal ya no es válida para esta sesión.")
                connection.with_user(request.env.user).exchange_code_for_token(code, state)
                _logger.info(
                    "OAuth Microsoft: conexión personal completada user=%s connection=%s",
                    request.env.uid, connection.id,
                )
        except (AccessError, UserError) as error:
            _logger.warning("OAuth Microsoft: callback rechazado user=%s: %s", request.env.uid, type(error).__name__)
            return self._error_page(error)
        except Exception:
            _logger.exception("OAuth Microsoft: error inesperado en callback user=%s", request.env.uid)
            return self._error_page("No se pudo completar la autenticación. Vuelva a intentarlo.", status=500)

        response = request.redirect("/web", code=303)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response
