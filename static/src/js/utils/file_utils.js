/** @odoo-module **/

const ICON_MAP = {
    image:  { icon: "bi-file-earmark-image",      color: "#10b981" },
    video:  { icon: "bi-file-earmark-play",       color: "#ef4444" },
    audio:  { icon: "bi-file-earmark-music",      color: "#f59e0b" },
    pdf:    { icon: "bi-file-earmark-pdf",        color: "#dc2626" },
    doc:    { icon: "bi-file-earmark-word",       color: "#2563eb" },
    xls:    { icon: "bi-file-earmark-excel",      color: "#16a34a" },
    ppt:    { icon: "bi-file-earmark-ppt",        color: "#ea580c" },
    zip:    { icon: "bi-file-earmark-zip",        color: "#7c3aed" },
    code:   { icon: "bi-file-earmark-code",       color: "#0891b2" },
    text:   { icon: "bi-file-earmark-text",       color: "#475569" },
    folder: { icon: "bi-folder-fill",             color: "#fbbf24" },
    default:{ icon: "bi-file-earmark",            color: "#64748b" },
};

const EXT_MAP = {
    jpg:"image", jpeg:"image", png:"image", gif:"image", webp:"image",
    bmp:"image", svg:"image", ico:"image", heic:"image",
    mp4:"video", mov:"video", avi:"video", mkv:"video", webm:"video", wmv:"video",
    mp3:"audio", wav:"audio", ogg:"audio", flac:"audio", m4a:"audio", aac:"audio",
    pdf:"pdf",
    doc:"doc", docx:"doc", odt:"doc", rtf:"doc",
    xls:"xls", xlsx:"xls", csv:"xls", ods:"xls",
    ppt:"ppt", pptx:"ppt", odp:"ppt",
    zip:"zip", rar:"zip", "7z":"zip", tar:"zip", gz:"zip",
    js:"code", ts:"code", py:"code", java:"code", cpp:"code", c:"code",
    h:"code", html:"code", css:"code", scss:"code", json:"code", xml:"code",
    yml:"code", yaml:"code", sh:"code", php:"code", rb:"code", go:"code",
    txt:"text", md:"text", log:"text",
};

export function getFileIcon(file) {
    if (!file) return ICON_MAP.default;
    if (file.folder) return ICON_MAP.folder;
    const name = file.name || "";
    const ext = name.split(".").pop().toLowerCase();
    const key = EXT_MAP[ext];
    return ICON_MAP[key] || ICON_MAP.default;
}

export function isImage(file) {
    if (!file || file.folder) return false;
    const name = file.name || "";
    const ext = name.split(".").pop().toLowerCase();
    return EXT_MAP[ext] === "image";
}

export function formatBytes(bytes) {
    if (bytes === undefined || bytes === null) return "—";
    if (bytes === 0) return "0 B";
    const sizes = ["B", "KB", "MB", "GB", "TB"];
    const i = Math.floor(Math.log(bytes) / Math.log(1024));
    return `${(bytes / Math.pow(1024, i)).toFixed(i ? 1 : 0)} ${sizes[i]}`;
}

export function formatDate(iso) {
    if (!iso) return "—";
    const d = new Date(iso);
    const now = new Date();
    const diff = (now - d) / 1000;
    if (diff < 60) return "Ahora";
    if (diff < 3600) return `Hace ${Math.floor(diff / 60)} min`;
    if (diff < 86400) return `Hace ${Math.floor(diff / 3600)} h`;
    if (diff < 604800) return `Hace ${Math.floor(diff / 86400)} d`;
    return d.toLocaleDateString();
}