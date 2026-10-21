/** @odoo-module **/
import { Component, useState, useRef, onWillStart, onMounted, onWillUnmount } from "@odoo/owl";
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
            locations: [],
            locationId: null,
            currentLocation: null,
            ctxMenu: { open: false, x: 0, y: 0, file: null },
            dragOver: false,
            detailFile: null,
            section: "documents", // documents | recent | favorites | shared
            favorites: new Set(),
            recentItems: [],
            versions: [],
            versionsLoading: false,
            versionFile: null,
            detailsTab: "details",
            activity: [],
            activityLoading: false,
            error: null,
            uploading: false,
            uploadResults: [],
        });

        this._searchDebounce = null;
        this._requestSeq = 0;
        this._uploadAbort = null;

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
            await this.loadLocations();
            if (this.state.locationId) await this.loadFiles();
        });

        onMounted(() => {
            document.addEventListener("click", this._onDocClick);
            document.addEventListener("keydown", this._onDocKey);
        });
        onWillUnmount(() => {
            document.removeEventListener("click", this._onDocClick);
            document.removeEventListener("keydown", this._onDocKey);
            if (this._searchDebounce) clearTimeout(this._searchDebounce);
            if (this._uploadAbort) this._uploadAbort.abort();
        });
    }

    // =========================================================
    // UBICACIONES: OneDrive y bibliotecas SharePoint autorizadas
    // =========================================================
    async loadLocations() {
        try {
            const res = await rpc("/microsoft/locations", {});
            this.state.locations = (res || []).filter((loc) => loc && loc.id && loc.role);
            if (this.state.locations.length) {
                const current = this.state.locations.find((loc) => loc.id === this.state.locationId);
                const target = current || this.state.locations[0];
                this.state.locationId = target.id;
                this.state.currentLocation = target;
            } else {
                this.state.locationId = null;
                this.state.currentLocation = null;
            }
            this.state.error = null;
        } catch (e) {
            console.error("loadLocations error:", e);
            this.state.locations = [];
            this.state.locationId = null;
            this.state.currentLocation = null;
            this.state.error = this.errorMessage(e);
        }
    }

    async onLocationChange(ev) {
        const id = Number(ev.target.value);
        this.state.locationId = id;
        this.state.currentLocation = this.state.locations.find((item) => item.id === id) || null;
        this.state.path = [];
        this.state.search = "";
        this.state.searching = false;
        this._selectedCache.clear();
        this.state.currentFolder = null;
        this.state.selected = new Set();
        this.state.detailFile = null;
        this.state.ctxMenu.open = false;
        this.state.section = "documents";
        await this.loadFiles();
    }

    canEdit() {
        return !!(this.state.currentLocation && this.state.currentLocation.can_edit);
    }

    canManage() {
        return !!(this.state.currentLocation && this.state.currentLocation.can_manage);
    }

    // =========================================================
    // LOAD
    // =========================================================
    async loadFiles(folderId = null) {
        if (!this.state.locationId) {
            this.state.loading = false;
            this.state.files = [];
            this.state.filteredFiles = [];
            return;
        }
        const seq = ++this._requestSeq;
        this.state.loading = true;
        this.state.error = null;
        // En modo selector NO reseteamos la selección al navegar:
        // el usuario puede haber elegido archivos en otras carpetas
        // y queremos mantenerlos. Solo limpiamos el Set visible.
        this.state.selected = new Set();
        try {
            const result = await rpc("/microsoft/list", {
                parent_id: folderId,
                location_id: this.state.locationId,
            });
            if (seq !== this._requestSeq) return;
            // Sanitizar respuesta: quedarnos solo con items válidos
            const raw = (result && result.value) || [];
            this.state.files = raw.filter(f => f && f.id && f.name);
            if (this.state.detailFile && !this.state.files.some(f => f.id === this.state.detailFile.id)) {
                this.state.detailFile = null;
            }
            this.state.currentFolder = folderId;
            if (folderId === null) this.state.path = [];
            this.applyFilterAndSort();
            this._rememberRecent(this.state.files);

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
            if (seq !== this._requestSeq) return;
            this.state.error = this.errorMessage(e);
            this.notify(_t("Error cargando archivos"), "danger");
            this.state.files = [];
            this.state.filteredFiles = [];
        } finally {
            if (seq === this._requestSeq) this.state.loading = false;
        }
    }

    applyFilterAndSort() {
        try {
            let list = (this.state.files || []).filter(f => f && f.id);

            if (this.state.section === "favorites") {
                list = list.filter(f => this.isFavorite(f));
            } else if (this.state.section === "recent") {
                list = list.filter(f => !!f.lastModifiedDateTime)
                    .sort((a, b) => (b.lastModifiedDateTime || "").localeCompare(a.lastModifiedDateTime || ""));
            } else if (this.state.section === "shared") {
                list = list.filter(f => !!f.shared);
            }
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
    // INFORMACIÓN PARA LA INTERFAZ MODERNA
    // =========================================================
    getStats() {
        const files = this.state.files || [];
        const now = Date.now();
        const todayLimit = 24 * 60 * 60 * 1000;
        return {
            files: files.filter(item => !item.folder).length,
            folders: files.filter(item => !!item.folder).length,
            modifiedToday: files.filter(item => {
                const value = Date.parse(item.lastModifiedDateTime || "");
                return Number.isFinite(value) && now - value >= 0 && now - value <= todayLimit;
            }).length,
            totalSize: files.reduce((total, item) => total + (item.folder ? 0 : Number(item.size || 0)), 0),
        };
    }

    getModifiedBy(file) {
        if (!file) return _t("Microsoft 365");
        const identity = file.lastModifiedBy || {};
        return (
            identity.user?.displayName ||
            identity.application?.displayName ||
            identity.device?.displayName ||
            _t("Microsoft 365")
        );
    }

    getModifierInitials(file) {
        const name = this.getModifiedBy(file).trim();
        if (!name) return "M";
        return name.split(/\s+/).slice(0, 2).map(part => part.charAt(0).toUpperCase()).join("");
    }

    formatRelativeDate(value) {
        const date = Date.parse(value || "");
        if (!Number.isFinite(date)) return _t("Sin fecha");
        const seconds = Math.max(0, Math.floor((Date.now() - date) / 1000));
        if (seconds < 60) return _t("Ahora");
        const minutes = Math.floor(seconds / 60);
        if (minutes < 60) return _t("Hace %s min", minutes);
        const hours = Math.floor(minutes / 60);
        if (hours < 24) return _t("Hace %s h", hours);
        const days = Math.floor(hours / 24);
        if (days === 1) return _t("Ayer");
        if (days < 30) return _t("Hace %s días", days);
        return this.formatDate(value);
    }

    getCurrentPath() {
        const location = this.state.currentLocation?.name || _t("Ubicación");
        const folders = (this.state.path || []).map(item => item.name).filter(Boolean);
        return [location, ...folders].join(" / ");
    }

    selectForDetails(file, ev) {
        if (ev) ev.stopPropagation();
        this.state.detailFile = file || null;
        this.state.detailsTab = "details";
        this.state.versions = [];
        this.state.versionFile = null;
    }

    closeDetails() {
        this.state.detailFile = null;
    }

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
        this.state.section = "documents";
        this.state.path.push({ id: file.id, name: file.name });
        await this.loadFiles(file.id);
    }

    /**
     * Abre el archivo en su app nativa de Microsoft (Office Online)
     * usando webUrl. Permite EDITAR Excel/Word/PowerPoint en el navegador.
     */
    openExternal(file) {
        if (file && file.webUrl && /^https:\/\//i.test(file.webUrl)) {
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
        this.state.section = "documents";
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
        // No consultar Microsoft Graph mientras el usuario escribe.
        // La búsqueda global se ejecuta únicamente al presionar Enter.
        if (!this.state.search.trim() && this.state.searching) {
            this.state.searching = false;
            await this.loadFiles(this.state.currentFolder);
        }
    }

    async onSearchSubmit(ev) {
        if (ev.key !== "Enter") return;
        ev.preventDefault();
        if (this._searchDebounce) {
            clearTimeout(this._searchDebounce);
            this._searchDebounce = null;
        }
        const q = this.state.search.trim();
        if (!q) return;
        await this._doGlobalSearch(q);
    }

    async _doGlobalSearch(q) {
        const seq = ++this._requestSeq;
        this.state.searching = true;
        this.state.loading = true;
        this.state.error = null;
        try {
            const res = await rpc("/microsoft/search", {
                query: q,
                location_id: this.state.locationId,
            });
            if (seq !== this._requestSeq) return;
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
            if (seq !== this._requestSeq) return;
            this.state.error = this.errorMessage(e);
            this.notify(_t("Error buscando"), "danger");
            this.state.files = [];
            this.state.filteredFiles = [];
        } finally {
            if (seq === this._requestSeq) this.state.loading = false;
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
                    location_id: this.state.locationId,
                    location_name: this.state.currentLocation?.name || "",
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
        const eligible = this.state.filteredFiles.filter(f => !this.selectorMode || !f.folder);
        if (eligible.length && eligible.every(f => this.state.selected.has(f.id))) {
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
                        location_id: this.state.locationId,
                        location_name: this.state.currentLocation?.name || "",
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
        if (this.selectorMode) return;
        if (ev) {
            ev.preventDefault();
            ev.stopPropagation();
        }
        // Ajuste para que no se salga de la pantalla
        const x = Math.max(8, Math.min(ev?.clientX || 0, window.innerWidth - 240));
        const y = Math.max(8, Math.min(ev?.clientY || 0, window.innerHeight - 320));
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
            locationId: this.state.locationId,
            onDownload: () => this.downloadItem(file),
            onShare: this.canManage() && !this.selectorMode ? () => this.shareItem(file) : undefined,
            onDelete: this.canManage() && !this.selectorMode ? () => this.deleteItem(file) : undefined,
            onOpenExternal: () => this.openExternal(file),
        });
    }

    downloadItem(file) {
        if (!file) return;
        const url = file.folder
            ? null
            : `/microsoft/download/${file.id}?location_id=${this.state.locationId}`;
        if (!url) {
            this.notify(_t("La descarga de carpetas completas no está habilitada para esta ubicación"), "warning");
            return;
        }
        window.open(url, "_blank");
    }

    async createFolder() {
        if (this.selectorMode || !this.canEdit()) return;
        this.dialog.add(PromptDialog, {
            title: _t("Nueva carpeta"),
            label: _t("Nombre de la carpeta"),
            placeholder: _t("Mi carpeta"),
            icon: "bi-folder-plus",
            confirmLabel: _t("Crear"),
            onConfirm: async (name) => {
                try {
                    await rpc("/microsoft/create_folder", {
                        name,
                        parent_id: this.state.currentFolder,
                        location_id: this.state.locationId,
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
        if (!file || !this.canEdit() || this.selectorMode) return;
        this.dialog.add(PromptDialog, {
            title: _t("Renombrar"),
            label: _t("Nuevo nombre"),
            value: file.name,
            icon: "bi-pencil",
            confirmLabel: _t("Renombrar"),
            onConfirm: async (name) => {
                try {
                    await rpc("/microsoft/rename", {
                        item_id: file.id,
                        new_name: name,
                        location_id: this.state.locationId,
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
        if (!file || !this.canManage() || this.selectorMode) return;
        this.dialog.add(ConfirmDialog, {
            title: _t("Eliminar elemento"),
            message: _t('¿Seguro que deseas eliminar "%s"?', file.name),
            description: _t("Se moverá a la papelera de OneDrive."),
            icon: "bi-trash",
            danger: true,
            confirmLabel: _t("Eliminar"),
            onConfirm: async () => {
                try {
                    await rpc("/microsoft/delete", {
                        item_id: file.id,
                        location_id: this.state.locationId,
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
        if (!this.canManage() || this.selectorMode) return;
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
                        await rpc("/microsoft/delete", {
                            item_id: id,
                            location_id: this.state.locationId,
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
        if (!file || !this.canManage() || this.selectorMode) return;
        this.dialog.add(ShareDialog, {
            file,
            locationId: this.state.locationId,
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
        if (this.selectorMode || !this.canEdit() || this.state.uploading) return;
        if (this.fileInputRef.el) this.fileInputRef.el.click();
    }

    async uploadFile(ev) {
        if (this.selectorMode || !this.canEdit() || this.state.uploading) return;
        const files = Array.from((ev && ev.target && ev.target.files) || ev || []);
        if (!files.length) return;
        const locationId = this.state.locationId;
        const parentId = this.state.currentFolder;
        this.state.uploading = true;
        this.state.uploadResults = [];
        try {
            for (const file of files) {
                if (!this.state.locations.some(loc => loc.id === locationId)) break;
                const result = await this._uploadOne(file, locationId, parentId);
                this.state.uploadResults.push({ name: file.name, ok: result.ok, error: result.error || null });
            }
        } finally {
            this.state.uploading = false;
            this.state.uploadProgress = null;
            if (this.fileInputRef.el) this.fileInputRef.el.value = "";
            if (locationId === this.state.locationId && parentId === this.state.currentFolder) {
                await this.loadFiles(parentId);
            }
            const failed = this.state.uploadResults.filter(r => !r.ok).length;
            if (failed) this.notify(_t("Fallaron %s de %s archivos", failed, files.length), "warning");
        }
    }

    async _uploadOne(file, locationId = this.state.locationId, parentId = this.state.currentFolder) {
        this.state.uploadProgress = { name: file.name, percent: 0 };
        const data = new FormData();
        data.append("file", file);
        data.append("location_id", String(locationId));
        if (parentId) data.append("parent_id", String(parentId));
        if (typeof odoo !== "undefined" && odoo.csrf_token) data.append("csrf_token", odoo.csrf_token);
        return new Promise((resolve) => {
            const xhr = new XMLHttpRequest();
            this._uploadAbort = xhr;
            let finished = false;
            const done = (ok, error) => {
                if (finished) return;
                finished = true;
                this._uploadAbort = null;
                this.state.uploadProgress = null;
                if (ok) this.notify(_t("Subido: %s", file.name), "success");
                else this.notify(_t("Error subiendo: %s", file.name), "danger");
                resolve({ ok, error });
            };
            xhr.open("POST", "/microsoft/upload");
            xhr.upload.onprogress = (e) => {
                if (e.lengthComputable) {
                    this.state.uploadProgress = {
                        name: file.name,
                        percent: Math.min(99, Math.round(e.loaded * 100 / e.total)),
                    };
                }
            };
            xhr.onload = () => {
                let body = null;
                try { body = JSON.parse(xhr.responseText || "{}"); } catch (_ignored) { /* Invalid response */ }
                const ok = xhr.status >= 200 && xhr.status < 300 && body?.status === "ok" && !!body.item;
                done(ok, ok ? null : body?.message || `HTTP ${xhr.status}`);
            };
            xhr.onerror = () => done(false, _t("Error de red"));
            xhr.onabort = () => done(false, _t("Carga cancelada"));
            xhr.ontimeout = () => done(false, _t("La carga excedió el tiempo permitido"));
            xhr.timeout = 30 * 60 * 1000;
            xhr.send(data);
        });
    }

    cancelUpload() {
        this._uploadAbort?.abort();
    }

    // =========================================================
    // DRAG & DROP
    // =========================================================
    onDragOver(ev) {
        // En modo selector no permitimos subir archivos por drag&drop
        if (this.selectorMode || !this.canEdit()) return;
        ev.preventDefault();
        if (ev.dataTransfer && ev.dataTransfer.types &&
            ev.dataTransfer.types.indexOf("Files") !== -1) {
            this.state.dragOver = true;
        }
    }

    onDragLeave(ev) {
        if (this.selectorMode || !this.canEdit()) return;
        ev.preventDefault();
        // Solo desactivar si salimos al exterior del componente
        if (!this.rootRef.el || !this.rootRef.el.contains(ev.relatedTarget)) {
            this.state.dragOver = false;
        }
    }

    async onDrop(ev) {
        if (this.selectorMode || !this.canEdit()) return;
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
        if (ev.key === "Delete" && this.state.selected.size > 0 && !this.selectorMode && this.canManage() && !ev.repeat) {
            // Solo si no estamos en un input
            const tag = (ev.target && ev.target.tagName) || "";
            if (tag !== "INPUT" && tag !== "TEXTAREA") {
                this.deleteSelected();
            }
        }
    }

    // =========================================================
    // SECCIONES, FAVORITOS Y ACTIVIDAD
    // =========================================================
    async setSection(section) {
        if (!["documents", "recent", "favorites", "shared"].includes(section)) return;
        if (this.selectorMode && section !== "documents") return;
        this.state.section = section;
        this.state.searching = false;
        this.state.search = "";
        if (section === "documents") await this.loadFiles(this.state.currentFolder);
        else this.applyFilterAndSort();
    }

    _favoriteKey(file) {
        return `${this.state.locationId}:${file?.id || ""}`;
    }

    isFavorite(file) { return !!file && this.state.favorites.has(this._favoriteKey(file)); }

    toggleFavorite(file, ev) {
        ev?.stopPropagation();
        if (!file || this.selectorMode) return;
        const key = this._favoriteKey(file);
        const set = new Set(this.state.favorites);
        if (set.has(key)) set.delete(key); else set.add(key);
        this.state.favorites = set;
        this.applyFilterAndSort();
    }

    _rememberRecent(files) {
        // Recentes de la sesión del explorador, no historial global de Microsoft.
        const known = new Map(this.state.recentItems.map(f => [`${f.location_id}:${f.id}`, f]));
        for (const file of files) {
            if (file?.id && !file.folder) known.set(`${this.state.locationId}:${file.id}`,
                { ...file, location_id: this.state.locationId });
        }
        this.state.recentItems = Array.from(known.values())
            .sort((a, b) => (b.lastModifiedDateTime || "").localeCompare(a.lastModifiedDateTime || ""))
            .slice(0, 100);
    }

    async loadVersions(file) {
        if (!file || file.folder) return;
        this.state.versionFile = file;
        this.state.versions = [];
        this.state.versionsLoading = true;
        try {
            const res = await rpc("/microsoft/versions", {
                item_id: file.id,
                location_id: this.state.locationId,
            });
            this.state.versions = res?.value || [];
        } catch (e) {
            this.notify(this.errorMessage(e), "danger");
        } finally {
            this.state.versionsLoading = false;
        }
    }

    restoreVersion(file, version) {
        if (!this.canManage() || !file || !version?.id || this.selectorMode) return;
        this.dialog.add(ConfirmDialog, {
            title: _t("Restaurar versión"),
            message: _t("¿Restaurar la versión %s de %s?", version.id, file.name),
            confirmLabel: _t("Restaurar"),
            onConfirm: async () => {
                await rpc("/microsoft/restore_version", {
                    item_id: file.id,
                    version_id: version.id,
                    location_id: this.state.locationId,
                });
                this.notify(_t("Versión restaurada"), "success");
                await this.loadVersions(file);
                await this._refresh();
            },
        });
    }

    async showActivity(file) {
        // La actividad documental requiere una ruta autorizada de lectura de auditoría.
        this.state.detailsTab = "activity";
        this.state.activity = [];
        this.state.activityLoading = false;
        if (file) this.state.detailFile = file;
    }

    errorMessage(e) {
        return e?.data?.message || e?.message || _t("Ocurrió un error inesperado");
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
