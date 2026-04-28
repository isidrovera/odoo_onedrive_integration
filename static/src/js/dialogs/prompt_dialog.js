// static/src/js/dialogs/prompt_dialog.js
/** @odoo-module **/
import { Component, useState, useRef, onMounted } from "@odoo/owl";
import { Dialog } from "@web/core/dialog/dialog";

export class PromptDialog extends Component {
    static template = "odoo_onedrive_integration.PromptDialog";
    static components = { Dialog };
    static props = {
        close: Function,
        title: String,
        label: { type: String, optional: true },
        value: { type: String, optional: true },
        placeholder: { type: String, optional: true },
        icon: { type: String, optional: true },
        confirmLabel: { type: String, optional: true },
        onConfirm: Function,
    };

    setup() {
        this.state = useState({ value: this.props.value || "" });
        this.inputRef = useRef("input");
        onMounted(() => {
            this.inputRef.el?.focus();
            this.inputRef.el?.select();
        });
    }

    async confirm() {
        const v = this.state.value.trim();
        if (!v) return;
        await this.props.onConfirm(v);
        this.props.close();
    }

    onKeyDown(ev) { if (ev.key === "Enter") this.confirm(); }
    cancel() { this.props.close(); }
}