// static/src/js/dialogs/properties_dialog.js
/** @odoo-module **/
import { Component } from "@odoo/owl";
import { Dialog } from "@web/core/dialog/dialog";
import { getFileIcon, formatBytes, formatDate } from "../utils/file_utils";

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

    closeDialog() { this.props.close(); }
}