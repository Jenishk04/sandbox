{
    'name': 'Full Database Sandbox Environment',
    'version': '1.0',
    'summary': 'Create temporary sandbox copies of the entire Odoo database.',
    'description': """
Full Database Sandbox Environment
=================================
Allows users to create a temporary clone of the production database for testing, 
module installation, or configuration changes without affecting the live system.
    """,
    'category': 'Technical',
    'author': 'Odoo Architect',
    'depends': ['base', 'web', 'mail'],
    'data': [
        'security/ir.model.access.csv',
        'views/sandbox_environment_views.xml',
        'views/res_config_settings_views.xml',
        'data/ir_cron_data.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'database_sandbox/static/src/css/sandbox.css',
            'database_sandbox/static/src/js/sandbox_systray.js',
            'database_sandbox/static/src/xml/sandbox_systray.xml',
        ],
    },
    'license': 'LGPL-3',
}
