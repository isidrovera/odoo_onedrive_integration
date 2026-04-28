// static/src/js/dialogs/confirm_dialog.js
/** @odoo-module **/
import { Component } from "@odoo/owl";
import { Dialog } from "@web/core/dialog/dialog";

export class ConfirmDialog extends Component {
    static template = "odoo_onedrive_integration.ConfirmDialog";
    static components = { Dialog };
    static props = {
        close: Function,
        title: { type: String, optional: true },
        message: String,
        description: { type: String, optional: true },
        icon: { type: String, optional: true },
        danger: { type: Boolean, optional: true },
        confirmLabel: { type: String, optional: true },
        cancelLabel: { type: String, optional: true },
        onConfirm: Function,
    };

    async confirm() {
        await this.props.onConfirm();
        this.props.close();
    }
    cancel() { this.props.close(); }
}