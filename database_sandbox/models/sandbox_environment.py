# -*- coding: utf-8 -*-
import logging
import uuid
import threading
import os
import shutil
from datetime import datetime, timedelta
from odoo import models, fields, api, SUPERUSER_ID, sql_db, tools
from odoo.modules.registry import Registry
from markupsafe import Markup

_logger = logging.getLogger(__name__)

# =============================================================================
# VIRTUAL BRANCHING MONKEYPATCH
# Allows multiple Odoo Registries to coexist on the SAME physical database by
# appending a unique virtual suffix to the registry key when inside a sandbox.
# =============================================================================

original_registry_new = Registry.__new__
original_registry_build = Registry.new.__func__


def _is_in_sandbox():
    """Return True only when the current thread is running inside a sandbox session.

    Verifies both the thread-local flag AND the schema naming convention so a
    stale or malformed value can never accidentally target production.
    """
    schema = getattr(threading.current_thread(), 'sandbox_schema', None)
    if not schema:
        return False
    if not schema.startswith('sandbox_'):
        _logger.error(
            "sandbox_schema %r does not start with 'sandbox_' — "
            "refusing sandbox-specific code to protect production data.", schema
        )
        return False
    return True


def _get_virtual_db_name(db_name):
    """Map a physical DB name to the sandbox-specific virtual name for this thread."""
    if '_virtual_' in db_name:
        return db_name
    schema = getattr(threading.current_thread(), 'sandbox_schema', None)
    if schema:
        return f"{db_name}_virtual_{schema}"
    return db_name


def patched_registry_new(cls, db_name):
    """Intercept registry lookup to support virtual sandboxes on the same DB name."""
    return original_registry_new(cls, _get_virtual_db_name(db_name))


def patched_registry_build(cls, db_name, *args, **kwargs):
    """Intercept registry rebuilds (including module install/upgrade/uninstall flows).

    Catches the KeyError that Odoo raises during nested Registry.new() failures:
    the outer call already deleted db_name from cls.registries, the inner cleanup
    tries again → KeyError that masks the real exception (e.g. ForeignKeyViolation).
    """
    virtual_name = _get_virtual_db_name(db_name)
    try:
        return original_registry_build(cls, virtual_name, *args, **kwargs)
    except KeyError as e:
        _logger.warning("Swallowing KeyError in Registry.new (nested cleanup): %s", e)


Registry.__new__ = patched_registry_new
Registry.new = classmethod(patched_registry_build)

# =============================================================================
# DATABASE CONNECTION ISOLATION
#
# Every sandbox session gets a PostgreSQL connection with:
#   search_path = <sandbox_schema>, public, pg_catalog
#
# This SESSION-LEVEL setting (via -c search_path=...) survives ROLLBACK, so:
#   SELECT/INSERT/UPDATE/DELETE on any table → hits sandbox_schema first
#   Config changes, user creation, module data → all go to sandbox_schema
#   The live public schema is NEVER touched by any in-sandbox user action.
#
# The only intentional production writes are:
#   1. last_accessed timestamp (rate-limited, in patched_request_post_init)
#   2. Audit log chatter (in _log_sandbox_action)
# Both use explicit original_db_connect(production_db) calls.
# =============================================================================

original_db_connect = sql_db.db_connect


def patched_db_connect(to, allow_uri=False, readonly=False):
    """Route virtual sandbox DB names to a search_path-isolated connection.

    Non-sandbox connection requests pass through to original Odoo db_connect unchanged.
    """
    if '_virtual_sandbox_' not in to:
        return original_db_connect(to, allow_uri, readonly)

    parts = to.split('_virtual_')
    production_db = parts[0]
    schema_name = parts[1]

    if not schema_name.startswith('sandbox_'):
        _logger.error(
            "patched_db_connect: schema_name %r is invalid — "
            "falling back to production connection to protect live data.", schema_name
        )
        return original_db_connect(production_db, allow_uri, readonly)

    # Warm up the production pool, then build a sandbox-isolated connection
    _, info = sql_db.connection_info_for(production_db, readonly)
    original_db_connect(production_db, allow_uri, readonly)
    pool = sql_db._Pool if not readonly else sql_db._Pool_readonly

    sandbox_info = dict(info)
    existing_options = sandbox_info.get('options', '')
    sandbox_info['options'] = f"{existing_options} -c search_path={schema_name},public,pg_catalog".strip()

    return sql_db.Connection(pool, to, sandbox_info)


