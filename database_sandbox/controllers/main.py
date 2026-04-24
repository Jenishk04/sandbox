# -*- coding: utf-8 -*-
import logging
from odoo import http, _
from odoo.http import request
from odoo.exceptions import AccessDenied
import threading

_logger = logging.getLogger(__name__)

class SandboxController(http.Controller):

    @http.route('/sandbox/start', type='json', auth='user', website=False)
    def start_sandbox(self, **kw):
        """Create a sandbox and switch the current user to it."""
        try:
            sandbox = request.env['sandbox.environment'].sudo().create_sandbox()
            return {
                'success': True,
                'sandbox_id': sandbox.id,
                'db_name': sandbox.db_name,
                'state': 'cloning',
            }
        except Exception as e:
            _logger.exception("Sandbox creation failed")
            return {
                'success': False,
                'error': str(e),
            }

    @http.route('/sandbox/status', type='json', auth='user', website=False)
    def poll_sandbox_status(self, sandbox_id, **kw):
        """Poll the status of a sandbox being created."""
        sandbox = request.env['sandbox.environment'].sudo().browse(sandbox_id)
        if not sandbox.exists():
            return {'success': False, 'error': 'Sandbox not found.'}
        
        # If active, set the session flag so the monkeypatch kicks in
        if sandbox.state == 'active':
            request.session['sandbox_schema'] = sandbox.db_name
            # Force a reload of the registry in this thread
            threading.current_thread().sandbox_schema = sandbox.db_name

        return {
            'success': True,
            'state': sandbox.state,
            'error_message': sandbox.error_message,
            'db_name': request.db, # Stay on same DB name
            'redirect_url': '/web',
        }

    @http.route('/sandbox/stop', type='json', auth='user', website=False)
    def stop_sandbox(self, **kw):
        """Terminate the current sandbox and return to production."""
        schema = request.session.get('sandbox_schema')
        if not schema:
            return {'success': False, 'error': "Not in a sandbox."}

        # Clear session flag
        request.session.pop('sandbox_schema', None)
        if hasattr(threading.current_thread(), 'sandbox_schema'):
            del threading.current_thread().sandbox_schema
            
        # Update the state in the production database
        production_db = request.db.split('_virtual_')[0] if '_virtual_' in request.db else request.db
        try:
            import odoo
            with odoo.sql_db.db_connect(production_db).cursor() as cr:
                env = odoo.api.Environment(cr, odoo.SUPERUSER_ID, {})
                sandbox = env['sandbox.environment'].search([('db_name', '=', schema)], limit=1)
                if sandbox and sandbox.state == 'active':
                    sandbox.state = 'paused'
        except Exception as e:
            _logger.exception("Failed to pause sandbox record")

        return {
            'success': True,
            'redirect_url': '/web',
        }

    @http.route('/sandbox/resume/<int:sandbox_id>', type='http', auth='user', website=False)
    def resume_sandbox(self, sandbox_id, **kw):
        """Resume an existing paused sandbox."""
        sandbox = request.env['sandbox.environment'].sudo().browse(sandbox_id)
        if not sandbox.exists():
            return request.redirect('/web')
            
        if sandbox.remaining_entries <= 0:
            sandbox.state = 'expired'
            return request.redirect('/web')
            
        if sandbox.state in ['paused', 'active', 'cloning']:
            sandbox.remaining_entries -= 1
            sandbox.state = 'active'
                
            request.session['sandbox_schema'] = sandbox.db_name
            threading.current_thread().sandbox_schema = sandbox.db_name
            
        return request.redirect('/web')
