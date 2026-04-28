// static/src/js/dialogs/file_preview_dialog.js
/** @odoo-module **/
import { Component, useState, onWillStart } from "@odoo/owl";
import { Dialog } from "@web/core/dialog/dialog";
import { rpc } from "@web/core/network/rpc";
import { getFileIcon, isImage, formatBytes, formatDate } from "../utils/file_utils";

export class FilePreviewDialog extends Component {
    static template = "odoo_onedrive_integration.FilePreviewDialog";
    static components = { Dialog };
    static props = {
        close: Function,
        file: Object,
        accountId: { type: [Number, Boolean], optional: true },
        onDownload: { type: Function, optional: true },
        onShare: { type: Function, optional: true },
        onDelete: { type: Function, optional: true },
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
                    this.state.previewUrl = `/onedrive/thumbnail/${this.props.file.id}?size=large`;
                } else {
                    const res = await rpc("/onedrive/preview", {
                        item_id: this.props.file.id,
                        account_id: this.props.accountId,
                    });
                    this.state.previewUrl = res.getUrl || res.postUrl || null;
                }
            } catch (e) {
                this.state.error = "No hay vista previa disponible";
            } finally {
                this.state.loading = false;
            }
        });
    }

    download() { this.props.onDownload?.(); }
    share() { this.props.close(); this.props.onShare?.(); }
    remove() { this.props.close(); this.props.onDelete?.(); }
    closeDialog() { this.props.close(); }
}