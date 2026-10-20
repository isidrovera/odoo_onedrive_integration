# -*- coding: utf-8 -*-
"""Compatibilidad del explorador OneDrive delegado anterior (Odoo 19).

Las nuevas bibliotecas y permisos deben operar mediante DriveService.
Esta clase conserva los nombres y respuestas utilizados por integraciones antiguas.
"""
import logging
import re
import time
from urllib.parse import quote

import requests
from odoo.exceptions import UserError

from .graph_client import GraphClient

_logger = logging.getLogger(__name__)


class GraphService:
    DEFAULT_PAGE_SIZE = 200
    DEFAULT_TIMEOUT = 60
    MAX_PAGES = 50
    UPLOAD_THRESHOLD = 4 * 1024 * 1024
    UPLOAD_CHUNK_SIZE = 5 * 1024 * 1024  # 320 KiB * 16
    MAX_UPLOAD_RETRIES = 3
    _INVALID_NAME = re.compile(r'["*:<>?/\\|\x00-\x1f]')

    def __init__(self, account):
        account.ensure_one()
        self.account = account
        self.base_url = account.env['ir.config_parameter'].sudo().get_param(
            'onedrive.graph.base_url'
        ) or account.env['ir.config_parameter'].sudo().get_param(
            'microsoft365.graph.base_url', 'https://graph.microsoft.com/v1.0'
        )
        # Conservamos el atributo token para compatibilidad con código heredado.
        self.token = account.get_valid_token()
        self.client = GraphClient(token_provider=self._get_token, env=account.env,
                                  base_url=self.base_url)
        self.base_url = self.client.base_url

    def _get_token(self):
        # Renovar token al pedirlo evita que expire a mitad de una operación larga.
        token = self.account.get_valid_token()
        if token:
            self.token = token
        return self.token

    @staticmethod
    def _segment(value):
        value = str(value or '').strip()
        if not value or len(value) > 512 or any(ord(ch) < 32 for ch in value):
            raise UserError('Identificador de archivo inválido.')
        return quote(value, safe='')

    @classmethod
    def _name(cls, name):
        if not isinstance(name, str):
            raise UserError('Debe indicar un nombre de archivo válido.')
        name = name.strip()
        if (not name or len(name) > 255 or name in ('.', '..')
                or name.endswith('.') or cls._INVALID_NAME.search(name)):
            raise UserError('El nombre contiene caracteres no permitidos por Microsoft 365.')
        return name

    def _headers(self, content_type='application/json'):
        return {'Authorization': f'Bearer {self._get_token()}',
                'Content-Type': content_type}

    def _request(self, method, endpoint, timeout=None, **kwargs):
        return self.client.request(method, endpoint,
                                   timeout=timeout or self.DEFAULT_TIMEOUT, **kwargs)

    def _request_absolute_url(self, url):
        return self.client.request('GET', url, absolute=True)

    def _fetch_all_pages(self, endpoint):
        values = self.client.fetch_all(endpoint, max_pages=self.MAX_PAGES)
        return {'value': values,
                'page_count': None}  # El cliente común devuelve todos los registros.

    def list_root(self):
        return self._fetch_all_pages(f'/me/drive/root/children?$top={self.DEFAULT_PAGE_SIZE}')

    def list_children(self, item_id):
        return self._fetch_all_pages(
            f'/me/drive/items/{self._segment(item_id)}/children?$top={self.DEFAULT_PAGE_SIZE}'
        )

    def get_item(self, item_id):
        return self._request('GET', f'/me/drive/items/{self._segment(item_id)}')

    def search(self, query):
        if not isinstance(query, str) or not query.strip():
            return {'value': [], 'page_count': 0}
        if len(query) > 200:
            raise UserError('La búsqueda excede el límite permitido.')
        escaped = query.strip().replace("'", "''")
        encoded = quote(escaped, safe="'")
        return self._fetch_all_pages(
            f"/me/drive/root/search(q='{encoded}')?$top={self.DEFAULT_PAGE_SIZE}"
        )

    def create_folder(self, name, parent_id=None):
        endpoint = ('/me/drive/root/children' if not parent_id else
                    f'/me/drive/items/{self._segment(parent_id)}/children')
        return self._request('POST', endpoint, json={
            'name': self._name(name), 'folder': {},
            '@microsoft.graph.conflictBehavior': 'rename',
        })

    def rename_item(self, item_id, new_name):
        return self._request('PATCH', f'/me/drive/items/{self._segment(item_id)}',
                             json={'name': self._name(new_name)})

    def move_item(self, item_id, target_parent_id):
        return self._request('PATCH', f'/me/drive/items/{self._segment(item_id)}',
                             json={'parentReference': {'id': str(target_parent_id)}})

    def copy_item(self, item_id, target_parent_id, new_name=None):
        payload = {'parentReference': {'id': str(target_parent_id)}}
        if new_name:
            payload['name'] = self._name(new_name)
        # Graph devuelve 202 para copias asíncronas; conservar comportamiento.
        return self._request('POST', f'/me/drive/items/{self._segment(item_id)}/copy',
                             json=payload)

    def delete_item(self, item_id):
        return self._request('DELETE', f'/me/drive/items/{self._segment(item_id)}')

    def get_download_url(self, item_id):
        data = self.get_item(item_id)
        if 'folder' in data:
            raise UserError('La descarga de carpetas requiere un proceso ZIP específico.')
        url = data.get('@microsoft.graph.downloadUrl')
        if not url:
            raise UserError('No se pudo obtener el enlace de descarga.')
        return url

    def get_folder_download_url(self, item_id):
        # Compatibilidad: se devuelve la ruta Graph; NO es un enlace ZIP público.
        return f'{self.base_url}/me/drive/items/{self._segment(item_id)}/content'

    def get_thumbnail_url(self, item_id, size='medium'):
        if size not in ('small', 'medium', 'large'):
            raise UserError('Tamaño de miniatura inválido.')
        try:
            data = self._request('GET',
                f'/me/drive/items/{self._segment(item_id)}/thumbnails')
            thumbnails = data.get('value', [])
            return thumbnails[0].get(size, {}).get('url') if thumbnails else None
        except UserError as exc:
            _logger.debug('Miniatura no disponible: %s', exc)
            return None

    def get_preview_url(self, item_id):
        return self._request('POST',
                             f'/me/drive/items/{self._segment(item_id)}/preview', json={})

    def create_share_link(self, item_id, share_type='view', scope='organization'):
        if share_type not in ('view', 'edit'):
            raise UserError('Tipo de enlace no permitido.')
        if scope != 'organization':
            raise UserError('Los enlaces anónimos están deshabilitados por seguridad.')
        return self._request('POST',
                             f'/me/drive/items/{self._segment(item_id)}/createLink',
                             json={'type': share_type, 'scope': scope})

    def upload_file(self, filename, file_content, parent_id=None):
        filename = self._name(filename)
        if not isinstance(file_content, (bytes, bytearray)):
            raise UserError('El archivo debe contener datos binarios.')
        if len(file_content) < self.UPLOAD_THRESHOLD:
            return self._upload_small(filename, file_content, parent_id)
        return self._upload_large(filename, file_content, parent_id)

    def _item_path(self, filename, parent_id=None):
        base = (f'/me/drive/items/{self._segment(parent_id)}' if parent_id
                else '/me/drive/root')
        return f'{base}:/{quote(self._name(filename), safe="")}:'

    def _upload_small(self, filename, file_content, parent_id=None):
        return self._request('PUT', self._item_path(filename, parent_id) + '/content',
                             headers={'Content-Type': 'application/octet-stream'},
                             data=file_content, timeout=self.DEFAULT_TIMEOUT)

    def _upload_large(self, filename, file_content, parent_id=None):
        session = self._request('POST',
            self._item_path(filename, parent_id) + '/createUploadSession',
            json={'item': {'@microsoft.graph.conflictBehavior': 'rename',
                           'name': self._name(filename)}})
        upload_url = session.get('uploadUrl')
        if not upload_url:
            raise UserError('Microsoft no devolvió una sesión de subida.')
        # URL prefirmada por Microsoft: nunca enviar el token OAuth ni seguir
        # redirecciones. El endpoint debe pertenecer a servicios Microsoft.
        from urllib.parse import urlsplit
        parsed = urlsplit(upload_url)
        hostname = (parsed.hostname or '').lower()
        if (parsed.scheme != 'https' or parsed.port not in (None, 443)
                or not (hostname.endswith('.sharepoint.com')
                        or hostname.endswith('.up.1drv.com')
                        or hostname.endswith('.onedrive.com')
                        or hostname == 'api.onedrive.com')):
            raise UserError('Microsoft devolvió una sesión de subida no autorizada.')
        total = len(file_content)
        offset = 0
        last = None
        while offset < total:
            end = min(offset + self.UPLOAD_CHUNK_SIZE, total)
            headers = {
                'Content-Length': str(end - offset),
                'Content-Range': f'bytes {offset}-{end - 1}/{total}',
            }
            for attempt in range(self.MAX_UPLOAD_RETRIES):
                try:
                    response = requests.put(
                        upload_url, headers=headers, data=file_content[offset:end],
                        timeout=self.DEFAULT_TIMEOUT * 2, allow_redirects=False,
                    )
                except (requests.Timeout, requests.ConnectionError) as exc:
                    if attempt + 1 == self.MAX_UPLOAD_RETRIES:
                        raise UserError('Se interrumpió la subida a Microsoft.') from exc
                    time.sleep(2 ** attempt)
                    continue
                if response.status_code in (429, 500, 502, 503, 504):
                    response.close()
                    if attempt + 1 == self.MAX_UPLOAD_RETRIES:
                        raise UserError('Microsoft no pudo completar la subida.')
                    time.sleep(2 ** attempt)
                    continue
                if response.status_code not in (200, 201, 202):
                    response.close()
                    raise UserError(f'Microsoft rechazó un fragmento ({response.status_code}).')
                try:
                    last = response.json() if response.content else {}
                except ValueError:
                    last = {}
                finally:
                    response.close()
                break
            offset = end
        if not isinstance(last, dict) or not last.get('id'):
            # Algunos servidores devuelven 202 durante sesiones pendientes.
            raise UserError('No se recibió confirmación final de la subida.')
        return last