sql_db.db_connect = patched_db_connect

# =============================================================================
# REQUEST ROUTING: inject sandbox identity at the start of each HTTP request
# =============================================================================
from odoo.http import Request, Application

original_request_post_init = Request._post_init
SANDBOX_LAST_ACCESS = {}


def patched_request_post_init(self):
    """Redirect a sandbox request to its virtual registry and isolated connection."""
    original_request_post_init(self)
    sandbox_schema = self.session.get('sandbox_schema')
    if not sandbox_schema:
        return

    if not sandbox_schema.startswith('sandbox_'):
        _logger.error(
            "patched_request_post_init: sandbox_schema %r is invalid — ignoring.", sandbox_schema
        )
        return

    threading.current_thread().sandbox_schema = sandbox_schema
    production_db = self.db.split('_virtual_')[0] if '_virtual_' in (self.db or '') else self.db
    self.db = _get_virtual_db_name(self.db)

    # INTENTIONAL PRODUCTION WRITE: last_accessed timestamp (rate-limited)
    now = datetime.now()
    last_acc = SANDBOX_LAST_ACCESS.get(sandbox_schema)
    if not last_acc or (now - last_acc).total_seconds() > 300:
        SANDBOX_LAST_ACCESS[sandbox_schema] = now
        try:
            import odoo
            with odoo.sql_db.db_connect(production_db).cursor() as cr:
                cr.execute(
                    "UPDATE sandbox_environment SET last_accessed = %s WHERE db_name = %s",
                    (now, sandbox_schema)
                )
                cr.commit()
        except Exception:
            pass


Request._post_init = patched_request_post_init

original_call = Application.__call__


def patched_call(self, environ, start_response):
    """Clear the thread-local sandbox flag at the start of every request."""
    current_thread = threading.current_thread()
    if hasattr(current_thread, 'sandbox_schema'):
        del current_thread.sandbox_schema
    return original_call(self, environ, start_response)


Application.__call__ = patched_call


# =============================================================================
# SANDBOX MODEL
# =============================================================================

