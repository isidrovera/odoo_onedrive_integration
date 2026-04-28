{
    "name": "OneDrive Integration",
    "version": "1.0.0",
    "summary": "Integración con OneDrive Empresarial usando Microsoft Graph",
    "description": """
Módulo de integración entre Odoo y OneDrive Empresarial.

Funcionalidades:
- Autenticación OAuth2 con Microsoft
- Explorador de archivos tipo nube
- Subida, descarga y eliminación de archivos
- Navegación de carpetas
    """,
    "author": "Tu Empresa",
    "website": "https://tuempresa.com",
    "category": "Tools",
    "license": "LGPL-3",

    # Dependencias base
    "depends": [
        "base",
        "web",
    ],

    # Archivos que carga Odoo
    "data": [
        "security/ir.model.access.csv",
        "views/onedrive_views.xml",
        "data/ir_config_parameter.xml",
    ],

    # Assets (frontend tipo nube)
    "assets": {
        "web.assets_backend": [
            "odoo_onedrive_integration/static/src/scss/onedrive_app.scss",
            "odoo_onedrive_integration/static/src/js/onedrive_app.js",
            "odoo_onedrive_integration/static/src/js/utils/file_utils.js",
            "odoo_onedrive_integration/static/src/js/dialogs/confirm_dialog.js",
            "odoo_onedrive_integration/static/src/js/dialogs/prompt_dialog.js",
            "odoo_onedrive_integration/static/src/js/dialogs/file_preview_dialog.js",
            "odoo_onedrive_integration/static/src/js/dialogs/share_dialog.js",
            "odoo_onedrive_integration/static/src/js/dialogs/properties_dialog.js",
            "odoo_onedrive_integration/static/src/xml/onedrive_templates.xml",
        ]
    },

    # Flags
    "installable": True,
    "application": True,
    "auto_install": False,
}