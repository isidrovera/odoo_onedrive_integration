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

        // bound handlers para poder removerlos correctamente
        this._onDocClick = this.closeCtxMenu.bind(this);
        this._onDocKey = this.onKeyDown.bind(this);

        // =========================================================
        // MODO SELECTOR (cuando se usa dentro del wizard de envío)
        // Cuando el componente padre provee env.onedriveSelector con
        // mode="select", el OneDriveApp oculta acciones destructivas
        // y notifica los archivos elegidos al padre.
        // =========================================================
        this.selectorMode = !!(
            this.env.onedriveSelector &&
            this.env.onedriveSelector.mode === "select"
        );

        // Cache de archivos seleccionados a través de carpetas
        // (id -> {id, name, size}) para que la selección persista
        // al navegar dentro del selector.
        this._selectedCache = new Map();

        onWillStart(async () => {
            await this.loadAccounts();
            await this.loadFiles();
        });

        onMounted(() => {
            document.addEventListener("click", this._onDocClick);
            document.addEventListener("keydown", this._onDocKey);
        });
    }

    // =========================================================
    // ACCOUNTS
    // =========================================================
    async loadAccounts() {
        try {
            const res = await rpc("/onedrive/accounts", {});
            this.state.accounts = res || [];
            if (res && res.length) this.state.accountId = res[0].id;
        } catch (e) {
            console.error("loadAccounts error:", e);
            this.state.accounts = [];
        }
    }

    // =========================================================
    // LOAD
    // =========================================================
    async loadFiles(folderId = null) {
        this.state.loading = true;
        // En modo selector NO reseteamos la selección al navegar:
        // el usuario puede haber elegido archivos en otras carpetas
        // y queremos mantenerlos. Solo limpiamos el Set visible.
        this.state.selected = new Set();
        try {
            const result = await rpc("/onedrive/list", {
                parent_id: folderId,
                account_id: this.state.accountId,
            });
            // Sanitizar respuesta: quedarnos solo con items válidos
            const raw = (result && result.value) || [];
            this.state.files = raw.filter(f => f && f.id && f.name);
            this.state.currentFolder = folderId;
            if (folderId === null) this.state.path = [];
            this.applyFilterAndSort();

            // Re-marcar como seleccionados los archivos que ya estaban
            // en el cache global del selector (si aplica).
            if (this.selectorMode && this._selectedCache.size > 0) {
                const visibleIds = new Set(this.state.files.map(f => f.id));
                const newSel = new Set();
                for (const id of this._selectedCache.keys()) {
                    if (visibleIds.has(id)) newSel.add(id);
                }
                this.state.selected = newSel;
            }
        } catch (e) {
            console.error("loadFiles error:", e);
            this.notify(_t("Error cargando archivos"), "danger");
            this.state.files = [];
            this.state.filteredFiles = [];
        } finally {
            this.state.loading = false;
        }
    }

    applyFilterAndSort() {
        try {
            let list = (this.state.files || []).filter(f => f && f.id);

            const q = (this.state.search || "").trim().toLowerCase();
            if (q && !this.state.searching) {
                list = list.filter(f => (f.name || "").toLowerCase().includes(q));
            }

            const dir = this.state.sortDir === "asc" ? 1 : -1;
            list.sort((a, b) => {
                // carpetas siempre primero
                const af = !!(a && a.folder);
                const bf = !!(b && b.folder);
                if (af !== bf) return af ? -1 : 1;

                let av, bv;
                switch (this.state.sortBy) {
                    case "modified":
                        av = a.lastModifiedDateTime || "";
                        bv = b.lastModifiedDateTime || "";
                        break;
                    case "size":
                        av = a.size || 0;
                        bv = b.size || 0;
                        break;
                    case "type":
                        av = (a.file && a.file.mimeType) || "folder";
                        bv = (b.file && b.file.mimeType) || "folder";
                        break;
                    default:
                        av = (a.name || "").toLowerCase();
                        bv = (b.name || "").toLowerCase();
                }
                if (av < bv) return -1 * dir;
                if (av > bv) return 1 * dir;
                return 0;
            });

            this.state.filteredFiles = list;
        } catch (e) {
            console.error("applyFilterAndSort error:", e);
            this.state.filteredFiles = this.state.files || [];
        }
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

    // =========================================================
    // NAVIGATION
    // =========================================================
    async openFolder(file) {
        if (!file) return;
        if (!file.folder) {
            // En modo selector: doble click selecciona archivo
            // en lugar de abrirlo en Office Online.
            if (this.selectorMode) {
                this.toggleSelect(file);
                return;
            }
            // Doble click en archivo (modo normal): abrir en Office Online
            this.openExternal(file);
            return;
        }
        // Salir de modo búsqueda al navegar
        if (this.state.searching) {
            this.state.searching = false;
            this.state.search = "";
        }
        this.state.path.push({ id: file.id, name: file.name });
        await this.loadFiles(file.id);
    }

    /**
     * Abre el archivo en su app nativa de Microsoft (Office Online)
     * usando webUrl. Permite EDITAR Excel/Word/PowerPoint en el navegador.
     */
    openExternal(file) {
        if (file && file.webUrl) {
            window.open(file.webUrl, "_blank", "noopener");
        } else {
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
        if (!target) return;
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

    // =========================================================
    // SEARCH (GLOBAL en todo OneDrive)
    // =========================================================
    async onSearchInput(ev) {
        this.state.search = ev.target.value;

        if (this._searchDebounce) {
            clearTimeout(this._searchDebounce);
            this._searchDebounce = null;
        }

        if (!this.state.search.trim()) {
            this.state.searching = false;
            await this.loadFiles(this.state.currentFolder);
            return;
        }

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
            const raw = (res && res.value) || [];
            this.state.files = raw.filter(f => f && f.id && f.name);
            this.applyFilterAndSort();

            // Re-marcar selección desde cache (modo selector)
            if (this.selectorMode && this._selectedCache.size > 0) {
                const visibleIds = new Set(this.state.files.map(f => f.id));
                const newSel = new Set();
                for (const id of this._selectedCache.keys()) {
                    if (visibleIds.has(id)) newSel.add(id);
                }
                this.state.selected = newSel;
            }
        } catch (e) {
            console.error("search error:", e);
            this.notify(_t("Error buscando"), "danger");
            this.state.files = [];
            this.state.filteredFiles = [];
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

    // =========================================================
    // SELECTION
    // =========================================================
    toggleSelect(file, ev) {
        if (ev) ev.stopPropagation();
        if (!file) return;
        // En modo selector NO se permite seleccionar carpetas
        if (this.selectorMode && file.folder) return;

        const sel = new Set(this.state.selected);
        if (sel.has(file.id)) {
            sel.delete(file.id);
            if (this.selectorMode) this._selectedCache.delete(file.id);
        } else {
            sel.add(file.id);
            if (this.selectorMode) {
                this._selectedCache.set(file.id, {
                    id: file.id,
                    name: file.name,
                    size: file.size,
                });
            }
        }
        this.state.selected = sel;
        this._notifySelectorChange();
    }

    isSelected(file) {
        return file ? this.state.selected.has(file.id) : false;
    }

    selectAll() {
        if (this.state.selected.size === this.state.filteredFiles.length) {
            // deseleccionar todos los visibles
            if (this.selectorMode) {
                for (const id of this.state.selected) {
                    this._selectedCache.delete(id);
                }
            }
            this.state.selected = new Set();
        } else {
            // En modo selector solo seleccionamos archivos, no carpetas
            const items = this.state.filteredFiles
                .filter(f => !this.selectorMode || !f.folder);
            this.state.selected = new Set(items.map(f => f.id));
            if (this.selectorMode) {
                for (const f of items) {
                    this._selectedCache.set(f.id, {
                        id: f.id,
                        name: f.name,
                        size: f.size,
                    });
                }
            }
        }
        this._notifySelectorChange();
    }

    /**
     * Notifica al diálogo padre (si estamos en modo selector) los
     * archivos elegidos acumulados a través de las distintas carpetas.
     */
    _notifySelectorChange() {
        if (!this.selectorMode) return;
        const cb = this.env.onedriveSelector && this.env.onedriveSelector.onSelectionChange;
        if (!cb) return;
        cb(Array.from(this._selectedCache.values()));
    }

    // =========================================================
    // CONTEXT MENU
    // =========================================================
    openCtxMenu(file, ev) {
        if (ev) {
            ev.preventDefault();
            ev.stopPropagation();
        }
        // Ajuste para que no se salga de la pantalla
        const x = Math.min(ev.clientX, window.innerWidth - 240);
        const y = Math.min(ev.clientY, window.innerHeight - 320);
        this.state.ctxMenu = { open: true, x, y, file };
    }

    closeCtxMenu() {
        if (this.state.ctxMenu.open) {
            this.state.ctxMenu = { open: false, x: 0, y: 0, file: null };
        }
    }

    // =========================================================
    // ACTIONS
    // =========================================================
    previewFile(file) {
        if (!file) return;
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

    downloadItem(file) {
        if (!file) return;
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
                    console.error("createFolder error:", e);
                    this.notify(_t("Error al crear"), "danger");
                }
            },
        });
    }

    renameItem(file) {
        if (!file) return;
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
                    await this._refresh();
                } catch (e) {
                    console.error("rename error:", e);
                    this.notify(_t("Error al renombrar"), "danger");
                }
            },
        });
    }

    deleteItem(file) {
        if (!file) return;
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
                    await this._refresh();
                } catch (e) {
                    console.error("delete error:", e);
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
                let ok = 0, ko = 0;
                for (const id of ids) {
                    try {
                        await rpc("/onedrive/delete", {
                            item_id: id,
                            account_id: this.state.accountId,
                        });
                        ok++;
                    } catch (e) {
                        ko++;
                    }
                }
                if (ko > 0) {
                    this.notify(_t("%s eliminados, %s con error", ok, ko), "warning");
                } else {
                    this.notify(_t("%s elementos eliminados", ok), "success");
                }
                await this._refresh();
            },
        });
    }

    shareItem(file) {
        if (!file) return;
        this.dialog.add(ShareDialog, {
            file,
            accountId: this.state.accountId,
        });
    }

    showProperties(file) {
        if (!file) return;
        this.dialog.add(PropertiesDialog, { file });
    }

    // =========================================================
    // UPLOAD
    // =========================================================
    triggerUpload() {
        if (this.fileInputRef.el) this.fileInputRef.el.click();
    }

    async uploadFile(ev) {
        const files = (ev && ev.target && ev.target.files) || ev;
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
        if (this.state.accountId) {
            formData.append("account_id", this.state.accountId);
        }
        if (this.state.currentFolder) {
            formData.append("parent_id", this.state.currentFolder);
        }

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

    // =========================================================
    // DRAG & DROP
    // =========================================================
    onDragOver(ev) {
        // En modo selector no permitimos subir archivos por drag&drop
        if (this.selectorMode) return;
        ev.preventDefault();
        if (ev.dataTransfer && ev.dataTransfer.types &&
            ev.dataTransfer.types.indexOf("Files") !== -1) {
            this.state.dragOver = true;
        }
    }

    onDragLeave(ev) {
        if (this.selectorMode) return;
        ev.preventDefault();
        // Solo desactivar si salimos al exterior del componente
        if (!this.rootRef.el || !this.rootRef.el.contains(ev.relatedTarget)) {
            this.state.dragOver = false;
        }
    }

    async onDrop(ev) {
        if (this.selectorMode) return;
        ev.preventDefault();
        this.state.dragOver = false;
        const files = ev.dataTransfer && ev.dataTransfer.files;
        if (files && files.length) await this.uploadFile(files);
    }

    // =========================================================
    // KEYBOARD
    // =========================================================
    onKeyDown(ev) {
        if (ev.key === "Escape") {
            this.state.selected = new Set();
            if (this.selectorMode) {
                this._selectedCache.clear();
                this._notifySelectorChange();
            }
            this.closeCtxMenu();
        }
        if (ev.key === "Delete" && this.state.selected.size > 0 && !this.selectorMode) {
            // Solo si no estamos en un input
            const tag = (ev.target && ev.target.tagName) || "";
            if (tag !== "INPUT" && tag !== "TEXTAREA") {
                this.deleteSelected();
            }
        }
    }

    // =========================================================
    // UTIL
    // =========================================================
    notify(msg, type = "info") {
        this.notification.add(msg, { type });
    }

    refresh() {
        this._refresh();
    }

    /** Refresca según el modo actual: búsqueda o navegación normal */
    async _refresh() {
        if (this.state.searching && this.state.search.trim()) {
            await this._doGlobalSearch(this.state.search.trim());
        } else {
            await this.loadFiles(this.state.currentFolder);
        }
    }
}

registry.category("actions").add("onedrive_app", OneDriveApp);