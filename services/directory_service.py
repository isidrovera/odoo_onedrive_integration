# -*- coding: utf-8 -*-
from odoo import fields
from odoo.exceptions import UserError

from .graph_client import GraphClient


class DirectoryService:
    def __init__(self, tenant):
        self.tenant = tenant
        self.env = tenant.env
        self.client = GraphClient(token_provider=lambda: tenant.get_app_token("graph"), env=self.env)

    @staticmethod
    def _escape(value):
        return (value or "").replace("'", "''")

    def find_user_by_email(self, email):
        email = (email or "").strip().lower()
        if not email:
            return None
        safe = self._escape(email)
        users = self.client.fetch_all(
            "/users",
            params={
                "$filter": f"mail eq '{safe}' or userPrincipalName eq '{safe}'",
                "$select": "id,displayName,mail,userPrincipalName,userType,accountEnabled,externalUserState",
                "$top": "10",
            },
            max_pages=2,
        )
        return users[0] if users else None

    def get_user(self, user_id):
        return self.client.request(
            "GET",
            f"/users/{user_id}",
            params={"$select": "id,displayName,mail,userPrincipalName,userType,accountEnabled,externalUserState"},
        )

    def invite_guest(self, email, display_name, send_message=True):
        redirect = self.tenant.invite_redirect_url
        if not redirect:
            redirect = self.env["ir.config_parameter"].sudo().get_param("web.base.url", "").rstrip("/") + "/web"
        payload = {
            "invitedUserEmailAddress": email,
            "invitedUserDisplayName": display_name,
            "inviteRedirectUrl": redirect,
            "sendInvitationMessage": bool(send_message),
            "invitedUserMessageInfo": {
                "customizedMessageBody": "Se le ha concedido acceso a los documentos de la organización desde Odoo.",
                "messageLanguage": "es-ES",
            },
        }
        return self.client.request("POST", "/invitations", json=payload)

    def create_group(self, group):
        payload = {
            "displayName": group.name,
            "description": group.description or "Administrado desde Odoo",
            "mailEnabled": group.group_type == "microsoft365",
            "mailNickname": group.mail_nickname,
            "securityEnabled": group.group_type in ("security", "microsoft365"),
            "groupTypes": ["Unified"] if group.group_type == "microsoft365" else [],
        }
        if group.group_type == "microsoft365":
            payload["visibility"] = group.visibility.capitalize()
        owner_ids = group.owner_ids.mapped("entra_user_id")
        member_ids = group.member_ids.mapped("entra_user_id")
        if owner_ids:
            payload["owners@odata.bind"] = [
                f"https://graph.microsoft.com/v1.0/users/{user_id}" for user_id in owner_ids
            ]
        if member_ids:
            payload["members@odata.bind"] = [
                f"https://graph.microsoft.com/v1.0/users/{user_id}" for user_id in member_ids
            ]
        return self.client.request("POST", "/groups", json=payload)

    def update_group(self, group):
        return self.client.request("PATCH", f"/groups/{group.graph_id}", json={
            "displayName": group.name,
            "description": group.description or "",
        })

    def delete_group(self, graph_id):
        return self.client.request("DELETE", f"/groups/{graph_id}")

    def add_group_member(self, group_id, user_id, owner=False):
        relation = "owners" if owner else "members"
        return self.client.request("POST", f"/groups/{group_id}/{relation}/$ref", json={
            "@odata.id": f"https://graph.microsoft.com/v1.0/directoryObjects/{user_id}",
        })

    def remove_group_member(self, group_id, user_id, owner=False):
        relation = "owners" if owner else "members"
        return self.client.request("DELETE", f"/groups/{group_id}/{relation}/{user_id}/$ref", expected=(404,))

    def list_groups(self):
        return self.client.fetch_all(
            "/groups",
            params={
                "$select": "id,displayName,description,mail,mailNickname,groupTypes,mailEnabled,securityEnabled,visibility",
                "$top": "200",
            },
        )

    def list_group_members(self, group_id, owner=False):
        relation = "owners" if owner else "members"
        return self.client.fetch_all(
            f"/groups/{group_id}/{relation}",
            params={"$select": "id,displayName,mail,userPrincipalName,userType", "$top": "200"},
        )

    def sync_groups(self):
        Group = self.env["microsoft.group"].sudo()
        for data in self.list_groups():
            group_types = data.get("groupTypes") or []
            if "Unified" in group_types:
                group_type = "microsoft365"
            elif data.get("securityEnabled") and not data.get("mailEnabled"):
                group_type = "security"
            else:
                group_type = "readonly"
            values = {
                "tenant_id": self.tenant.id,
                "graph_id": data.get("id"),
                "name": data.get("displayName"),
                "description": data.get("description"),
                "mail": data.get("mail"),
                "mail_nickname": data.get("mailNickname") or data.get("id", "group")[:20],
                "group_type": group_type,
                "visibility": (data.get("visibility") or "private").lower(),
                "last_sync": fields.Datetime.now(),
            }
            existing = Group.search([("tenant_id", "=", self.tenant.id), ("graph_id", "=", data.get("id"))], limit=1)
            if existing:
                existing.write(values)
            else:
                Group.create(values)
        return True
