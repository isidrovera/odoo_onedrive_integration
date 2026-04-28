{
    "name": "OneDrive Integration",
    "version": "1.0.0",
    "summary": "Integración con OneDrive Empresarial usando Microsoft Graph",
    "author": "Tu Empresa",
    "category": "Tools",
    "license": "LGPL-3",
    "depends": ["base", "web"],
    "data": [
        "security/ir.model.access.csv",
        "views/onedrive_views.xml",
        "data/ir_config_parameter.xml",
    ],
    "assets": {
        "web.assets_backend": [
            # Bootstrap Icons (CDN)
            ("include", "odoo_onedrive_integration/static/src/scss/bootstrap_icons.scss"),
            # Estilos propios
            "odoo_onedrive_integration/static/src/scss/onedrive_app.scss",
            # Utils y diálogos PRIMERO
            "odoo_onedrive_integration/static/src/js/utils/file_utils.js",
            "odoo_onedrive_integration/static/src/js/dialogs/confirm_dialog.js",
            "odoo_onedrive_integration/static/src/js/dialogs/prompt_dialog.js",
            "odoo_onedrive_integration/static/src/js/dialogs/file_preview_dialog.js",
            "odoo_onedrive_integration/static/src/js/dialogs/share_dialog.js",
            "odoo_onedrive_integration/static/src/js/dialogs/properties_dialog.js",
            # App principal AL FINAL
            "odoo_onedrive_integration/static/src/js/onedrive_app.js",
            # Templates
            "odoo_onedrive_integration/static/src/xml/onedrive_templates.xml",
        ]
    },
    "installable": True,
    "application": True,
    "auto_install": False,
}