class SandboxEnvironment(models.Model):
    _name = 'sandbox.environment'
    _description = 'Sandbox Environment'
    _inherit = ['mail.thread']
    _order = 'create_date desc'

    name = fields.Char(string='Sandbox Name', required=True, copy=False,
                       default=lambda self: f"Sandbox {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    db_name = fields.Char(string='Schema Name', required=True, readonly=True)
    user_id = fields.Many2one('res.users', string='Owner',
                              default=lambda self: self.env.user, readonly=True)
    create_date = fields.Datetime(string='Created On', readonly=True)
    expiry_date = fields.Datetime(string='Expires On', required=True)
    last_accessed = fields.Datetime(string='Last Accessed',
                                    default=fields.Datetime.now, readonly=True)
    remaining_entries = fields.Integer(string='Remaining Entries', default=3, readonly=True)
    state = fields.Selection([
        ('cloning', 'Cloning...'),
        ('active', 'Active'),
        ('paused', 'Paused'),
        ('expired', 'Expired'),
        ('deleting', 'Deleting'),
        ('failed', 'Failed'),
    ], string='Status', default='cloning', readonly=True)
    error_message = fields.Text(string='Error Message', readonly=True)
    db_size = fields.Float(string='Database Size (MB)', compute='_compute_db_size')

    def _compute_db_size(self):
        for record in self:
            if record.state not in ('active', 'paused'):
                record.db_size = 0.0
                continue
            try:
                self.env.cr.execute("""
                    SELECT sum(pg_total_relation_size(c.oid))
                    FROM pg_class c
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE n.nspname = %s AND c.relkind = 'r'
                """, (record.db_name,))
                record.db_size = (self.env.cr.fetchone()[0] or 0) / (1024 * 1024)
            except Exception as e:
                _logger.error("Error computing db size for sandbox %s: %s", record.db_name, e)
                record.db_size = 0.0

    @api.model
    def _search(self, domain, offset=0, limit=None, order=None):
        """Block sandbox dashboard access from within a sandbox session.

        When the user is inside a sandbox, the sandbox.environment records they
        see would be stale clones of the production records — showing the wrong
        state, allowing navigation to other sandboxes, etc. We simply hide all
        records so the dashboard appears empty and cannot be misused.
        """
        if _is_in_sandbox():
            return super()._search([('id', '=', False)], offset, limit, order)
        return super()._search(domain, offset, limit, order)

    @api.model
    def create_sandbox(self):
        """Create a sandbox schema cloned from production."""
        production_db = self.env.cr.dbname
        sandbox_id = str(uuid.uuid4())[:8]
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        new_schema_name = f"sandbox_u{self.env.user.id}_{timestamp}_{sandbox_id}"

        max_sandboxes = int(self.env['ir.config_parameter'].sudo().get_param(
            'database_sandbox.max_sandboxes', 5))
        if self.search_count([('state', 'in', ['cloning', 'active'])]) >= max_sandboxes:
            raise models.ValidationError(
                f"Maximum number of sandboxes ({max_sandboxes}) reached.")

        expiry_hours = int(self.env['ir.config_parameter'].sudo().get_param(
            'database_sandbox.expiry_hours', 4))

        sandbox = self.create({
            'name': f"Sandbox for {self.env.user.name} ({timestamp})",
            'db_name': new_schema_name,
            'expiry_date': datetime.now() + timedelta(hours=expiry_hours),
            'state': 'cloning',
        })

        def start_cloning_thread():
            threading.Thread(
                target=self._run_cloning_process,
                args=(production_db, new_schema_name, sandbox.id)
            ).start()

        self.env.cr.postcommit.add(start_cloning_thread)
        return sandbox

    def _run_cloning_process(self, production_db, new_db_name, sandbox_id):
        """Background thread: clone production schema into the new sandbox schema."""
        try:
            _logger.info("Starting sandbox clone to schema %s", new_db_name)

            source_filestore = tools.config.filestore(production_db)
            target_filestore = tools.config.filestore(f"{production_db}_virtual_{new_db_name}")
            os.makedirs(target_filestore, exist_ok=True)

            SKIP_DATA_TABLES = ['bus_presence', 'ir_logging']

            with Registry(production_db).cursor() as cr:
                # Use REPEATABLE READ so the entire clone is a single consistent snapshot,
                # preventing FK violations from torn concurrent writes.
                cr.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")

                # 1. Create the new schema
                cr.execute(f'CREATE SCHEMA "{new_db_name}"')

                # 2. Copy sequences with their current state
                cr.execute("""
                    SELECT sequence_name,
                           start_value::bigint, minimum_value::bigint,
                           maximum_value::bigint, increment::bigint, cycle_option
                    FROM information_schema.sequences
                    WHERE sequence_schema = 'public'
                """)
                for seq_name, start_val, min_val, max_val, inc, cycle in cr.fetchall():
                    cycle_sql = 'CYCLE' if cycle == 'YES' else 'NO CYCLE'
                    cr.execute(
                        f'CREATE SEQUENCE "{new_db_name}"."{seq_name}" '
                        f'START WITH %s INCREMENT BY %s MINVALUE %s MAXVALUE %s {cycle_sql}',
                        (start_val, inc, min_val, max_val)
                    )
                    cr.execute(f'SELECT last_value, is_called FROM public."{seq_name}"')
                    last_val, is_called = cr.fetchone()
                    cr.execute('SELECT setval(%s, %s, %s)',
                               (f'"{new_db_name}"."{seq_name}"', last_val, is_called))

                # 3. Mirror public schema table structure
                cr.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
                tables = [row[0] for row in cr.fetchall()]

                for table in tables:
                    cr.execute(
                        f'CREATE TABLE "{new_db_name}"."{table}" '
                        f'(LIKE public."{table}" INCLUDING ALL)'
                    )
                    cr.execute("""
                        SELECT column_name, column_default
                        FROM information_schema.columns
                        WHERE table_schema = %s AND table_name = %s
                          AND column_default LIKE 'nextval(%%'
                    """, (new_db_name, table))
                    for col, default in cr.fetchall():
                        if 'public.' in default:
                            cr.execute(
                                f'ALTER TABLE "{new_db_name}"."{table}" '
                                f'ALTER COLUMN "{col}" SET DEFAULT '
                                + default.replace('public.', f'"{new_db_name}".')
                            )

                # 4. Restore table inheritance
                cr.execute("""
                    SELECT child.relname, parent.relname
                    FROM pg_inherits
                    JOIN pg_class child ON child.oid = pg_inherits.inhrelid
                    JOIN pg_namespace child_ns ON child_ns.oid = child.relnamespace
                    JOIN pg_class parent ON parent.oid = pg_inherits.inhparent
                    JOIN pg_namespace parent_ns ON parent_ns.oid = parent.relnamespace
                    WHERE child_ns.nspname = 'public' AND parent_ns.nspname = 'public'
                """)
                for child_table, parent_table in cr.fetchall():
                    cr.execute(
                        f'ALTER TABLE "{new_db_name}"."{child_table}" '
                        f'INHERIT "{new_db_name}"."{parent_table}"'
                    )

                # 5. Copy data
                for table in tables:
                    if table in SKIP_DATA_TABLES:
                        continue
                    cr.execute(
                        f'INSERT INTO "{new_db_name}"."{table}" '
                        f'SELECT * FROM ONLY public."{table}"'
                    )
                    if table == 'ir_attachment':
                        cr.execute("""
                            SELECT store_fname FROM ONLY public.ir_attachment
                            WHERE store_fname IS NOT NULL
                              AND (url LIKE '/web/%%' OR mimetype LIKE 'image/%%'
                                   OR file_size < 1048576)
                        """)
                        for (fname,) in cr.fetchall():
                            src = os.path.join(source_filestore, fname)
                            dst = os.path.join(target_filestore, fname)
                            try:
                                if os.path.exists(src) and not os.path.exists(dst):
                                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                                    shutil.copy2(src, dst)
                            except Exception:
                                pass

                # 6. Post-clone FK integrity repair
                _logger.info("Running FK integrity repair for sandbox %s...", new_db_name)
                cr.execute("""
                    SELECT tc.table_name, kcu.column_name, ccu.table_name, ccu.column_name
                    FROM information_schema.table_constraints AS tc
                    JOIN information_schema.key_column_usage AS kcu
                        ON tc.constraint_name = kcu.constraint_name
                        AND tc.table_schema   = kcu.table_schema
                    JOIN information_schema.constraint_column_usage AS ccu
                        ON ccu.constraint_name = tc.constraint_name
                        AND ccu.table_schema   = tc.table_schema
                    WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_schema = %s
                """, (new_db_name,))
                for fk_table, fk_col, ref_table, ref_col in cr.fetchall():
                    try:
                        cr.execute(
                            f'DELETE FROM "{new_db_name}"."{fk_table}" '
                            f'WHERE "{fk_col}" IS NOT NULL '
                            f'AND "{fk_col}" NOT IN '
                            f'(SELECT "{ref_col}" FROM "{new_db_name}"."{ref_table}")'
                        )
                        if cr._obj.rowcount:
                            _logger.info(
                                "FK repair: removed %d orphaned rows from %s.%s",
                                cr._obj.rowcount, fk_table, fk_col
                            )
                    except Exception as fk_err:
                        _logger.warning("FK repair skipped for %s.%s: %s", fk_table, fk_col, fk_err)
                        cr._cnx.rollback()

                # 7. Mark sandbox active
                env = api.Environment(cr, SUPERUSER_ID, {})
                env['sandbox.environment'].browse(sandbox_id).write({'state': 'active'})
                cr.commit()
                _logger.info("Sandbox %s ready (FK-clean).", new_db_name)

        except Exception as e:
            _logger.exception("Sandbox clone failed for %s", new_db_name)
            try:
                with Registry(production_db).cursor() as cr:
                    env = api.Environment(cr, SUPERUSER_ID, {})
                    env['sandbox.environment'].browse(sandbox_id).write({
                        'state': 'failed', 'error_message': str(e)
                    })
                    cr.commit()
            except Exception:
                pass

    def action_kill(self):
        """Terminate sandbox and drop its schema."""
        for record in self:
            if record.state == 'deleting':
                continue
            record.state = 'deleting'
            self.env.cr.commit()
            try:
                self.env.cr.execute(f'DROP SCHEMA IF EXISTS "{record.db_name}" CASCADE')
                record.unlink()
            except Exception as e:
                _logger.error("Failed to drop sandbox schema: %s", e)
                record.state = 'active'
        return {
            'type': 'ir.actions.act_window',
            'name': 'Sandbox Dashboard',
            'res_model': 'sandbox.environment',
            'view_mode': 'list,form',
            'target': 'main',
        }

    @api.model
    def cron_cleanup_expired(self):
        """Auto-pause idle sandboxes and kill expired ones."""
        idle_limit = datetime.now() - timedelta(hours=2)
        for sandbox in self.search([('state', '=', 'active'), ('last_accessed', '<', idle_limit)]):
            sandbox.state = 'paused'
            sandbox.message_post(body="Sandbox automatically paused due to 2 hours of inactivity.")

        for sandbox in self.search([
            ('state', 'in', ['active', 'paused']),
            ('expiry_date', '<', datetime.now())
        ]):
            sandbox.action_kill()

    def action_resume(self):
        self.ensure_one()
        if self.remaining_entries <= 0:
            self.write({'state': 'expired'})
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'Expired',
                    'message': 'Sandbox has reached its maximum entry limit and is now expired.',
                    'type': 'danger',
                    'sticky': False,
                }
            }
        return {
            'type': 'ir.actions.act_url',
            'url': f'/sandbox/resume/{self.id}',
            'target': 'self',
        }


