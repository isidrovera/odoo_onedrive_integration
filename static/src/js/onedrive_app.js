/** @odoo-module **/
import { Component, useState, useRef, onWillStart, onMounted } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { rpc } from "@web/core/network/rpc";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";

import { FilePreviewDialog } from "./dialogs/file_preview_dialog";
import { PromptDialog } from "./dialogs/prompt_dialog";
import { ShareDialog } from "./dialogs/share_dialog";
import { ConfirmDialog } from "./dialogs/confirm_dialog";
import { PropertiesDialog } from "./dialogs/properties_dialog";
import { getFileIcon, formatBytes, formatDate } from "./utils/file_utils";

export class OneDriveApp extends Component {
    static template = "odoo_onedrive_integration.OneDriveApp";

    setup() {
        this.dialog = useService("dialog");
        this.notification = useService("notification");
        this.rootRef = useRef("root");
        this.fileInputRef = useRef("fileInput");

        this.state = useState({
            files: [],
            filteredFiles: [],
            loading: true,
            currentFolder: null,
            path: [],
            view: "grid", // grid | list
            sortBy: "name",
            sortDir: "asc",
            search: "",
            searching: false,
            selected: new Set(),
            uploadProgress: null,
            accounts: [],
            accountId: null,
            ctxMenu: { open: false, x: 0, y: 0, file: null },
            dragOver: false,
        });

        this._searchDebounce = null;

        // helpers expuestos al template
        this.getFileIcon = getFileIcon;
        this.formatBytes = formatBytes;
        this.formatDate = formatDate;

        onWillStart(async () => {
            await this.loadAccounts();
            await this.loadFiles();
        });

        onMounted(() => {
            document.addEventListener("click", this.closeCtxMenu.bind(this));
            document.addEventListener("keydown", this.onKeyDown.bind(this));
        });
    }

    // -------- ACCOUNTS --------
    async loadAccounts() {
        try {
            const res = await rpc("/onedrive/accounts", {});
            this.state.accounts = res || [];
            if (res && res.length) this.state.accountId = res[0].id;
        } catch (e) {
            this.state.accounts = [];
        }
    }

    // -------- LOAD --------
    async loadFiles(folderId = null) {
        this.state.loading = true;
        this.state.selected.clear();
        try {
            const result = await rpc("/onedrive/list", {
                parent_id: folderId,
                account_id: this.state.accountId,
            });
            this.state.files = result.value || [];
            this.state.currentFolder = folderId;
            if (folderId === null) this.state.path = [];
            this.applyFilterAndSort();
        } catch (e) {
            this.notify(_t("Error cargando archivos"), "danger");
        } finally {
            this.state.loading = false;
        }
    }

    applyFilterAndSort() {
        let list = [...this.state.files];
        // En modo búsqueda global, los resultados ya vienen filtrados del servidor
        // No aplicar filtro local sobre ellos
        const q = this.state.search.trim().toLowerCase();
        if (q && !this.state.searching) {
            list = list.filter((f) => f.name.toLowerCase().includes(q));
        }
        const dir = this.state.sortDir === "asc" ? 1 : -1;
        list.sort((a, b) => {
            // carpetas primero
            const af = !!a.folder, bf = !!b.folder;
            if (af !== bf) return af ? -1 : 1;
            let av, bv;
            switch (this.state.sortBy) {
                case "modified":
                    av = a.lastModifiedDateTime || ""; bv = b.lastModifiedDateTime || ""; break;
                case "size":
                    av = a.size || 0; bv = b.size || 0; break;
                case "type":
                    av = (a.file?.mimeType || "folder"); bv = (b.file?.mimeType || "folder"); break;
                default:
                    av = a.name?.toLowerCase() || ""; bv = b.name?.toLowerCase() || "";
            }
            return av < bv ? -1 * dir : av > bv ? 1 * dir : 0;
        });
        this.state.filteredFiles = list;
    }

    setSort(field) {
        if (this.state.sortBy === field) {
            this.state.sortDir = this.state.sortDir === "asc" ? "desc" : "asc";
        } else {
            this.state.sortBy = field;
            this.state.sortDir = "asc";
        }
        this.applyFilterAndSort();
    }

    setView(v) { this.state.view = v; }

    // -------- NAVIGATION --------
    async openFolder(file) {
        if (!file.folder) {
            // Doble click en archivo: abrir en Office Online / navegador (modo edición)
            this.openExternal(file);
            return;
        }
        // Si estábamos en búsqueda global, salir de ese modo
        if (this.state.searching) {
            this.state.searching = false;
            this.state.search = "";
        }
        this.state.path.push({ id: file.id, name: file.name });
        await this.loadFiles(file.id);
    }

    /**
     * Abre el archivo en su aplicación nativa de Microsoft (Office Online)
     * usando la webUrl que provee Microsoft Graph. Esto permite EDITAR
     * archivos de Excel/Word/PowerPoint en el navegador.
     */
    openExternal(file) {
        if (file.webUrl) {
            window.open(file.webUrl, "_blank", "noopener");
        } else {
            // Fallback: vista previa interna
            this.previewFile(file);
        }
    }

