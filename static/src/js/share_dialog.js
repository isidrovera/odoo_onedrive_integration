// static/src/js/dialogs/share_dialog.js
/** @odoo-module **/
import { Component, useState } from "@odoo/owl";
import { Dialog } from "@web/core/dialog/dialog";
import { rpc } from "@web/core/network/rpc";
import { useService } from "@web/core/utils/hooks";

export class ShareDialog extends Component {
    static template = "odoo_onedrive_integration.ShareDialog";
    static components = { Dialog };
    static props = {
        close: Function,
        file: Object,
        accountId: { type: [Number, Boolean], optional: true },
    };

    setup() {
        this.notification = useService("notification");
        this.state = useState({
            shareType: "view", // view | edit
            scope: "anonymous", // anonymous | organization
            link: null,
            loading: false,
        });
    }

    async generate() {
        this.state.loading = true;
        try {
            const res = await rpc("/onedrive/share", {
                item_id: this.props.file.id,
                share_type: this.state.shareType,
                scope: this.state.scope,
                account_id: this.props.accountId,
            });
            this.state.link = res?.link?.webUrl || null;
        } catch (e) {
            this.notification.add("Error generando enlace", { type: "danger" });
        } finally {
            this.state.loading = false;
        }
    }

    async copyLink() {
        if (!this.state.link) return;
        try {
            await navigator.clipboard.writeText(this.state.link);
            this.notification.add("Enlace copiado", { type: "success" });
        } catch (e) {
            this.notification.add("No se pudo copiar", { type: "warning" });
        }
    }

    closeDialog() { this.props.close(); }
}