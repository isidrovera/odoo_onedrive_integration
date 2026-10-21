
/** @odoo-module **/

import { Component, useState, useRef, onMounted } from "@odoo/owl";
import { Dialog } from "@web/core/dialog/dialog";
import { useService } from "@web/core/utils/hooks";

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
        this.notification = useService("notification");

        this.state = useState({
            value: this.props.value || "",
            loading: false,
            error: "",
        });

        this.inputRef = useRef("input");

        onMounted(() => {
            this.inputRef.el?.focus();
            this.inputRef.el?.select();
        });
    }

    validateName(value) {
        const name = value.trim();

        if (!name) {
            return "Ingrese un nombre.";
        }

        if (name.length > 255) {
            return "El nombre no puede superar los 255 caracteres.";
        }

        if (/["*:<>?/\\|]/.test(name)) {
            return 'El nombre contiene caracteres no permitidos: " * : < > ? / \\ |';
        }

        if (name.endsWith(".") || name.endsWith(" ")) {
            return "El nombre no puede terminar en un punto o espacio.";
        }

        if (name === "." || name === "..") {
            return "El nombre indicado no es válido.";
        }

        if (/^(CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])(?:\..*)?$/i.test(name)) {
            return "Este nombre está reservado por el sistema.";
        }

        return "";
    }

    async confirm() {
        if (this.state.loading) {
            return;
        }

        const value = this.state.value.trim();
        const error = this.validateName(value);

        if (error) {
            this.state.error = error;
            this.notification.add(error, { type: "warning" });
            return;
        }

        this.state.error = "";
        this.state.loading = true;

        try {
            const result = await this.props.onConfirm(value);

            if (result === false) {
                return;
            }

            this.props.close();
        } catch (error) {
            console.error("[Microsoft 365] Error en diálogo:", error);

            this.state.error =
                error?.data?.message ||
                error?.message ||
                "No se pudo completar la operación.";

            this.notification.add(this.state.error, {
                type: "danger",
            });
        } finally {
            this.state.loading = false;
        }
    }

    onKeyDown(ev) {
        if (ev.key === "Enter") {
            ev.preventDefault();
            if (!this.state.loading && !ev.repeat) {
                this.confirm();
            }
        }

        if (ev.key === "Escape") {
            ev.stopPropagation();
            this.cancel();
        }
    }

    cancel() {
        if (!this.state.loading) {
            this.props.close();
        }
    }
}