    async goRoot() {
        if (this.state.searching) {
            this.state.searching = false;
            this.state.search = "";
        }
        this.state.path = [];
        await this.loadFiles(null);
    }

    async goTo(idx) {
        if (this.state.searching) {
            this.state.searching = false;
            this.state.search = "";
        }
        const target = this.state.path[idx];
        this.state.path = this.state.path.slice(0, idx + 1);
        await this.loadFiles(target.id);
    }

    async goUp() {
        if (this.state.path.length === 0) return;
        if (this.state.searching) {
            this.state.searching = false;
            this.state.search = "";
        }
        this.state.path.pop();
        const last = this.state.path[this.state.path.length - 1];
        await this.loadFiles(last ? last.id : null);
    }

    // -------- SEARCH (GLOBAL) --------
    async onSearchInput(ev) {
        this.state.search = ev.target.value;

        // Cancelar timer pendiente
        if (this._searchDebounce) {
            clearTimeout(this._searchDebounce);
            this._searchDebounce = null;
        }

        // Si se vacía el input, volver a la carpeta actual
        if (!this.state.search.trim()) {
            this.state.searching = false;
            await this.loadFiles(this.state.currentFolder);
            return;
        }

        // Debounce 400ms: búsqueda global mientras escribe
        this._searchDebounce = setTimeout(async () => {
            await this._doGlobalSearch(this.state.search.trim());
        }, 400);
    }

    async onSearchSubmit(ev) {
        if (ev.key !== "Enter") return;
        if (this._searchDebounce) {
            clearTimeout(this._searchDebounce);
            this._searchDebounce = null;
        }
        const q = this.state.search.trim();
        if (!q) return;
        await this._doGlobalSearch(q);
    }

    async _doGlobalSearch(q) {
        this.state.searching = true;
        this.state.loading = true;
        try {
            const res = await rpc("/onedrive/search", {
                query: q,
                account_id: this.state.accountId,
            });
            this.state.files = res.value || [];
            this.applyFilterAndSort();
        } catch (e) {
            this.notify(_t("Error buscando"), "danger");
        } finally {
            this.state.loading = false;
        }
    }

    async clearSearch() {
        if (this._searchDebounce) {
            clearTimeout(this._searchDebounce);
            this._searchDebounce = null;
        }
        this.state.search = "";
        this.state.searching = false;
        await this.loadFiles(this.state.currentFolder);
    }

    // -------- SELECTION --------
    toggleSelect(file, ev) {
        if (ev) ev.stopPropagation();
        if (this.state.selected.has(file.id)) {
            this.state.selected.delete(file.id);
        } else {
            this.state.selected.add(file.id);
        }
        this.state.selected = new Set(this.state.selected);
    }

    isSelected(file) { return this.state.selected.has(file.id); }

    selectAll() {
        if (this.state.selected.size === this.state.filteredFiles.length) {
            this.state.selected = new Set();
        } else {
            this.state.selected = new Set(this.state.filteredFiles.map(f => f.id));
        }
    }

    // -------- CONTEXT MENU --------
    openCtxMenu(file, ev) {
        ev.preventDefault();
        ev.stopPropagation();
        this.state.ctxMenu = { open: true, x: ev.clientX, y: ev.clientY, file };
    }

    closeCtxMenu() {
        if (this.state.ctxMenu.open) this.state.ctxMenu = { open: false, x: 0, y: 0, file: null };
    }

    // -------- ACTIONS --------
    /**
     * Vista previa interna (iframe) - solo lectura.
     * Para editar use openExternal().
     */
    previewFile(file) {
        if (file.folder) {
            this.openFolder(file);
            return;
        }
        this.dialog.add(FilePreviewDialog, {
            file,
            accountId: this.state.accountId,
            onDownload: () => this.downloadItem(file),
            onShare: () => this.shareItem(file),
            onDelete: () => this.deleteItem(file),
            onOpenExternal: () => this.openExternal(file),
        });
    }

    /**
     * Descargar archivo o carpeta (zip)
     */
    downloadItem(file) {
        const url = file.folder
            ? `/onedrive/download_folder/${file.id}`
            : `/onedrive/download/${file.id}`;
        window.open(url, "_blank");
    }

    async createFolder() {
        this.dialog.add(PromptDialog, {
            title: _t("Nueva carpeta"),
            label: _t("Nombre de la carpeta"),
            placeholder: _t("Mi carpeta"),
            icon: "bi-folder-plus",
            confirmLabel: _t("Crear"),
            onConfirm: async (name) => {
                try {
                    await rpc("/onedrive/create_folder", {
                        name,
                        parent_id: this.state.currentFolder,
                        account_id: this.state.accountId,
                    });
                    this.notify(_t("Carpeta creada"), "success");
                    await this.loadFiles(this.state.currentFolder);
                } catch (e) {
                    this.notify(_t("Error al crear"), "danger");
                }
            },
        });
    }

