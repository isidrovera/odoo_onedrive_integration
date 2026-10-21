
/** @odoo-module **/

import { Component } from "@odoo/owl";
import { Dialog } from "@web/core/dialog/dialog";
import {
    getFileIcon,
    formatBytes,
    formatDate,
} from "../utils/file_utils";

export class PropertiesDialog extends Component {
    static template = "odoo_onedrive_integration.PropertiesDialog";
    static components = { Dialog };

    static props = {
        close: Function,
        file: Object,
    };

    setup() {
        this.icon = getFileIcon(this.props.file);
        this.formatBytes = formatBytes;
        this.formatDate = formatDate;
    }

    get isFolder() {
        return Boolean(this.props.file?.folder);
    }

    get fileName() {
        return this.props.file?.name || "Sin nombre";
    }

    get fileType() {
        if (this.isFolder) {
            return "Carpeta";
        }

        return this.props.file?.file?.mimeType || "Archivo";
    }

    get fileSize() {
        if (this.isFolder) {
            return "—";
        }

        return this.formatBytes(this.props.file?.size || 0);
    }

    get childCount() {
        return this.props.file?.folder?.childCount || 0;
    }

    get createdBy() {
        return (
            this.props.file?.createdBy?.user?.displayName ||
            this.props.file?.createdBy?.application?.displayName ||
            "Microsoft 365"
        );
    }

    get modifiedBy() {
        return (
            this.props.file?.lastModifiedBy?.user?.displayName ||
            this.props.file?.lastModifiedBy?.application?.displayName ||
            "Microsoft 365"
        );
    }

    get webUrl() {
        const value = this.props.file?.webUrl;

        if (!value || typeof value !== "string") {
            return null;
        }

        try {
            const url = new URL(value);

            if (url.protocol !== "https:") {
                return null;
            }

            return url.href;
        } catch {
            return null;
        }
    }

    closeDialog() {
        this.props.close();
    }
}
