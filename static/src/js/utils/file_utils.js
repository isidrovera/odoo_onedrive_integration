// static/src/js/utils/file_utils.js
/** @odoo-module **/

const ICON_MAP = {
    // Imágenes
    image: { icon: "fa-file-image", color: "#10b981", category: "image" },
    // Video
    video: { icon: "fa-file-video", color: "#ef4444", category: "video" },
    // Audio
    audio: { icon: "fa-file-audio", color: "#f59e0b", category: "audio" },
    // PDF
    pdf: { icon: "fa-file-pdf", color: "#dc2626", category: "pdf" },
    // Word
    doc: { icon: "fa-file-word", color: "#2563eb", category: "doc" },
    // Excel
    xls: { icon: "fa-file-excel", color: "#16a34a", category: "xls" },
    // PowerPoint
    ppt: { icon: "fa-file-powerpoint", color: "#ea580c", category: "ppt" },
    // Archive
    zip: { icon: "fa-file-archive", color: "#7c3aed", category: "archive" },
    // Code
    code: { icon: "fa-file-code", color: "#0891b2", category: "code" },
    // Text
    text: { icon: "fa-file-text", color: "#475569", category: "text" },
    // Folder
    folder: { icon: "fa-folder", color: "#fbbf24", category: "folder" },
    // Default
    default: { icon: "fa-file", color: "#64748b", category: "file" },
};

const EXT_MAP = {
    // images
    jpg: "image", jpeg: "image", png: "image", gif: "image", webp: "image",
    bmp: "image", svg: "image", ico: "image", heic: "image",
    // video
    mp4: "video", mov: "video", avi: "video", mkv: "video", webm: "video", wmv: "video",
    // audio
    mp3: "audio", wav: "audio", ogg: "audio", flac: "audio", m4a: "audio", aac: "audio",
    // pdf
    pdf: "pdf",
    // word
    doc: "doc", docx: "doc", odt: "doc", rtf: "doc",
    // excel
    xls: "xls", xlsx: "xls", csv: "xls", ods: "xls",
    // ppt
    ppt: "ppt", pptx: "ppt", odp: "ppt",
    // archive
    zip: "zip", rar: "zip", "7z": "zip", tar: "zip", gz: "zip",
    // code
    js: "code", ts: "code", py: "code", java: "code", cpp: "code", c: "code",
    h: "code", html: "code", css: "code", scss: "code", json: "code", xml: "code",
    yml: "code", yaml: "code", sh: "code", php: "code", rb: "code", go: "code",
    // text
    txt: "text", md: "text", log: "text",
};

export function getFileIcon(file) {
    if (file.folder) return ICON_MAP.folder;
    const name = file.name || "";
    const ext = name.split(".").pop().toLowerCase();
    const key = EXT_MAP[ext];
    return ICON_MAP[key] || ICON_MAP.default;
}

export function isImage(file) {
    return getFileIcon(file).category === "image";
}

export function isPreviewable(file) {
    if (file.folder) return false;
    const cat = getFileIcon(file).category;
    return ["image", "video", "audio", "pdf", "doc", "xls", "ppt", "text"].includes(cat);
}

export function formatBytes(bytes) {
    if (!bytes && bytes !== 0) return "—";
    const sizes = ["B", "KB", "MB", "GB", "TB"];
    if (bytes === 0) return "0 B";
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