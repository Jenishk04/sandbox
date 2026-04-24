# -*- coding: utf-8 -*-
from odoo import models, fields, api

class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    max_sandboxes = fields.Integer(string='Max Sandboxes', config_parameter='database_sandbox.max_sandboxes', default=5)
    expiry_hours = fields.Integer(string='Sandbox Expiration (Hours)', config_parameter='database_sandbox.expiry_hours', default=4)
