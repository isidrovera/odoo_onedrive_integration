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

        this.folderInputRef = useRef("folderInput");

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

            createMenuOpen: false,
            moveDialog: { open: false, action: "move", parentId: null, path: [], folders: [], busy: false, loading: false },

            creatingDocument: false,

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

        this._onCreateOutsideClick = this.onCreateOutsideClick.bind(this);

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

            document.addEventListener("click", this._onCreateOutsideClick);

            document.addEventListener("keydown", this._onDocKey);

        });

        onWillUnmount(() => {

            document.removeEventListener("click", this._onDocClick);

            document.removeEventListener("click", this._onCreateOutsideClick);

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

    getThumbnailUrl(file, size = "medium") {

        if (!file?.id || file.folder || !this.state.locationId) return "";

        const safeSize = ["small", "medium", "large"].includes(size) ? size : "medium";

        return `/microsoft/thumbnail/${encodeURIComponent(String(file.id))}?location_id=${encodeURIComponent(String(this.state.locationId))}&size=${safeSize}`;

    }

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

    getCreatedBy(file) {
        const identity = file?.createdBy || {};
        return file?.createdByOdoo || identity.user?.displayName || identity.application?.displayName ||
            identity.device?.displayName || "No identificado por Microsoft";
    }

    getCreatorInitials(file) {
        const name = this.getCreatedBy(file).trim();
        return name.split(/\s+/).slice(0, 2).map(s => s.charAt(0).toUpperCase()).join("") || "?";
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

        if (file && file.webUrl && /^https?:\/\//i.test(file.webUrl)) {

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

    // Mover/copiar por lotes dentro de la misma unidad SharePoint.
    getSelectedItems() {
        return (this.state.files || []).filter(f => this.state.selected.has(f.id));
    }

    clearSelectedItems() {
        this.state.selected = new Set();
        this.state.detailFile = null;
    }

    async openMoveDialog(action = "move", files = null) {
        if (this.selectorMode || !this.canEdit()) return;
        const selected = files || this.getSelectedItems();
        if (!selected.length) return;
        this._operationItems = selected.map(f => ({ id: f.id, name: f.name, folder: !!f.folder }));
        this.closeCtxMenu();
        this.state.moveDialog = {
            open: true, action, parentId: null, path: [], folders: [],
            busy: false, loading: true,
        };
        await this.loadMoveFolders(null);
    }

    closeMoveDialog() {
        if (this.state.moveDialog.busy) return;
        this.state.moveDialog.open = false;
        this._operationItems = [];
    }

    async loadMoveFolders(parentId, path = []) {
        const dlg = this.state.moveDialog;
        dlg.loading = true;
        try {
            const response = await rpc("/microsoft/list", {
                location_id: this.state.locationId, parent_id: parentId,
            });
            dlg.parentId = parentId;
            dlg.path = path;
            const blocked = new Set((this._operationItems || []).filter(f => f.folder).map(f => f.id));
            dlg.folders = (response.value || []).filter(f => f.folder && !blocked.has(f.id));
        } catch (err) {
            this.notify(this.errorMessage(err), "danger");
        } finally {
            dlg.loading = false;
        }
    }

    async executeMoveDialog() {
        const dlg = this.state.moveDialog;
        if (!dlg.open || dlg.busy || dlg.loading || !this.canEdit()) return;
        const items = [...(this._operationItems || [])];
        if (!items.length) return;
        dlg.busy = true;
        let completed = 0;
        const failures = [];
        const endpoint = dlg.action === "copy" ? "/microsoft/copy" : "/microsoft/move";
        for (const file of items) {
            try {
                await rpc(endpoint, {
                    item_id: file.id,
                    target_parent_id: dlg.parentId,
                    location_id: this.state.locationId,
                });
                completed++;
            } catch (err) {
                failures.push(file.name + ": " + this.errorMessage(err));
            }
        }
        dlg.busy = false;
        dlg.open = false;
        this._operationItems = [];
        this.clearSelectedItems();
        await this.loadFiles(this.state.currentFolder);
        if (completed) this.notify(`${completed} elemento(s) ${endpoint.endsWith("copy") ? "en proceso de copia" : "movido(s)"}`, "success");
        if (failures.length) this.notify(`${failures.length} error(es): ${failures.slice(0, 2).join("; ")}`, "danger");
    }

    downloadSelected() {
        if (this.selectorMode) return;
        const files = this.getSelectedItems();
        if (!files.length) return;
        if (files.some(f => f.folder)) {
            this.notify("Para descargar un ZIP, selecciona solamente archivos.", "warning");
            return;
        }
        if (files.length === 1) return this.downloadItem(files[0]);
        if (files.length > 25) {
            this.notify("Puedes descargar hasta 25 archivos por ZIP.", "warning");
            return;
        }
        const ids = files.map(f => encodeURIComponent(f.id)).join(",");
        window.open(`/microsoft/download_zip?location_id=${encodeURIComponent(this.state.locationId)}&ids=${ids}`, "_blank", "noopener");
    }

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

    // =========================================================

    // CREAR O SUBIR: menú único y creación real en SharePoint

    // =========================================================

    toggleCreateMenu() {

        if (this.selectorMode || !this.canEdit() || this.state.uploading || this.state.creatingDocument) {

            return;

        }

        this.state.createMenuOpen = !this.state.createMenuOpen;

    }

    closeCreateMenu() {

        this.state.createMenuOpen = false;

    }

    onCreateOutsideClick(ev) {

        if (!this.state.createMenuOpen) return;

        const element = ev.target;

        if (!element?.closest?.(".od_create_dropdown")) this.closeCreateMenu();

    }

    createMenuUpload(kind) {

        this.closeCreateMenu();

        if (kind === "folder") this.triggerFolderUpload();

        else this.triggerUpload();

    }

    createMenuFolder() {

        this.closeCreateMenu();

        this.createFolder();

    }

    /**

     * Tipos admitidos por DriveService.DOCUMENT_TYPES. El servidor valida

     * de nuevo tipo, nombre, ubicación y permiso de escritura.

     */

    createNewDocument(docType) {

        this.closeCreateMenu();

        if (this.selectorMode || !this.canEdit() || this.state.creatingDocument) return;

        const types = {

            word: ["Documento de Word", "Documento", "bi-file-earmark-word"],

            excel: ["Libro de Excel", "Libro", "bi-file-earmark-excel"],

            powerpoint: ["Presentación de PowerPoint", "Presentación", "bi-file-earmark-ppt"],

            text: ["Documento de texto", "Documento", "bi-file-earmark-text"],

            markdown: ["Documento Markdown", "Documento", "bi-markdown"],

            csv: ["Archivo CSV", "Datos", "bi-filetype-csv"],

            html: ["Documento HTML", "Documento", "bi-filetype-html"],

            json: ["Documento JSON", "Datos", "bi-filetype-json"],

        };

        const info = types[docType];

        if (!info) {

            this.notify(_t("Este formato no está disponible"), "warning");

            return;

        }

        const locationId = this.state.locationId;

        const parentId = this.state.currentFolder;

        if (!locationId) return;

        this.dialog.add(PromptDialog, {

            title: _t("Crear %s", info[0]),

            label: _t("Nombre del documento"),

            placeholder: info[1],

            icon: info[2],

            confirmLabel: _t("Crear"),

            onConfirm: async (name) => {

                if (!name || !String(name).trim()) {

                    this.notify(_t("Escribe un nombre para el documento"), "warning");

                    return;

                }

                if (this.state.creatingDocument) return;

                this.state.creatingDocument = true;

                try {

                    const item = await rpc("/microsoft/create_document", {

                        name: String(name).trim(),

                        doc_type: docType,

                        parent_id: parentId,

                        location_id: locationId,

                        content: "",

                    });

                    console.info("[MICROSOFT][CREATE_DOCUMENT][SUCCESS]", {

                        type: docType, itemId: item?.id, locationId,

                    });

                    this.notify(_t("Documento creado: %s", item?.name || name), "success");

                    if (this.state.locationId === locationId && this.state.currentFolder === parentId) {

                        await this.loadFiles(parentId);

                    }

                    // Abrir únicamente el vínculo web devuelto por Graph.

                    if (item?.webUrl && /^https?:\/\//i.test(item.webUrl)) {

                        window.open(item.webUrl, "_blank", "noopener,noreferrer");

                    }

                } catch (error) {

                    console.error("[MICROSOFT][CREATE_DOCUMENT][ERROR]", error);

                    this.notify(this.errorMessage(error), "danger");

                } finally {

                    this.state.creatingDocument = false;

                }

            },

        });

    }

    /** Crea un fichero .url, sin presentarlo como vínculo nativo de SharePoint. */

    createNewLink() {

        this.closeCreateMenu();

        if (this.selectorMode || !this.canEdit() || this.state.creatingDocument) return;

        const locationId = this.state.locationId;

        const parentId = this.state.currentFolder;

        this.dialog.add(PromptDialog, {

            title: _t("Nuevo vínculo"),

            label: _t("Nombre del vínculo"),

            placeholder: _t("Enlace de referencia"),

            icon: "bi-link-45deg",

            confirmLabel: _t("Continuar"),

            onConfirm: (name) => {

                if (!name || !String(name).trim()) {

                    this.notify(_t("Escribe un nombre para el vínculo"), "warning");

                    return;

                }

                this.dialog.add(PromptDialog, {

                    title: _t("Dirección del vínculo"),

                    label: _t("URL HTTPS"),

                    placeholder: "https\://",

                    icon: "bi-link-45deg",

                    confirmLabel: _t("Crear vínculo"),

                    onConfirm: async (targetUrl) => {

                        let address;

                        try {

                            address = new URL(String(targetUrl || "").trim());

                            if (address.protocol !== "https:" || address.username || address.password) {

                                throw new Error("invalid_url");

                            }

                        } catch (_error) {

                            this.notify(_t("Indica una dirección HTTPS válida"), "warning");

                            return;

                        }

                        if (this.state.creatingDocument) return;

                        this.state.creatingDocument = true;

                        try {

                            const item = await rpc("/microsoft/create_link", {

                                name: String(name).trim(),

                                target_url: address.toString(),

                                parent_id: parentId,

                                location_id: locationId,

                            });

                            console.info("[MICROSOFT][CREATE_LINK][SUCCESS]", {

                                itemId: item?.id, locationId,

                            });

                            this.notify(_t("Vínculo creado: %s", item?.name || name), "success");

                            if (this.state.locationId === locationId && this.state.currentFolder === parentId) {

                                await this.loadFiles(parentId);

                            }

                        } catch (error) {

                            console.error("[MICROSOFT][CREATE_LINK][ERROR]", error);

                            this.notify(this.errorMessage(error), "danger");

                        } finally {

                            this.state.creatingDocument = false;

                        }

                    },

                });

            },

        });

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

        this.fileInputRef.el?.click();

    }

    triggerFolderUpload() {

        if (this.selectorMode || !this.canEdit() || this.state.uploading) return;

        this.folderInputRef.el?.click();

    }

    async uploadFile(ev) {

        if (this.selectorMode || !this.canEdit() || this.state.uploading) return;

        const files = Array.from(ev?.target?.files || ev || []);

        if (!files.length) return;

        const locationId = this.state.locationId;

        const parentId = this.state.currentFolder;

        this.state.uploading = true;

        this._uploadCancelled = false;

        this.state.uploadResults = [];

        const folderCache = new Map();

        folderCache.set("", parentId);

        try {

            for (const file of files) {

                if (this._uploadCancelled) break;

                if (locationId !== this.state.locationId) break;

                const relative = (file.webkitRelativePath || file.name).replace(/\\/g, "/");

                const segments = relative.split("/").filter(Boolean);

                if (!segments.length || segments.some(seg => seg === "." || seg === "..")) continue;

                let folderId = parentId;

                let path = "";

                let failed = null;

                for (const segment of segments.slice(0, -1)) {

                    path = path ? `${path}/${segment}` : segment;

                    if (!folderCache.has(path)) {

                        try {

                            const folder = await rpc("/microsoft/ensure_folder", {

                                name: segment, parent_id: folderId, location_id: locationId,

                            });

                            folderCache.set(path, folder.id);

                        } catch (error) {

                            failed = this.errorMessage(error);

                            break;

                        }

                    }

                    folderId = folderCache.get(path);

                }

                const result = failed

                    ? {ok: false, error: failed}

                    : await this._uploadOne(file, locationId, folderId);

                this.state.uploadResults.push({name: relative, ok: result.ok, error: result.error || null});

            }

        } finally {

            this.state.uploading = false;

            this.state.uploadProgress = null;

            this._uploadAbort = null;

            if (this.fileInputRef.el) this.fileInputRef.el.value = "";

            if (this.folderInputRef.el) this.folderInputRef.el.value = "";

            if (locationId === this.state.locationId && parentId === this.state.currentFolder) {

                await this.loadFiles(parentId);

            }

            const failed = this.state.uploadResults.filter(r => !r.ok);

            if (failed.length) {

                this.notify(_t("Fallaron %s de %s archivos. Primer error: %s", failed.length,

                    this.state.uploadResults.length, failed[0].error || "Desconocido"), "warning");

            }

        }

    }

    async _uploadOne(file, locationId = this.state.locationId, parentId = this.state.currentFolder) {

        // Cada petición al servidor transporta como máximo 5 MiB, no el archivo completo.

        const chunkSize = 5 * 1024 * 1024;

        this.state.uploadProgress = {name: file.name, percent: 0};

        try {

            const session = await rpc("/microsoft/upload_session", {

                name: file.name, size: file.size, parent_id: parentId, location_id: locationId,

            });

            let offset = 0;

            let lastItem = null;

            while (offset < file.size) {

                if (this._uploadCancelled) throw new Error("Carga cancelada");

                const end = Math.min(offset + chunkSize, file.size);

                const blob = file.slice(offset, end);

                let successful = false;

                for (let attempt = 0; attempt < 4 && !successful; attempt++) {

                    try {

                        const result = await this._sendUploadChunk(blob, session, offset, end, file.size, locationId);

                        if (result.item?.id) {

                            lastItem = result.item;

                            offset = file.size;

                        } else {

                            const next = Number(result.next_offset);

                            if (!Number.isSafeInteger(next) || next <= offset || next > file.size) {

                                throw new Error("Microsoft devolvió una posición de subida incorrecta.");

                            }

                            offset = next;

                        }

                        successful = true;

                    } catch (error) {

                        if (error?.name === "AbortError" || error?.message === "Carga cancelada") throw error;

                        // Consultar la posición real evita repetir fragmentos ya aceptados.

                        try {

                            const status = await rpc("/microsoft/upload_status", {

                                upload_url: session.upload_url, proof: session.proof, location_id: locationId,

                            });

                            if (status.item?.id) {

                                lastItem = status.item;

                                offset = file.size;

                                successful = true;

                                break;

                            }

                            const next = Number(status.next_offset);

                            if (Number.isSafeInteger(next) && next > offset) {

                                offset = next;

                                successful = true;

                                break;

                            }

                        } catch (_statusError) { /* El reintento mostrará el error original. */ }

                        if (attempt === 3) throw error;

                        await new Promise(resolve => setTimeout(resolve, 700 * (attempt + 1)));

                    }

                }

                this.state.uploadProgress = {

                    name: file.name, percent: Math.min(99, Math.floor(offset * 100 / file.size)),

                };

            }

            if (!lastItem?.id) {

                // Microsoft puede confirmar el último chunk sin retornar item en estado intermedio.

                const result = await rpc("/microsoft/upload_complete", {

                    upload_url: session.upload_url, proof: session.proof, location_id: locationId,

                });

                lastItem = result.item;

            }

            if (!lastItem?.id) throw new Error("No se confirmó la subida del archivo.");

            this.state.uploadProgress = {name: file.name, percent: 100};

            this.notify(_t("Subido: %s", file.name), "success");

            return {ok: true};

        } catch (error) {

            const message = this.errorMessage(error);

            this.notify(_t("Error subiendo: %s", file.name), "danger");

            return {ok: false, error: message};

        } finally {

            this.state.uploadProgress = null;

            this._uploadAbort = null;

        }

    }

    _sendUploadChunk(blob, session, start, end, total, locationId) {

        return new Promise((resolve, reject) => {

            const xhr = new XMLHttpRequest();

            this._uploadAbort = xhr;

            xhr.open("POST", "/microsoft/upload_chunk");

            const data = new FormData();

            data.append("chunk", blob, "fragment.bin");

            data.append("upload_url", session.upload_url);

            data.append("proof", session.proof);

            data.append("location_id", String(locationId));

            data.append("start", String(start));

            data.append("end", String(end));

            data.append("total", String(total));

            if (typeof odoo !== "undefined" && odoo.csrf_token) data.append("csrf_token", odoo.csrf_token);

            xhr.timeout = 180000;

            xhr.onload = () => {

                let response;

                try { response = JSON.parse(xhr.responseText || "{}"); }

                catch (_error) { return reject(new Error(`Respuesta inválida (HTTP ${xhr.status})`)); }

                if (xhr.status >= 200 && xhr.status < 300 && response.status === "ok") resolve(response);

                else reject(new Error(response.message || `HTTP ${xhr.status}`));

            };

            xhr.onerror = () => reject(new Error("Fallo de red durante la subida"));

            xhr.onabort = () => reject(new Error("Carga cancelada"));

            xhr.ontimeout = () => reject(new Error("Tiempo de espera agotado"));

            xhr.send(data);

        });

    }

    cancelUpload() { this._uploadCancelled = true; this._uploadAbort?.abort(); }

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

            this.closeCreateMenu();

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
