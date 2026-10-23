# -*- coding: utf-8 -*-

"""Operaciones documentales Microsoft Graph para ubicaciones autorizadas Odoo 19.



Este servicio opera únicamente dentro del drive de la ubicación recibida.

Las autorizaciones de operaciones se comprueban también en el servidor.

"""

import io
import zipfile
from xml.sax.saxutils import escape as xml_escape

import logging

import re

import time

from urllib.parse import quote, urlsplit



import requests



from odoo.exceptions import AccessError, UserError



from .graph_client import GraphClient



_logger = logging.getLogger(__name__)





class DriveService:

    SMALL_UPLOAD_LIMIT = 4 * 1024 * 1024

    CHUNK_SIZE = 5 * 1024 * 1024  # Múltiplo de 320 KiB obligatorio en Graph.

    UPLOAD_TIMEOUT = 180

    MAX_CHUNK_RETRIES = 4

    MAX_FILE_NAME = 255

    _INVALID_FILE_CHARS = re.compile(r'["*:<>?/\\|\x00-\x1f]')

    _ALLOWED_DOWNLOAD_DOMAINS = (

        "sharepoint.com", "sharepoint-df.com", "sharepointonline.com",

        "1drv.com", "onedrive.com", "onedrive.live.com",

        "svc.ms", "livefilestore.com", "storage.live.com",

    )



    def __init__(self, location, user=None):

        location.ensure_one()

        self.location = location

        self.env = location.env

        self.user = user or self.env.user

        self.tenant = location.tenant_id

        if not location.active or not self.tenant:

            raise AccessError("La ubicación documental o la conexión Microsoft no están activas.")



        self.connection = self.env["microsoft.user.connection"].sudo().search([

            ("tenant_id", "=", self.tenant.id),

            ("user_id", "=", self.user.id),

            ("active", "=", True),

        ], limit=1)



        # SharePoint/drive explícito: preferir identidad delegada conectada,

        # conservando la autenticación de aplicación que ya usa el módulo.

        if self.connection and self.connection.state == "connected":

            token_provider = lambda: self.connection.with_user(self.user).get_valid_token()

        elif (

            location.location_type == "onedrive"

            and self.user.has_group("odoo_onedrive_integration.group_microsoft_admin")

            and bool(self.tenant.sudo().refresh_token)

        ):

            # El refresh token es un campo protegido. Solo se consulta con sudo

            # despues de confirmar el rol administrador, nunca desde el cliente.

            token_provider = lambda: self.tenant.get_valid_token()

        elif location.location_type == "sharepoint" or location.drive_id:

            token_provider = lambda: self.tenant.get_app_token("graph")

        else:

            raise UserError("Conecte su identidad Microsoft desde Mi conexión antes de abrir OneDrive.")



        self.client = GraphClient(token_provider=token_provider, env=self.env)

        self.drive_base = (

            f"/drives/{self._segment(location.drive_id)}"

            if location.drive_id else "/me/drive"

        )



    @staticmethod

    def _segment(value):

        if not isinstance(value, str) or not value.strip():

            raise UserError("El identificador documental es obligatorio.")

        value = value.strip()

        if len(value) > 512 or any(ord(c) < 32 for c in value):

            raise UserError("El identificador documental no es válido.")

        return quote(value, safe="")



    @classmethod

    def _filename(cls, name):

        if not isinstance(name, str):

            raise UserError("Debe indicar un nombre de documento válido.")

        name = name.strip()

        if (

            not name or name in (".", "..")

            or len(name) > cls.MAX_FILE_NAME

            or cls._INVALID_FILE_CHARS.search(name)

            or name.endswith(".")

        ):

            raise UserError("El nombre contiene caracteres no permitidos por Microsoft 365.")

        return name



    def _check(self, operation):

        # La verificación en el controlador no sustituye a la de servicio.

        return self.location.check_operation(operation, self.user)



    def _item(self, item_id):

        return f"{self.drive_base}/items/{self._segment(item_id)}"



    def _children(self, parent_id):

        return (f"{self._item(parent_id)}/children" if parent_id

                else f"{self.drive_base}/root/children")



    @classmethod
    def _validate_temporary_url(cls, url, *, extra_hosts=()):
        """Validar URL temporal evitando SSRF y filtración de credenciales.

        Se admite una lista explícita de hosts adicionales solo para miniaturas,
        obtenida desde configuración administrativa. Nunca se siguen redirecciones.
        No se registra la URL completa: contiene credenciales temporales.
        """
        if not isinstance(url, str) or not url or len(url) > 10000:
            raise UserError("Microsoft no devolvió una URL temporal válida.")
        try:
            parts = urlsplit(url)
            host = (parts.hostname or "").lower().rstrip(".")
            port = parts.port
        except ValueError as exc:
            raise UserError("Microsoft devolvió una URL temporal mal formada.") from exc

        # La lista extra son nombres EXACTOS, sin subdominios automáticos.
        extras = {
            value.lower().strip().rstrip(".")
            for value in extra_hosts
            if isinstance(value, str) and value.strip()
        }
        matches_microsoft = any(
            host == domain or host.endswith("." + domain)
            for domain in cls._ALLOWED_DOWNLOAD_DOMAINS
        )
        allowed_host = matches_microsoft or host in extras
        valid_structure = (
            parts.scheme.lower() == "https"
            and bool(host)
            and parts.username is None
            and parts.password is None
            and port in (None, 443)
            and not parts.fragment
            and not any(ord(ch) < 32 for ch in url)
        )
        if not valid_structure or not allowed_host:
            _logger.warning(
                "[MICROSOFT][TEMP_URL][REJECT] host=%s allowed=%s "
                "https=%s port=%s userinfo=%s",
                host[:253], allowed_host, parts.scheme.lower() == "https",
                port, bool(parts.username or parts.password),
            )
            raise UserError(
                "La dirección temporal no pertenece a un dominio autorizado de Microsoft. "
                "Revise el host registrado en los logs de Odoo."
            )
        _logger.debug("[MICROSOFT][TEMP_URL][OK] host=%s", host)
        return url

    def _thumbnail_extra_hosts(self):
        """Hosts adicionales explícitos aprobados por el administrador.

        Parámetro opcional: microsoft365.thumbnail_extra_hosts
        Lista separada por comas o saltos de línea, solo nombres DNS exactos.
        """
        raw = self.env["ir.config_parameter"].sudo().get_param(
            "microsoft365.thumbnail_extra_hosts", ""
        ) or ""
        hosts = []
        for candidate in re.split(r"[,;\s]+", raw):
            candidate = candidate.strip().lower().rstrip(".")
            if not candidate:
                continue
            if (len(candidate) > 253 or not re.fullmatch(
                r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+",
                candidate,
            ) or candidate.startswith("xn--")):
                _logger.warning(
                    "[MICROSOFT][THUMBNAIL] Host adicional inválido omitido: %s",
                    candidate[:120],
                )
                continue
            hosts.append(candidate)
        return tuple(hosts)


    # ------------------------------------------------------------

    # Navegación y búsquedas

    # ------------------------------------------------------------

    def list_root(self):

        self._check("list")

        return {"value": self.client.fetch_all(self._children(None), params={"$top": "200"})}



    def list_children(self, item_id):

        self._check("list")

        return {"value": self.client.fetch_all(self._children(item_id), params={"$top": "200"})}



    def get_item(self, item_id):

        self._check("properties")

        return self.client.request("GET", self._item(item_id))



    def get_root(self):

        self._check("properties")

        return self.client.request("GET", f"{self.drive_base}/root")



    def search(self, query):

        self._check("search")

        query = (query or "").strip()

        if not query:

            return {"value": []}

        if len(query) > 256:

            raise UserError("La búsqueda es demasiado extensa.")

        safe = quote(query.replace("'", "''"), safe="")

        endpoint = f"{self.drive_base}/root/search(q='{safe}')"

        return {"value": self.client.fetch_all(endpoint, params={"$top": "200"})}



    # ------------------------------------------------------------

    # Edición

    # ------------------------------------------------------------

    def ensure_folder(self, name, parent_id=None):
        """Reutiliza una carpeta existente; nunca duplica una por nombre."""
        self._check("create_folder")
        name = self._filename(name)
        children = self.client.fetch_all(self._children(parent_id), params={"$top": "200"})
        for child in children:
            if child.get("name", "").casefold() == name.casefold():
                if not child.get("folder"):
                    raise UserError("Existe un archivo con el nombre de la carpeta solicitada.")
                return child
        try:
            return self.client.request("POST", self._children(parent_id), json={
                "name": name, "folder": {}, "@microsoft.graph.conflictBehavior": "fail",
            })
        except UserError:
            # Puede haberla creado simultáneamente otra carga.
            children = self.client.fetch_all(self._children(parent_id), params={"$top": "200"})
            for child in children:
                if child.get("name", "").casefold() == name.casefold() and child.get("folder"):
                    return child
            raise

    def start_chunk_upload(self, name, size, parent_id=None):
        """Crear una sesión Graph sin recibir el contenido completo en Odoo."""
        self._check("upload")
        name = self._filename(name)
        try:
            size = int(size)
        except (TypeError, ValueError) as exc:
            raise UserError("Tamaño de archivo no válido.") from exc
        # Límite externo Graph: 250 GB por archivo; no imponer 100 MB.
        if size <= 0 or size > 250 * 1000**3:
            raise UserError("El tamaño debe ser positivo y no superar el máximo de Microsoft 365 (250 GB).")
        if parent_id:
            folder = self.client.request("GET", self._item(parent_id))
            if not folder.get("folder"):
                raise UserError("El destino debe ser una carpeta.")
        session = self.client.request(
            "POST", self._item_path(name, parent_id, "createUploadSession"),
            json={"item": {"@microsoft.graph.conflictBehavior": "rename", "name": name}},
        )
        url = self._validate_temporary_url(session.get("uploadUrl"))
        return {"upload_url": url, "size": size, "name": name, "parent_id": parent_id}

    # ------------------------------------------------------------
    # Creación documental: contenidos reales, nunca extensiones vacías
    # ------------------------------------------------------------

    DOCUMENT_TYPES = {
        "word": (".docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        "excel": (".xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        "powerpoint": (".pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
        "text": (".txt", "text/plain; charset=utf-8"),
        "markdown": (".md", "text/markdown; charset=utf-8"),
        "csv": (".csv", "text/csv; charset=utf-8"),
        "html": (".html", "text/html; charset=utf-8"),
        "json": (".json", "application/json; charset=utf-8"),
    }

    @staticmethod
    def _zip_parts(parts):
        """Crear un paquete Office Open XML en memoria, sin librerías externas."""
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as package:
            for path, content in parts.items():
                package.writestr(path, content.encode("utf-8"))
        return stream.getvalue()

    @staticmethod
    def _xml_header():
        return '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'

    @classmethod
    def _office_document(cls, doc_type):
        """Crear plantillas mínimas válidas para Office Online / Microsoft 365."""
        h = cls._xml_header()
        rels_ns = "http://schemas.openxmlformats.org/package/2006/relationships"
        ct_ns = "http://schemas.openxmlformats.org/package/2006/content-types"
        doc_rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
        root_rels = (h + f'<Relationships xmlns="{rels_ns}">'
                     f'<Relationship Id="rId1" Type="{doc_rel}/officeDocument" '
                     'Target="{target}"/></Relationships>')
        if doc_type == "word":
            parts = {
                "[Content_Types].xml": h + f'<Types xmlns="{ct_ns}"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>',
                "_rels/.rels": root_rels.format(target="word/document.xml"),
                "word/document.xml": h + '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p/><w:sectPr><w:pgSz w:w="12240" w:h="15840"/><w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/></w:sectPr></w:body></w:document>',
            }
        elif doc_type == "excel":
            parts = {
                "[Content_Types].xml": h + f'<Types xmlns="{ct_ns}"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>',
                "_rels/.rels": root_rels.format(target="xl/workbook.xml"),
                "xl/workbook.xml": h + f'<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="{doc_rel}"><sheets><sheet name="Hoja1" sheetId="1" r:id="rId1"/></sheets></workbook>',
                "xl/_rels/workbook.xml.rels": h + f'<Relationships xmlns="{rels_ns}"><Relationship Id="rId1" Type="{doc_rel}/worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
                "xl/worksheets/sheet1.xml": h + '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData/></worksheet>',
            }
        elif doc_type == "powerpoint":
            # Un archivo PPTX debe tener master/layout, tema y una diapositiva real.
            p = "http://schemas.openxmlformats.org/presentationml/2006/main"
            a = "http://schemas.openxmlformats.org/drawingml/2006/main"
            slide_content = f'<p:cSld name="Diapositiva 1"><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr></p:spTree></p:cSld>'
            parts = {
                "[Content_Types].xml": h + f'<Types xmlns="{ct_ns}"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/><Override PartName="/ppt/slides/slide1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/><Override PartName="/ppt/slideLayouts/slideLayout1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml"/><Override PartName="/ppt/slideMasters/slideMaster1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml"/><Override PartName="/ppt/theme/theme1.xml" ContentType="application/vnd.openxmlformats-officedocument.theme+xml"/></Types>',
                "_rels/.rels": root_rels.format(target="ppt/presentation.xml"),
                "ppt/presentation.xml": h + f'<p:presentation xmlns:p="{p}" xmlns:a="{a}" xmlns:r="{doc_rel}"><p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId1"/></p:sldMasterIdLst><p:sldIdLst><p:sldId id="256" r:id="rId2"/></p:sldIdLst><p:sldSz cx="12192000" cy="6858000" type="screen16x9"/><p:notesSz cx="6858000" cy="9144000"/></p:presentation>',
                "ppt/_rels/presentation.xml.rels": h + f'<Relationships xmlns="{rels_ns}"><Relationship Id="rId1" Type="{doc_rel}/slideMaster" Target="slideMasters/slideMaster1.xml"/><Relationship Id="rId2" Type="{doc_rel}/slide" Target="slides/slide1.xml"/></Relationships>',
                "ppt/slides/slide1.xml": h + f'<p:sld xmlns:p="{p}" xmlns:a="{a}" xmlns:r="{doc_rel}">{slide_content}<p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sld>',
                "ppt/slides/_rels/slide1.xml.rels": h + f'<Relationships xmlns="{rels_ns}"><Relationship Id="rId1" Type="{doc_rel}/slideLayout" Target="../slideLayouts/slideLayout1.xml"/></Relationships>',
                "ppt/slideLayouts/slideLayout1.xml": h + f'<p:sldLayout xmlns:p="{p}" xmlns:a="{a}" xmlns:r="{doc_rel}" type="blank" preserve="1">{slide_content}<p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sldLayout>',
                "ppt/slideLayouts/_rels/slideLayout1.xml.rels": h + f'<Relationships xmlns="{rels_ns}"><Relationship Id="rId1" Type="{doc_rel}/slideMaster" Target="../slideMasters/slideMaster1.xml"/></Relationships>',
                "ppt/slideMasters/slideMaster1.xml": h + f'<p:sldMaster xmlns:p="{p}" xmlns:a="{a}" xmlns:r="{doc_rel}">{slide_content}<p:sldLayoutIdLst><p:sldLayoutId id="2147483649" r:id="rId1"/></p:sldLayoutIdLst><p:txStyles><p:titleStyle/><p:bodyStyle/><p:otherStyle/></p:txStyles></p:sldMaster>',
                "ppt/slideMasters/_rels/slideMaster1.xml.rels": h + f'<Relationships xmlns="{rels_ns}"><Relationship Id="rId1" Type="{doc_rel}/slideLayout" Target="../slideLayouts/slideLayout1.xml"/><Relationship Id="rId2" Type="{doc_rel}/theme" Target="../theme/theme1.xml"/></Relationships>',
                "ppt/theme/theme1.xml": h + f'<a:theme xmlns:a="{a}" name="Tema básico"><a:themeElements><a:clrScheme name="Office"><a:dk1><a:sysClr val="windowText" lastClr="000000"/></a:dk1><a:lt1><a:sysClr val="window" lastClr="FFFFFF"/></a:lt1><a:dk2><a:srgbClr val="44546A"/></a:dk2><a:lt2><a:srgbClr val="E7E6E6"/></a:lt2><a:accent1><a:srgbClr val="4472C4"/></a:accent1><a:accent2><a:srgbClr val="ED7D31"/></a:accent2><a:accent3><a:srgbClr val="A5A5A5"/></a:accent3><a:accent4><a:srgbClr val="FFC000"/></a:accent4><a:accent5><a:srgbClr val="5B9BD5"/></a:accent5><a:accent6><a:srgbClr val="70AD47"/></a:accent6><a:hlink><a:srgbClr val="0563C1"/></a:hlink><a:folHlink><a:srgbClr val="954F72"/></a:folHlink></a:clrScheme><a:fontScheme name="Office"><a:majorFont><a:latin typeface="Aptos Display"/></a:majorFont><a:minorFont><a:latin typeface="Aptos"/></a:minorFont></a:fontScheme><a:fmtScheme name="Office"><a:fillStyleLst><a:solidFill><a:schemeClr val="accent1"/></a:solidFill><a:solidFill><a:schemeClr val="accent2"/></a:solidFill><a:solidFill><a:schemeClr val="accent3"/></a:solidFill></a:fillStyleLst><a:lnStyleLst><a:ln w="9525"><a:solidFill><a:schemeClr val="accent1"/></a:solidFill></a:ln><a:ln w="25400"><a:solidFill><a:schemeClr val="accent2"/></a:solidFill></a:ln><a:ln w="38100"><a:solidFill><a:schemeClr val="accent3"/></a:solidFill></a:ln></a:lnStyleLst><a:effectStyleLst><a:effectStyle><a:effectLst/></a:effectStyle><a:effectStyle><a:effectLst/></a:effectStyle><a:effectStyle><a:effectLst/></a:effectStyle></a:effectStyleLst><a:bgFillStyleLst><a:solidFill><a:schemeClr val="lt1"/></a:solidFill><a:solidFill><a:schemeClr val="lt2"/></a:solidFill><a:solidFill><a:schemeClr val="dk1"/></a:solidFill></a:bgFillStyleLst></a:fmtScheme></a:themeElements><a:objectDefaults/><a:extraClrSchemeLst/></a:theme>',
            }
        else:
            raise UserError("Tipo de documento Office no permitido.")
        return cls._zip_parts(parts)

    @classmethod
    def _new_content(cls, doc_type, text=""):
        """Crear contenido real y editable; se rechazan tamaños exagerados."""
        if doc_type in ("word", "excel", "powerpoint"):
            return cls._office_document(doc_type)
        if not isinstance(text, str) or len(text.encode("utf-8")) > 1024 * 1024:
            raise UserError("El contenido inicial debe ser texto de hasta 1 MB.")
        if doc_type == "json":
            import json
            try:
                json.loads(text or "{}")
            except ValueError as exc:
                raise UserError("El contenido JSON no es válido.") from exc
            text = text or "{}"
        elif doc_type == "html":
            text = text or '<!doctype html>\n<html lang="es"><head><meta charset="utf-8"><title>Documento</title></head><body></body></html>\n'
        elif doc_type == "csv":
            text = text or "Columna 1,Columna 2\r\n"
        return text.encode("utf-8")

    def create_document(self, name, doc_type, parent_id=None, content=""):
        """Crear Word/Excel/PowerPoint/TXT/MD/CSV/HTML/JSON en el drive actual.

        El controlador debe autorizar la operación upload (y el usuario debe
        tener acceso de escritura). La creación se registra por el controlador.
        """
        self._check("upload")
        if doc_type not in self.DOCUMENT_TYPES:
            raise UserError("El tipo de documento solicitado no está disponible.")
        suffix, mime = self.DOCUMENT_TYPES[doc_type]
        filename = self._filename(name)
        if not filename.lower().endswith(suffix):
            filename = self._filename(filename + suffix)
        if parent_id:
            folder = self.client.request("GET", self._item(parent_id))
            if folder.get("folder") is None:
                raise UserError("El destino debe ser una carpeta existente.")
        data = self._new_content(doc_type, content)
        _logger.info(
            "[MICROSOFT][CREATE_DOCUMENT][START] type=%s bytes=%d user=%s location=%s",
            doc_type, len(data), self.user.id, self.location.id,
        )
        # Usar conflictBehavior=rename para no sobrescribir un documento existente.
        path = self._item_path(filename, parent_id)
        joiner = "&" if "?" in path else "?"
        result = self.client.request(
            "PUT", path + joiner + "@microsoft.graph.conflictBehavior=rename",
            data=data,
            headers={"Content-Type": "application/octet-stream"},
            timeout=120,
        )
        if not isinstance(result, dict) or not result.get("id"):
            raise UserError("Microsoft no confirmó la creación del documento.")
        _logger.info(
            "[MICROSOFT][CREATE_DOCUMENT][SUCCESS] type=%s item=%s location=%s",
            doc_type, result["id"], self.location.id,
        )
        return result

    def create_link_file(self, name, target_url, parent_id=None):
        """Crear un archivo .url, no un vínculo especial de SharePoint."""
        self._check("upload")
        if not isinstance(target_url, str) or len(target_url) > 2048:
            raise UserError("Indique una URL válida.")
        try:
            parsed = urlsplit(target_url)
        except ValueError as exc:
            raise UserError("La URL indicada no es válida.") from exc
        if parsed.scheme.lower() != "https" or not parsed.hostname or parsed.username or parsed.password or any(ord(c) < 32 for c in target_url):
            raise UserError("Los vínculos deben usar HTTPS y no contener credenciales.")
        filename = self._filename(name)
        if not filename.lower().endswith(".url"):
            filename = self._filename(filename + ".url")
        payload = ("[InternetShortcut]\r\nURL=" + target_url + "\r\n").encode("utf-8")
        result = self.client.request(
            "PUT", self._item_path(filename, parent_id) + "?@microsoft.graph.conflictBehavior=rename",
            data=payload, headers={"Content-Type": "application/octet-stream"}, timeout=120,
        )
        if not isinstance(result, dict) or not result.get("id"):
            raise UserError("Microsoft no confirmó la creación del vínculo.")
        _logger.info("[MICROSOFT][CREATE_LINK] item=%s location=%s", result["id"], self.location.id)
        return result

    def create_folder(self, name, parent_id=None):

        self._check("create_folder")

        name = self._filename(name)

        return self.client.request("POST", self._children(parent_id), json={

            "name": name, "folder": {}, "@microsoft.graph.conflictBehavior": "rename",

        })



    def rename_item(self, item_id, new_name):

        self._check("rename")

        return self.client.request("PATCH", self._item(item_id), json={

            "name": self._filename(new_name),

        })



    def move_item(self, item_id, target_parent_id):

        self._check("move")

        if str(item_id) == str(target_parent_id):

            raise UserError("No puede mover una carpeta dentro de sí misma.")

        return self.client.request("PATCH", self._item(item_id), json={

            "parentReference": {"id": str(target_parent_id)},

        })



    def copy_item(self, item_id, target_parent_id, new_name=None):

        self._check("copy")

        payload = {"parentReference": {"id": str(target_parent_id)}}

        self._segment(target_parent_id)

        if new_name:

            payload["name"] = self._filename(new_name)

        # Graph devuelve habitualmente 202 (operación asíncrona).

        return self.client.request("POST", f"{self._item(item_id)}/copy", json=payload)



    def delete_item(self, item_id):

        self._check("delete")

        return self.client.request("DELETE", self._item(item_id))



    # ------------------------------------------------------------

    # Lectura, previsualización y descargas

    # ------------------------------------------------------------

    def get_download_url(self, item_id):

        """Retorna (url, item), como exige controllers/main.py."""

        self._check("download")

        item = self.client.request("GET", self._item(item_id))

        if item.get("folder") is not None:

            raise UserError("La descarga directa de carpetas no está disponible.")

        url = item.get("@microsoft.graph.downloadUrl")

        if not url:

            raise UserError("Microsoft no devolvió un enlace temporal de descarga.")

        return self._validate_temporary_url(url), item



    def get_thumbnail_url(self, item_id, size="medium"):

        self._check("preview")

        if size not in ("small", "medium", "large"):

            raise UserError("Tamaño de miniatura no permitido.")

        data = self.client.request("GET", f"{self._item(item_id)}/thumbnails")

        for entry in data.get("value") or []:

            thumbnail = (entry.get(size) or {}) if isinstance(entry, dict) else {}

            if thumbnail.get("url"):

                return self._validate_temporary_url(
                    thumbnail["url"], extra_hosts=self._thumbnail_extra_hosts()
                )

        return None



    def get_preview_url(self, item_id):

        self._check("preview")

        return self.client.request("POST", f"{self._item(item_id)}/preview", json={})



    # ------------------------------------------------------------

    # Compartición y permisos Graph

    # ------------------------------------------------------------

    def create_share_link(self, item_id, share_type="view", scope="organization"):

        self._check("share")

        if share_type not in ("view", "edit"):

            raise UserError("Tipo de enlace no admitido.")

        if scope not in ("organization", "anonymous"):

            raise UserError("Ámbito de compartición no admitido.")

        # La compartición anónima requiere activación administrativa expresa.

        if scope == "anonymous":

            enabled = self.env["ir.config_parameter"].sudo().get_param(

                "microsoft365.allow_anonymous_links", "False"

            )

            if str(enabled).lower() not in ("true", "1", "yes"):

                raise AccessError("Los enlaces anónimos están deshabilitados por política.")

        return self.client.request("POST", f"{self._item(item_id)}/createLink", json={

            "type": share_type, "scope": scope,

        })



    def invite_user(self, email, role="read", item_id=None, send_invitation=False):

        return self.invite_recipient({"email": email}, role, item_id, send_invitation)



    def invite_recipient(self, recipient, role="read", item_id=None, send_invitation=False):

        self._check("share")

        if not isinstance(recipient, dict) or len(recipient) != 1 or not set(recipient) <= {

            "email", "alias", "objectId"

        }:

            raise UserError("Debe indicar un único destinatario Microsoft válido.")

        key, value = next(iter(recipient.items()))

        if not isinstance(value, str) or not value.strip() or len(value) > 320:

            raise UserError("El destinatario no es válido.")

        if role not in ("read", "write"):

            raise UserError("Rol de invitación no válido.")

        if not item_id:

            item_id = self.get_root().get("id")

        return self.client.request("POST", f"{self._item(item_id)}/invite", json={

            "recipients": [{key: value.strip()}],

            "message": "Acceso documental asignado desde Odoo",

            "requireSignIn": True,

            "sendInvitation": bool(send_invitation),

            "roles": [role],

        })



    def remove_permission(self, item_id, permission_id):

        self._check("share")

        return self.client.request(

            "DELETE", f"{self._item(item_id)}/permissions/{self._segment(permission_id)}",

            expected=(404,),

        )



    def list_permissions(self, item_id=None):

        self._check("share")

        item_id = item_id or self.get_root().get("id")

        return self.client.fetch_all(

            f"{self._item(item_id)}/permissions", params={"$top": "200"}

        )



    @staticmethod

    def permission_group_ids(permission):

        if not isinstance(permission, dict):

            return set()

        identities = []

        if permission.get("grantedToV2"):

            identities.append(permission["grantedToV2"])

        identities.extend(permission.get("grantedToIdentitiesV2") or [])

        return {

            identity["group"]["id"]

            for identity in identities

            if isinstance(identity, dict)

            and isinstance(identity.get("group"), dict)

            and identity["group"].get("id")

        }



    # ------------------------------------------------------------

    # Versiones de archivos (Graph driveItem versions)

    # ------------------------------------------------------------

    def list_versions(self, item_id):

        self._check("properties")

        return {"value": self.client.fetch_all(

            f"{self._item(item_id)}/versions", params={"$top": "200"}

        )}



    def get_version(self, item_id, version_id):

        self._check("properties")

        return self.client.request(

            "GET", f"{self._item(item_id)}/versions/{self._segment(version_id)}"

        )



    def restore_version(self, item_id, version_id):

        self._check("rename")  # Derecho de modificación, no solo lectura.

        return self.client.request(

            "POST", f"{self._item(item_id)}/versions/{self._segment(version_id)}/restoreVersion",

            json={},

        )



    # ------------------------------------------------------------

    # Subidas pequeñas y grandes, sin guardar originales en Odoo

    # ------------------------------------------------------------

    def upload_file(self, filename, file_content, parent_id=None):

        self._check("upload")

        if not isinstance(file_content, (bytes, bytearray)):

            raise UserError("El contenido del documento debe ser binario.")

        return self.upload_stream(filename, io.BytesIO(file_content), len(file_content), parent_id)



    def upload_stream(self, filename, stream, size, parent_id=None):

        self._check("upload")

        filename = self._filename(filename)

        try:

            size = int(size)

        except (TypeError, ValueError) as error:

            raise UserError("Tamaño de archivo inválido.") from error

        if size < 0:

            raise UserError("El tamaño de archivo es inválido.")

        if not hasattr(stream, "read"):

            raise UserError("No se recibió un flujo de archivo válido.")

        if size < self.SMALL_UPLOAD_LIMIT:

            content = stream.read(size)

            if len(content) != size:

                raise UserError("No se recibió el archivo completo.")

            return self._upload_small(filename, content, parent_id)

        return self._upload_large(filename, stream, size, parent_id)



    def _item_path(self, filename, parent_id=None, suffix="content"):

        filename = quote(self._filename(filename), safe="")

        if suffix not in ("content", "createUploadSession"):

            raise UserError("Operación de subida inválida.")

        base = self._item(parent_id) if parent_id else f"{self.drive_base}/root"

        return f"{base}:/{filename}:/{suffix}"



    def _upload_small(self, filename, content, parent_id=None):

        self._check("upload")

        return self.client.request(

            "PUT", self._item_path(filename, parent_id),

            data=content,

            headers={"Content-Type": "application/octet-stream"},

            timeout=120,

        )



    @staticmethod

    def _json_response(response):

        try:

            payload = response.json()

        except ValueError:

            return {}

        return payload if isinstance(payload, dict) else {}



    def _upload_large(self, filename, stream, total_size, parent_id=None):

        self._check("upload")

        session = self.client.request(

            "POST", self._item_path(filename, parent_id, "createUploadSession"),

            json={"item": {

                "@microsoft.graph.conflictBehavior": "rename",

                "name": filename,

            }},

        )

        upload_url = self._validate_temporary_url(session.get("uploadUrl"))

        offset = 0

        last_payload = {}

        try:

            while offset < total_size:

                chunk = stream.read(min(self.CHUNK_SIZE, total_size - offset))

                if not chunk:

                    raise UserError("El archivo terminó antes del tamaño declarado.")

                end = offset + len(chunk) - 1

                headers = {

                    "Content-Length": str(len(chunk)),

                    "Content-Range": f"bytes {offset}-{end}/{total_size}",

                }

                response = None

                for attempt in range(self.MAX_CHUNK_RETRIES):

                    try:

                        response = requests.put(

                            upload_url, headers=headers, data=chunk,

                            timeout=self.UPLOAD_TIMEOUT, allow_redirects=False,

                        )

                    except (requests.Timeout, requests.ConnectionError) as error:

                        if attempt + 1 == self.MAX_CHUNK_RETRIES:

                            raise UserError("Se interrumpió la subida a Microsoft 365.") from error

                        # No repetir ciegamente un fragmento: consultar primero el estado.

                        next_offset = self._session_offset(upload_url, offset)

                        if next_offset != offset:

                            if next_offset == end + 1:

                                offset = next_offset

                                break

                            raise UserError("Microsoft cambió el desplazamiento de la subida.")

                        time.sleep(min(2 ** attempt, 8))

                        continue

                    try:

                        if response.status_code in (200, 201):

                            last_payload = self._json_response(response)

                            if not last_payload.get("id"):

                                raise UserError("Microsoft no confirmó el archivo finalizado.")

                            return last_payload

                        if response.status_code == 202:

                            payload = self._json_response(response)

                            ranges = payload.get("nextExpectedRanges") or []

                            if ranges:

                                try:

                                    next_offset = int(str(ranges[0]).split("-")[0])

                                except (ValueError, IndexError):

                                    next_offset = end + 1

                            else:

                                next_offset = end + 1

                            if next_offset != end + 1:

                                raise UserError("Microsoft devolvió una posición de subida inesperada.")

                            offset = next_offset

                            break

                        if response.status_code in (429, 500, 502, 503, 504):

                            if attempt + 1 == self.MAX_CHUNK_RETRIES:

                                raise UserError(f"Microsoft rechazó la subida ({response.status_code}).")

                            next_offset = self._session_offset(upload_url, offset)

                            if next_offset == end + 1:

                                offset = next_offset

                                break

                            if next_offset != offset:

                                raise UserError("Microsoft devolvió un desplazamiento incompatible.")

                            time.sleep(min(2 ** attempt, 8))

                            continue

                        raise UserError(f"Microsoft rechazó un fragmento ({response.status_code}).")

                    finally:

                        response.close()

                else:

                    raise UserError("No se pudo completar la subida del fragmento.")

            # La confirmación final debe incluir driveItem.id; no simular éxito.

            raise UserError("Microsoft no confirmó que se haya completado la subida.")

        except Exception:

            # El servidor debe intentar cerrar una sesión abandonada.

            try:

                requests.delete(upload_url, timeout=10, allow_redirects=False)

            except requests.RequestException:

                _logger.debug("No se pudo cancelar una sesión de subida Microsoft.")

            raise



    def _session_offset(self, upload_url, default):

        self._validate_temporary_url(upload_url)

        try:

            response = requests.get(upload_url, timeout=30, allow_redirects=False)

        except requests.RequestException:

            return default

        try:

            if response.status_code != 200:

                return default

            ranges = self._json_response(response).get("nextExpectedRanges") or []

            if not ranges:

                return default

            return int(str(ranges[0]).split("-")[0])

        except (ValueError, IndexError):

            return default

        finally:

            response.close()
