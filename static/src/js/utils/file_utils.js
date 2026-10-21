
/** @odoo-module **/

const ICON_MAP = {
    image: { icon: "bi-file-earmark-image", color: "#10b981" },
    video: { icon: "bi-file-earmark-play", color: "#ef4444" },
    audio: { icon: "bi-file-earmark-music", color: "#f59e0b" },
    pdf: { icon: "bi-file-earmark-pdf", color: "#dc2626" },
    doc: { icon: "bi-file-earmark-word", color: "#2563eb" },
    xls: { icon: "bi-file-earmark-excel", color: "#16a34a" },
    ppt: { icon: "bi-file-earmark-ppt", color: "#ea580c" },
    zip: { icon: "bi-file-earmark-zip", color: "#7c3aed" },
    code: { icon: "bi-file-earmark-code", color: "#0891b2" },
    text: { icon: "bi-file-earmark-text", color: "#475569" },
    folder: { icon: "bi-folder-fill", color: "#fbbf24" },
    default: { icon: "bi-file-earmark", color: "#64748b" },
};

const EXT_MAP = {
    jpg: "image", jpeg: "image", png: "image",
    gif: "image", webp: "image", bmp: "image",
    svg: "image", ico: "image", heic: "image",
    tif: "image", tiff: "image", avif: "image",

    mp4: "video", mov: "video", avi: "video",
    mkv: "video", webm: "video", wmv: "video",
    mpeg: "video", mpg: "video", m4v: "video",

    mp3: "audio", wav: "audio", ogg: "audio",
    flac: "audio", m4a: "audio", aac: "audio",
    wma: "audio",

    pdf: "pdf",

    doc: "doc", docx: "doc", docm: "doc",
    odt: "doc", rtf: "doc",

    xls: "xls", xlsx: "xls", xlsm: "xls",
    xlsb: "xls", csv: "xls", ods: "xls",

    ppt: "ppt", pptx: "ppt", pptm: "ppt",
    odp: "ppt",

    zip: "zip", rar: "zip", "7z": "zip",
    tar: "zip", gz: "zip", bz2: "zip",

    js: "code", ts: "code", py: "code",
    java: "code", cpp: "code", c: "code",
    h: "code", html: "code", css: "code",
    scss: "code", json: "code", xml: "code",
    yml: "code", yaml: "code", sh: "code",
    php: "code", rb: "code", go: "code",
    sql: "code",

    txt: "text", md: "text", log: "text",
};

const MIME_MAP = {
    "application/pdf": "pdf",
    "application/msword": "doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "doc",
    "application/vnd.ms-excel": "xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xls",
    "application/vnd.ms-powerpoint": "ppt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "ppt",
    "application/zip": "zip",
    "text/plain": "text",
    "text/csv": "xls",
};

function getExtension(filename) {
    const name = String(filename || "");
    const index = name.lastIndexOf(".");

    if (index <= 0 || index === name.length - 1) {
        return "";
    }

    return name.substring(index + 1).toLowerCase();
}

function getFileCategory(file) {
    if (!file) {
        return "default";
    }

    if (file.folder) {
        return "folder";
    }

    const extension = getExtension(file.name);
    const mimeType = String(file.file?.mimeType || "").toLowerCase();

    if (EXT_MAP[extension]) {
        return EXT_MAP[extension];
    }

    if (MIME_MAP[mimeType]) {
        return MIME_MAP[mimeType];
    }

    if (mimeType.startsWith("image/")) {
        return "image";
    }

    if (mimeType.startsWith("video/")) {
        return "video";
    }

    if (mimeType.startsWith("audio/")) {
        return "audio";
    }

    if (mimeType.startsWith("text/")) {
        return "text";
    }

    return "default";
}

export function getFileIcon(file) {
    return ICON_MAP[getFileCategory(file)] || ICON_MAP.default;
}

export function isImage(file) {
    return !!file && !file.folder && getFileCategory(file) === "image";
}

export function formatBytes(bytes) {
    if (bytes === undefined || bytes === null || bytes === "") {
        return "—";
    }

    const value = Number(bytes);

    if (!Number.isFinite(value) || value < 0) {
        return "—";
    }

    if (value === 0) {
        return "0 B";
    }

    const sizes = ["B", "KB", "MB", "GB", "TB", "PB"];
    const index = Math.min(
        Math.floor(Math.log(value) / Math.log(1024)),
        sizes.length - 1
    );

    const number = value / Math.pow(1024, index);

    return `${number.toLocaleString("es-PE", {
        minimumFractionDigits: index === 0 ? 0 : 1,
        maximumFractionDigits: index === 0 ? 0 : 1,
    })} ${sizes[index]}`;
}

export function formatDate(iso) {
    if (!iso) {
        return "—";
    }

    const date = new Date(iso);

    if (Number.isNaN(date.getTime())) {
        return "—";
    }

    const difference = Date.now() - date.getTime();

    // Las fechas futuras muestran su fecha real.
    if (difference < 0) {
        return date.toLocaleDateString("es-PE");
    }

    const seconds = Math.floor(difference / 1000);

    if (seconds < 60) {
        return "Ahora";
    }

    if (seconds < 3600) {
        return `Hace ${Math.floor(seconds / 60)} min`;
    }

    if (seconds < 86400) {
        return `Hace ${Math.floor(seconds / 3600)} h`;
    }

    if (seconds < 604800) {
        return `Hace ${Math.floor(seconds / 86400)} d`;
    }

    return date.toLocaleDateString("es-PE");
}