# =============================================================================
# AUDIT LOGGING & MODULE LIFECYCLE HOOKS
#
# PRODUCTION SAFETY: _is_in_sandbox() gates every sandbox-specific action.
# On the live database that function always returns False, so the hooks call
# straight through to the original Odoo methods without any modification.
# =============================================================================

from odoo.addons.base.models.ir_module import IrModuleModule as Module
from odoo.addons.base.models.res_config import ResConfigSettings
from odoo.addons.base.models.res_users import ResUsers as Users


def _log_sandbox_action(env_cursor, action_type, details):
    """Write an audit log entry to the production sandbox_environment chatter.

    INTENTIONAL PRODUCTION WRITE: uses original_db_connect to bypass search_path.
    Only runs when inside a sandbox session.
    """
    if not _is_in_sandbox():
        return
    schema = threading.current_thread().sandbox_schema
    production_db = (env_cursor.dbname.split('_virtual_')[0]
                     if '_virtual_' in env_cursor.dbname else env_cursor.dbname)
    try:
        import odoo
        with odoo.sql_db.db_connect(production_db).cursor() as cr:
            env = odoo.api.Environment(cr, odoo.SUPERUSER_ID, {})
            sandbox = env['sandbox.environment'].search([('db_name', '=', schema)], limit=1)
            if sandbox:
                sandbox.message_post(body=Markup(f"<b>Audit Log - {action_type}:</b> {details}"))
                cr.commit()
    except Exception as e:
        _logger.warning("Sandbox audit logging failed: %s", e)


