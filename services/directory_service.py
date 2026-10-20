# -*- coding: utf-8 -*-
"""Microsoft Entra ID (Graph): invitados, grupos y sincronización con Odoo 19.

Los permisos de aplicación son amplios: este servicio debe invocarse únicamente
mediante acciones de servidor que comprueben el rol administrativo correspondiente.
"""
import logging
import re
from urllib.parse import quote

from odoo import fields
from odoo.exceptions import UserError

from .graph_client import GraphClient

_logger = logging.getLogger(__name__)


class DirectoryService:
    PAGE_SIZE = 100
    MAX_SYNC_GROUPS = 5000
    _GUID = re.compile(r"^[a-fA-F0-9-]{32,36}$")

    def __init__(self, tenant):
        tenant.ensure_one()
        self.tenant = tenant
        self.env = tenant.env
        self.client = GraphClient(
            token_provider=lambda: tenant.get_app_token("graph"),
            env=self.env,
        )

    @staticmethod
    def _escape(value):
        """Escapar literales OData (no interpolar filtros sin escapado)."""
        return str(value or "").replace("'", "''")

    @staticmethod
    def _segment(value):
        value = str(value or "").strip()
        if not value or len(value) > 512 or any(ord(c) < 32 for c in value):
            raise UserError("Identificador de Microsoft Entra no válido.")
        return quote(value, safe="")

    @staticmethod
    def _email(value):
        value = str(value or "").strip().lower()
        if not value or len(value) > 254 or "@" not in value or any(c.isspace() for c in value):
            raise UserError("Debe indicar un correo electrónico válido.")
        return value

    @staticmethod
    def _principal(data):
        return data.get("mail") or data.get("userPrincipalName") or data.get("otherMails", [None])[0]

    def find_user_by_email(self, email):
        """Buscar por correo directo, UPN u otras direcciones de un invitado B2B.

        Graph no siempre permite filtros `otherMails/any` en todos los tenants;
        por eso se usa primero el filtro simple y después el alternativo.
        """
        email = self._email(email)
        filt = "mail eq '%s' or userPrincipalName eq '%s'" % (
            self._escape(email), self._escape(email)
        )
        data = self.client.request("GET", "/users", params={
            "$filter": filt,
            "$select": "id,displayName,mail,userPrincipalName,userType,externalUserState,accountEnabled,otherMails",
            "$top": "20",
        })
        matches = data.get("value", []) if isinstance(data, dict) else []
        for user in matches:
            addresses = [user.get("mail"), user.get("userPrincipalName")]
            addresses += user.get("otherMails") or []
            if email in {str(address).lower() for address in addresses if address}:
                return user

        # Para invitados existentes #EXT#, el correo puede vivir en otherMails.
        try:
            data = self.client.request("GET", "/users", params={
                "$filter": "otherMails/any(x:x eq '%s')" % self._escape(email),
                "$select": "id,displayName,mail,userPrincipalName,userType,externalUserState,accountEnabled,otherMails",
                "$top": "20",
                "$count": "true",
            }, headers={"ConsistencyLevel": "eventual"})
        except UserError:
            return None
        for user in data.get("value", []):
            addresses = [user.get("mail"), user.get("userPrincipalName")]
            addresses += user.get("otherMails") or []
            if email in {str(address).lower() for address in addresses if address}:
                return user
        return None

    def get_user(self, user_id):
        return self.client.request("GET", "/users/%s" % self._segment(user_id), expected=(404,))

    def invite_guest(self, email, display_name, send_message=True):
        email = self._email(email)
        if not str(display_name or "").strip():
            raise UserError("Indique el nombre del invitado.")
        existing = self.find_user_by_email(email)
        if existing and existing.get("id"):
            return {"invitedUser": existing, "inviteRedeemUrl": None, "existingUser": True}
        base_url = self.env["ir.config_parameter"].sudo().get_param("web.base.url")
        redirect = self.tenant.invite_redirect_url or (base_url.rstrip("/") + "/web" if base_url else False)
        if not redirect or not redirect.lower().startswith("https://"):
            raise UserError("Configure una URL HTTPS válida para redirigir al invitado.")
        payload = {
            "invitedUserEmailAddress": email,
            "invitedUserDisplayName": str(display_name).strip(),
            "inviteRedirectUrl": redirect,
            "sendInvitationMessage": bool(send_message),
        }
        return self.client.request("POST", "/invitations", json=payload)

    def create_group(self, group):
        group.ensure_one()
        if group.tenant_id != self.tenant:
            raise UserError("El grupo no pertenece al tenant configurado.")
        if group.group_type == "readonly":
            raise UserError("Este grupo solo permite sincronización de lectura.")
        alias = (group.mail_nickname or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9._-]+", alias):
            raise UserError("Alias Microsoft 365 no válido.")
        is_m365 = group.group_type == "microsoft365"
        payload = {
            "displayName": group.name,
            "description": group.description or "",
            "mailEnabled": is_m365,
            "mailNickname": alias,
            "securityEnabled": not is_m365,
            "groupTypes": ["Unified"] if is_m365 else [],
        }
        if is_m365:
            payload["visibility"] = "Public" if group.visibility == "public" else "Private"
        owners = sorted({guest.entra_user_id for guest in group.owner_ids if guest.entra_user_id})
        members = sorted({guest.entra_user_id for guest in group.member_ids if guest.entra_user_id})
        if is_m365 and not owners:
            raise UserError("Un grupo Microsoft 365 requiere al menos un propietario.")
        if owners:
            payload["owners@odata.bind"] = [
                "https://graph.microsoft.com/v1.0/users/%s" % self._segment(uid) for uid in owners
            ]
        if members:
            payload["members@odata.bind"] = [
                "https://graph.microsoft.com/v1.0/users/%s" % self._segment(uid) for uid in members
            ]
        return self.client.request("POST", "/groups", json=payload)

    def update_group(self, group):
        group.ensure_one()
        if not group.graph_id or group.tenant_id != self.tenant:
            raise UserError("El grupo debe existir en el tenant Microsoft seleccionado.")
        if group.group_type == "readonly":
            raise UserError("Este grupo está configurado como solo lectura.")
        payload = {"displayName": group.name, "description": group.description or ""}
        if group.group_type == "microsoft365":
            payload["visibility"] = "Public" if group.visibility == "public" else "Private"
        return self.client.request("PATCH", "/groups/%s" % self._segment(group.graph_id), json=payload)

    def delete_group(self, group_id):
        return self.client.request("DELETE", "/groups/%s" % self._segment(group_id))

    def add_group_member(self, group_id, user_id, owner=False):
        group_id = self._segment(group_id)
        user_id = self._segment(user_id)
        kind = "owners" if owner else "members"
        return self.client.request("POST", "/groups/%s/%s/$ref" % (group_id, kind), json={
            "@odata.id": "https://graph.microsoft.com/v1.0/directoryObjects/%s" % user_id,
        })

    def remove_group_member(self, group_id, user_id, owner=False):
        kind = "owners" if owner else "members"
        return self.client.request("DELETE", "/groups/%s/%s/%s/$ref" % (
            self._segment(group_id), kind, self._segment(user_id)
        ), expected=(404,))

    def list_groups(self):
        return self.client.fetch_all(
            "/groups", params={
                "$select": "id,displayName,description,mail,mailNickname,groupTypes,securityEnabled,visibility,mailEnabled",
                "$top": str(self.PAGE_SIZE),
            }, max_pages=100
        )

    def list_group_members(self, group_id, owner=False):
        kind = "owners" if owner else "members"
        # /members puede contener otros directoryObjects que no son usuarios.
        return self.client.fetch_all(
            "/groups/%s/%s" % (self._segment(group_id), kind),
            params={"$top": str(self.PAGE_SIZE)}, max_pages=100,
        )

    def sync_groups(self):
        """Sincronizar metadatos sin alterar membresías locales ni borrar grupos.

        Retorna número de registros procesados. El administrador aplica membresía
        en una acción explícita; sincronizar no concede ni revoca accesos.
        """
        Group = self.env["microsoft.group"].sudo()
        data = self.list_groups()
        if isinstance(data, dict):
            groups = data.get("value", [])
        else:
            groups = data or []
        if len(groups) > self.MAX_SYNC_GROUPS:
            raise UserError("Se superó el máximo de grupos permitido para una sincronización.")
        count = 0
        for remote in groups:
            graph_id = remote.get("id")
            if not graph_id:
                continue
            is_m365 = "Unified" in (remote.get("groupTypes") or [])
            is_security = bool(remote.get("securityEnabled")) and not is_m365
            group_type = "microsoft365" if is_m365 else ("security" if is_security else "readonly")
            vals = {
                "name": remote.get("displayName") or graph_id,
                "description": remote.get("description") or False,
                "mail": remote.get("mail") or False,
                "mail_nickname": remote.get("mailNickname") or ("g_" + graph_id.replace("-", "")[:20]),
                "group_type": group_type,
                "visibility": "public" if str(remote.get("visibility") or "").lower() == "public" else "private",
                "last_sync": fields.Datetime.now(),
                "last_error": False,
            }
            record = Group.search([
                ("tenant_id", "=", self.tenant.id), ("graph_id", "=", graph_id),
            ], limit=1)
            if record:
                record.write(vals)
            else:
                vals.update({"tenant_id": self.tenant.id, "graph_id": graph_id})
                Group.create(vals)
            count += 1
        _logger.info("Microsoft 365 grupos sincronizados tenant_id=%s count=%s", self.tenant.id, count)
        return count
