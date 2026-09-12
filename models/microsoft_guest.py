# -*- coding: utf-8 -*-
from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError


class MicrosoftGuest(models.Model):
    _name = "microsoft.guest"
    _description = "Usuario invitado Microsoft"
    _rec_name = "display_name"
    _order = "display_name, email"
    _inherit = ["mail.thread", "mail.activity.mixin"]

    tenant_id = fields.Many2one("onedrive.account", required=True, ondelete="cascade", tracking=True, index=True)
    user_id = fields.Many2one("res.users", string="Usuario Odoo", ondelete="set null", tracking=True, index=True)
    display_name = fields.Char(required=True, tracking=True)
    email = fields.Char(required=True, tracking=True, index=True)
    entra_user_id = fields.Char(string="ID Microsoft", copy=False, readonly=True, index=True)
    user_principal_name = fields.Char(string="UPN invitado", copy=False, readonly=True)
    state = fields.Selection([
        ("draft", "No registrado"),
        ("pending", "Pendiente de aceptación"),
        ("accepted", "Aceptado"),
        ("blocked", "Bloqueado"),
        ("error", "Error"),
    ], default="draft", required=True, tracking=True, index=True)
    account_enabled = fields.Boolean(default=True, readonly=True)
    group_ids = fields.Many2many(
        "microsoft.group", "microsoft_group_guest_rel", "guest_id", "group_id",
        string="Grupos como miembro",
    )
    owner_group_ids = fields.Many2many(
        "microsoft.group", "microsoft_group_owner_guest_rel", "guest_id", "group_id",
        string="Grupos como propietario",
    )
    invitation_url = fields.Char(copy=False, groups="odoo_onedrive_integration.group_microsoft_admin")
    invitation_date = fields.Datetime(readonly=True)
    accepted_date = fields.Datetime(readonly=True)
    last_sync = fields.Datetime(readonly=True)
    last_error = fields.Text(readonly=True)
    active = fields.Boolean(default=True)

    _sql_constraints = [
        ("microsoft_guest_email_tenant_unique", "unique(tenant_id, email)", "El correo ya está registrado en este tenant."),
        ("microsoft_guest_entra_tenant_unique", "unique(tenant_id, entra_user_id)", "La identidad Microsoft ya está vinculada."),
    ]

    @api.constrains("email")
    def _check_email(self):
        for record in self:
            if not record.email or "@" not in record.email:
                raise ValidationError("Ingrese un correo válido.")

    @api.model_create_multi
    def create(self, vals_list):
        for values in vals_list:
            if values.get("email"):
                values["email"] = values["email"].strip().lower()
        return super().create(vals_list)

    def write(self, values):
        if values.get("email"):
            values["email"] = values["email"].strip().lower()
        return super().write(values)

    def _check_admin(self):
        if not self.env.user.has_group("odoo_onedrive_integration.group_microsoft_admin"):
            raise AccessError("Solo el administrador Microsoft 365 puede administrar invitados.")

    def _apply_remote_user(self, data):
        self.ensure_one()
        external_state = (data.get("externalUserState") or data.get("status") or "").lower().replace(" ", "")
        if data.get("accountEnabled") is False:
            state = "blocked"
        elif (data.get("userType") or "").lower() == "member" or external_state == "accepted":
            state = "accepted"
        else:
            state = "pending"
        values = {
            "entra_user_id": data.get("id") or (data.get("invitedUser") or {}).get("id"),
            "user_principal_name": data.get("userPrincipalName") or (data.get("invitedUser") or {}).get("userPrincipalName"),
            "state": state,
            "account_enabled": data.get("accountEnabled", True),
            "last_sync": fields.Datetime.now(),
            "last_error": False,
        }
        if state == "accepted" and not self.accepted_date:
            values["accepted_date"] = fields.Datetime.now()
        self.sudo().write(values)

    def action_invite(self):
        self._check_admin()
        from ..services.directory_service import DirectoryService
        Audit = self.env["microsoft.audit.log"]
        for guest in self:
            service = DirectoryService(guest.tenant_id)
            try:
                data = service.get_user(guest.entra_user_id) if guest.entra_user_id else service.find_user_by_email(guest.email)
                if not data:
                    invitation = service.invite_guest(guest.email, guest.display_name, send_message=True)
                    guest.sudo().write({
                        "invitation_url": invitation.get("inviteRedeemUrl"),
                        "invitation_date": fields.Datetime.now(),
                    })
                    data = invitation
                guest._apply_remote_user(data)
                guest.action_apply_groups()
                Audit._log("invite", tenant_id=guest.tenant_id.id, item_id=guest.entra_user_id, item_name=guest.email)
            except Exception as error:
                guest.sudo().write({"state": "error", "last_error": str(error), "last_sync": fields.Datetime.now()})
                Audit._log("invite", status="error", tenant_id=guest.tenant_id.id, item_name=guest.email, message=str(error))
                raise
        return True

    def action_sync(self):
        self._check_admin()
        from ..services.directory_service import DirectoryService
        for guest in self:
            if not guest.entra_user_id:
                data = DirectoryService(guest.tenant_id).find_user_by_email(guest.email)
                if not data:
                    continue
            else:
                data = DirectoryService(guest.tenant_id).get_user(guest.entra_user_id)
            guest._apply_remote_user(data)
        return True

    def action_apply_groups(self):
        self._check_admin()
        from ..services.directory_service import DirectoryService
        for guest in self:
            if not guest.entra_user_id:
                raise UserError(f"Primero invite a {guest.email}.")
            for group in guest.group_ids:
                group.action_apply_members()
            for group in guest.owner_group_ids:
                group.action_apply_members()
        return True

    @api.model
    def _cron_sync_pending_guests(self):
        from ..services.directory_service import DirectoryService
        guests = self.sudo().search([("state", "in", ("pending", "error")), ("entra_user_id", "!=", False)], limit=500)
        for guest in guests:
            try:
                data = DirectoryService(guest.tenant_id).get_user(guest.entra_user_id)
                guest._apply_remote_user(data)
            except Exception:
                continue
        return True
