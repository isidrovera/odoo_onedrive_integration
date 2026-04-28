/** @odoo-module **/

import { Component, useState, onWillStart } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { rpc } from "@web/core/network/rpc";

export class OneDriveApp extends Component {

    setup() {
        this.state = useState({
            files: [],
            loading: true,
            currentFolder: null,
            path: [], // breadcrumb
        });

        onWillStart(async () => {
            await this.loadFiles();
        });
    }

    // ---------------------------------------
    // LOAD FILES
    // ---------------------------------------
    async loadFiles(folderId = null, folderName = "Inicio") {

        this.state.loading = true;

        const result = await rpc("/onedrive/list", {
            parent_id: folderId
        });

        this.state.files = result.value || [];
        this.state.currentFolder = folderId;

        if (folderId === null) {
            this.state.path = [];
        }

        this.state.loading = false;
    }

    // ---------------------------------------
    // OPEN FOLDER
    // ---------------------------------------
    async openFolder(file) {
        if (!file.folder) return;

        this.state.path.push({
            id: file.id,
            name: file.name
        });

        await this.loadFiles(file.id);
    }

    // ---------------------------------------
    // GO TO BREADCRUMB
    // ---------------------------------------
    async goTo(index) {

        const target = this.state.path[index];

        this.state.path = this.state.path.slice(0, index + 1);

        await this.loadFiles(target.id);
    }

    // ---------------------------------------
    // GO ROOT
    // ---------------------------------------
    async goRoot() {
        this.state.path = [];
        await this.loadFiles(null);
    }

    // ---------------------------------------
    // DELETE
    // ---------------------------------------
    async deleteItem(file) {
        if (!confirm("¿Eliminar archivo?")) return;

        await rpc("/onedrive/delete", {
            item_id: file.id
        });

        await this.loadFiles(this.state.currentFolder);
    }

    // ---------------------------------------
    // DOWNLOAD
    // ---------------------------------------
    downloadItem(file) {
        window.open(`/onedrive/download/${file.id}`, "_blank");
    }

    // ---------------------------------------
    // CREATE FOLDER
    // ---------------------------------------
    async createFolder() {

        const name = prompt("Nombre de carpeta:");
        if (!name) return;

        await rpc("/onedrive/create_folder", {
            name: name,
            parent_id: this.state.currentFolder
        });

        await this.loadFiles(this.state.currentFolder);
    }

    // ---------------------------------------
    // UPLOAD
    // ---------------------------------------
    async uploadFile(ev) {

        const file = ev.target.files[0];
        if (!file) return;

        const formData = new FormData();
        formData.append("file", file);

        await fetch("/onedrive/upload", {
            method: "POST",
            body: formData
        });

        await this.loadFiles(this.state.currentFolder);
    }
}
OneDriveApp.template = "odoo_onedrive_integration.onedrive_app";

registry.category("actions").add("onedrive_app", OneDriveApp);