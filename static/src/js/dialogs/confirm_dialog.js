
/** @odoo-module **/

import { Component, useState } from "@odoo/owl";
import { Dialog } from "@web/core/dialog/dialog";
import { useService } from "@web/core/utils/hooks";

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

    setup() {
        this.notification = useService("notification");

        this.state = useState({
            loading: false,
        });
    }

    async confirm() {
        if (this.state.loading) {
            return;
        }

        this.state.loading = true;

        try {
            const result = await this.props.onConfirm();

            if (result === false) {
                return;
            }

            this.props.close();
        } catch (error) {
            console.error(
                "[Microsoft 365] Error en confirmación:",
                error
            );

            this.notification.add(
                error?.data?.message ||
                error?.message ||
                "No se pudo completar la operación.",
                {
                    type: "danger",
                }
            );
        } finally {
            this.state.loading = false;
        }
    }

    cancel() {
        if (!this.state.loading) {
            this.props.close();
        }
    }
}
