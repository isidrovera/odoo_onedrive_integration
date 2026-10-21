
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
        locationId: { type: [Number, Boolean], optional: true },
    };

    setup() {
        this.notification = useService("notification");

        this.state = useState({
            shareType: "view",
            scope: "organization",
            link: null,
            loading: false,
        });
    }

    _validate() {
        if (!this.props.file?.id) {
            throw new Error("No se ha seleccionado un documento válido.");
        }

        if (!this.props.locationId) {
            throw new Error("No se ha seleccionado una ubicación documental.");
        }

        if (!["view", "edit"].includes(this.state.shareType)) {
            throw new Error("El tipo de permiso no es válido.");
        }

        if (!["organization", "anonymous"].includes(this.state.scope)) {
            throw new Error("El alcance del enlace no es válido.");
        }

        // Los enlaces anónimos deben estar habilitados expresamente
        // por la administración del tenant y por el servidor.
        if (this.state.scope === "anonymous") {
            throw new Error(
                "Los enlaces públicos están deshabilitados. " +
                "Utilice el acceso para usuarios de la organización."
            );
        }
    }

    _extractLink(response) {
        const link = response?.link?.webUrl || null;

        if (!link || typeof link !== "string") {
            return null;
        }

        try {
            const url = new URL(link);

            if (url.protocol !== "https:") {
                return null;
            }

            return url.href;
        } catch {
            return null;
        }
    }

    async generate() {
        if (this.state.loading) {
            return;
        }

        this.state.loading = true;
        this.state.link = null;

        try {
            this._validate();

            const result = await rpc("/microsoft/share", {
                item_id: this.props.file.id,
                share_type: this.state.shareType,
                scope: this.state.scope,
                location_id: this.props.locationId,
            });

            const link = this._extractLink(result);

            if (!link) {
                throw new Error(
                    "Microsoft no devolvió un enlace válido para compartir."
                );
            }

            this.state.link = link;

            this.notification.add(
                "Enlace generado correctamente.",
                { type: "success" }
            );
        } catch (error) {
            console.error(
                "[Microsoft 365] Error generando enlace:",
                error
            );

            this.notification.add(
                error?.data?.message ||
                error?.message ||
                "No se pudo generar el enlace.",
                { type: "danger" }
            );
        } finally {
            this.state.loading = false;
        }
    }

    async copyLink() {
        if (!this.state.link) {
            return;
        }

        try {
            await navigator.clipboard.writeText(this.state.link);

            this.notification.add(
                "Enlace copiado al portapapeles.",
                { type: "success" }
            );
        } catch (error) {
            console.error(
                "[Microsoft 365] Error copiando enlace:",
                error
            );

            this.notification.add(
                "No se pudo copiar el enlace automáticamente.",
                { type: "warning" }
            );
        }
    }

    closeDialog() {
        if (!this.state.loading) {
            this.props.close();
        }
    }
}