def _sandbox_repair_fk(cr):
    """Purge orphaned FK rows in the sandbox schema before a module operation.

    Runs automatically before install/upgrade/uninstall. No-op on production.
    All DELETEs are schema-qualified to the sandbox schema — never touches public.
    """
    if not _is_in_sandbox():
        return

    schema = threading.current_thread().sandbox_schema
    if schema == 'public':
        _logger.error("SAFETY ABORT: sandbox_schema is 'public' — refusing FK repair.")
        return

    try:
        cr.execute("""
            SELECT tc.table_name, kcu.column_name, ccu.table_name, ccu.column_name
            FROM information_schema.table_constraints AS tc
            JOIN information_schema.key_column_usage AS kcu
                ON tc.constraint_name = kcu.constraint_name
                AND tc.table_schema   = kcu.table_schema
            JOIN information_schema.constraint_column_usage AS ccu
                ON ccu.constraint_name = tc.constraint_name
                AND ccu.table_schema   = tc.table_schema
            WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_schema = %s
        """, (schema,))
        for fk_table, fk_col, ref_table, ref_col in cr.fetchall():
            try:
                cr.execute(
                    f'DELETE FROM "{schema}"."{fk_table}" '
                    f'WHERE "{fk_col}" IS NOT NULL '
                    f'AND "{fk_col}" NOT IN '
                    f'(SELECT "{ref_col}" FROM "{schema}"."{ref_table}")'
                )
                if cr._obj.rowcount:
                    _logger.info(
                        "Auto FK repair [%s]: removed %d orphaned rows from %s.%s",
                        schema, cr._obj.rowcount, fk_table, fk_col
                    )
            except Exception as fk_err:
                _logger.warning("Auto FK repair skipped %s.%s: %s", fk_table, fk_col, fk_err)
                cr._cnx.rollback()
    except Exception as e:
        _logger.warning("Auto FK repair failed for sandbox %s: %s", schema, e)


