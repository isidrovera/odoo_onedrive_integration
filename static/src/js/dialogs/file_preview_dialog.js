/** @odoo-module **/

import { Component, useState, onWillStart } from "@odoo/owl";
import { Dialog } from "@web/core/dialog/dialog";
import { rpc } from "@web/core/network/rpc";
import { getFileIcon, isImage, formatBytes, formatDate } from "../utils/file_utils";

/** Vista previa sin exponer tokens ni saltarse los permisos del controlador. */
export class FilePreviewDialog extends Component {
    static template = "odoo_onedrive_integration.FilePreviewDialog";
    static components = { Dialog };
    static props = {
        close: Function,
        file: Object,
        locationId: { type: [Number, Boolean], optional: true },
        onDownload: { type: Function, optional: true },
        onShare: { type: Function, optional: true },
        onDelete: { type: Function, optional: true },
        onOpenExternal: { type: Function, optional: true },
    };

    setup() {
        this.state = useState({
            previewUrl: null,
            loading: true,
            error: null,
        });
        this.icon = getFileIcon(this.props.file);
        this.formatBytes = formatBytes;
        this.formatDate = formatDate;

        onWillStart(async () => {
            try {
                if (isImage(this.props.file)) {
                    this.state.previewUrl = `/microsoft/thumbnail/${encodeURIComponent(this.props.file.id)}?size=large&location_id=${encodeURIComponent(this.props.locationId)}`;
                } else {
                    const res = await rpc("/microsoft/preview", {
                        item_id: this.props.file.id,
                        location_id: this.props.locationId,
                    });
                    const url = res?.getUrl || res?.postUrl || null;
                    if (url && this._safeUrl(url)) {
                        this.state.previewUrl = url;
                    } else {
                        this.state.error = "No hay vista previa disponible para este archivo.";
                    }
                }
            } catch (error) {
                this.state.error = "No hay vista previa disponible.";
            } finally {
                this.state.loading = false;
            }
        });
    }

    _safeUrl(url) {
        if (!url || typeof url !== "string") return false;
        try {
            const parsed = new URL(url, window.location.origin);
            if (parsed.protocol !== "https:") return false;
            const host = parsed.hostname.toLowerCase();
            return host === "sharepoint.com" ||
                host.endsWith(".sharepoint.com") ||
                host === "office.com" ||
                host.endsWith(".office.com") ||
                host === "officeapps.live.com" ||
                host.endsWith(".officeapps.live.com") ||
                host === "onedrive.live.com" ||
                host.endsWith(".onedrive.live.com") ||
                host === "microsoft.com" ||
                host.endsWith(".microsoft.com");
        } catch {
            return false;
        }
    }

    download() {
        this.props.onDownload?.();
    }

    share() {
        this.props.close();
        this.props.onShare?.();
    }

    remove() {
        this.props.close();
        this.props.onDelete?.();
    }

    openExternal() {
        this.props.onOpenExternal?.();
    }

    closeDialog() {
        this.props.close();
    }
}