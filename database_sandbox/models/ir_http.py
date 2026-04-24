# -*- coding: utf-8 -*-
import threading
from odoo import models, api
from odoo.http import request

class IrHttp(models.AbstractModel):
    _inherit = 'ir.http'

    def session_info(self):
        result = super(IrHttp, self).session_info()
        sandbox_schema = request.session.get('sandbox_schema')
        
        # MASK THE VIRTUAL NAME:
        # Even if we are in a sandbox, tell the UI we are on the production DB.
        # This fixes the name shown in debug mode and the top bar.
        if sandbox_schema:
            result['is_sandbox'] = True
            result['sandbox_db_name'] = sandbox_schema
            # If the current DB name is the virtual one, strip the suffix for the UI
            if '_virtual_' in result['db']:
                result['db'] = result['db'].split('_virtual_')[0]
        else:
            result['is_sandbox'] = False
            
        return result