    renameItem(file) {
        this.dialog.add(PromptDialog, {
            title: _t("Renombrar"),
            label: _t("Nuevo nombre"),
            value: file.name,
            icon: "bi-pencil",
            confirmLabel: _t("Renombrar"),
            onConfirm: async (name) => {
                try {
                    await rpc("/onedrive/rename", {
                        item_id: file.id,
                        new_name: name,
                        account_id: this.state.accountId,
                    });
                    this.notify(_t("Renombrado"), "success");
                    if (this.state.searching) {
                        await this._doGlobalSearch(this.state.search.trim());
                    } else {
                        await this.loadFiles(this.state.currentFolder);
                    }
                } catch (e) {
                    this.notify(_t("Error al renombrar"), "danger");
                }
            },
        });
    }

    deleteItem(file) {
        this.dialog.add(ConfirmDialog, {
            title: _t("Eliminar elemento"),
            message: _t('¿Seguro que deseas eliminar "%s"?', file.name),
            description: _t("Se moverá a la papelera de OneDrive."),
            icon: "bi-trash",
            danger: true,
            confirmLabel: _t("Eliminar"),
            onConfirm: async () => {
                try {
                    await rpc("/onedrive/delete", {
                        item_id: file.id,
                        account_id: this.state.accountId,
                    });
                    this.notify(_t("Elemento eliminado"), "success");
                    if (this.state.searching) {
                        await this._doGlobalSearch(this.state.search.trim());
                    } else {
                        await this.loadFiles(this.state.currentFolder);
                    }
                } catch (e) {
                    this.notify(_t("Error al eliminar"), "danger");
                }
            },
        });
    }

    deleteSelected() {
        const ids = Array.from(this.state.selected);
        if (!ids.length) return;
        this.dialog.add(ConfirmDialog, {
            title: _t("Eliminar elementos"),
            message: _t("¿Eliminar %s elementos seleccionados?", ids.length),
            danger: true,
            icon: "bi-trash",
            confirmLabel: _t("Eliminar todos"),
            onConfirm: async () => {
                for (const id of ids) {
                    try {
                        await rpc("/onedrive/delete", { item_id: id, account_id: this.state.accountId });
                    } catch (e) { /* continue */ }
                }
                this.notify(_t("Elementos eliminados"), "success");
                if (this.state.searching) {
                    await this._doGlobalSearch(this.state.search.trim());
                } else {
                    await this.loadFiles(this.state.currentFolder);
                }
            },
        });
    }

    shareItem(file) {
        this.dialog.add(ShareDialog, {
            file,
            accountId: this.state.accountId,
        });
    }

    showProperties(file) {
        this.dialog.add(PropertiesDialog, { file });
    }

    // -------- UPLOAD --------
    triggerUpload() { this.fileInputRef.el?.click(); }

    async uploadFile(ev) {
        const files = ev.target?.files || ev;
        if (!files || !files.length) return;
        for (const file of files) {
            await this._uploadOne(file);
        }
        await this.loadFiles(this.state.currentFolder);
        if (this.fileInputRef.el) this.fileInputRef.el.value = "";
    }

    async _uploadOne(file) {
        this.state.uploadProgress = { name: file.name, percent: 0 };
        const formData = new FormData();
        formData.append("file", file);
        if (this.state.accountId) formData.append("account_id", this.state.accountId);
        if (this.state.currentFolder) formData.append("parent_id", this.state.currentFolder);

        return new Promise((resolve) => {
            const xhr = new XMLHttpRequest();
            xhr.open("POST", "/onedrive/upload");
            xhr.upload.onprogress = (e) => {
                if (e.lengthComputable) {
                    this.state.uploadProgress = {
                        name: file.name,
                        percent: Math.round((e.loaded / e.total) * 100),
                    };
                }
            };
            xhr.onload = () => {
                this.state.uploadProgress = null;
                if (xhr.status < 400) {
                    this.notify(_t("Subido: %s", file.name), "success");
                } else {
                    this.notify(_t("Error subiendo: %s", file.name), "danger");
                }
                resolve();
            };
            xhr.onerror = () => {
                this.state.uploadProgress = null;
                this.notify(_t("Error de red"), "danger");
                resolve();
            };
            xhr.send(formData);
        });
    }

    // -------- DRAG & DROP --------
    onDragOver(ev) {
        ev.preventDefault();
        this.state.dragOver = true;
    }
    onDragLeave(ev) {
        ev.preventDefault();
        this.state.dragOver = false;
    }
    async onDrop(ev) {
        ev.preventDefault();
        this.state.dragOver = false;
        const files = ev.dataTransfer?.files;
        if (files && files.length) await this.uploadFile(files);
    }

    // -------- KEYBOARD --------
    onKeyDown(ev) {
        if (ev.key === "Escape") {
            this.state.selected = new Set();
            this.closeCtxMenu();
        }
        if (ev.key === "Delete" && this.state.selected.size > 0) {
            this.deleteSelected();
        }
    }

    // -------- UTIL --------
    notify(msg, type = "info") {
        this.notification.add(msg, { type });
    }

    refresh() {
        if (this.state.searching) {
            this._doGlobalSearch(this.state.search.trim());
        } else {
            this.loadFiles(this.state.currentFolder);
        }
    }
}

registry.category("actions").add("onedrive_app", OneDriveApp);