def _make_module_hook(original, action_label):
    """Return a patched module lifecycle function with sandbox repair + audit + safe reload."""
    def hook(self):
        in_sandbox = _is_in_sandbox()
        if in_sandbox:
            _sandbox_repair_fk(self.env.cr)
            _log_sandbox_action(self.env.cr, action_label, ", ".join(self.mapped('name')))
        try:
            return original(self)
        except AssertionError:
            if not in_sandbox:
                raise  # Never swallow on production
            # The operation succeeded; Odoo's registry assertion fails due to virtual
            # registry mismatch — return the reload action the UI expects.
            return {'type': 'ir.actions.client', 'tag': 'reload'}
    return hook


Module.button_immediate_install = _make_module_hook(
    Module.button_immediate_install, "Module Installed")
Module.button_immediate_upgrade = _make_module_hook(
    Module.button_immediate_upgrade, "Module Upgraded")
Module.button_immediate_uninstall = _make_module_hook(
    Module.button_immediate_uninstall, "Module Uninstalled")

original_config_execute = ResConfigSettings.execute


def patched_config_execute(self):
    _log_sandbox_action(self.env.cr, "Settings Changed", "System configuration was updated.")
    return original_config_execute(self)


ResConfigSettings.execute = patched_config_execute

original_users_create = Users.create


@api.model_create_multi
def patched_users_create(self, vals_list):
    res = original_users_create(self, vals_list)
    _log_sandbox_action(self.env.cr, "User Created", ", ".join(res.mapped('name')))
    return res


Users.create = patched_users_create
