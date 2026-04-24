/** @odoo-module **/
import { Component, useState } from "@odoo/owl";
import { rpc } from "@web/core/network/rpc";
import { session } from "@web/session";
import { registry } from "@web/core/registry";
import { _t } from "@web/core/l10n/translation";
import { user } from "@web/core/user";

export class SandboxSystray extends Component {
    static template = "database_sandbox.SandboxSystray";
    
    setup() {
        this.session = session;
        this.state = useState({
            isCloning: false,
        });
    }

    async startSandbox() {
        if (this.state.isCloning) return;   // prevent double-click
        if (confirm(_t("This will create a full clone of the database. This may take a few minutes and will briefly disconnect all users. Continue?"))) {
            this.state.isCloning = true;
            const result = await rpc("/sandbox/start", {});
            if (result.success) {
                const pollStatus = async () => {
                    const status = await rpc("/sandbox/status", { sandbox_id: result.sandbox_id });
                    if (status.success) {
                        if (status.state === 'active') {
                            window.location.reload();
                        } else if (status.state === 'failed') {
                            alert(_t("Cloning failed: ") + status.error_message);
                            this.state.isCloning = false;
                        } else {
                            setTimeout(pollStatus, 5000);
                        }
                    } else {
                        alert(status.error);
                        this.state.isCloning = false;
                    }
                };
                alert(_t("Cloning started in background. You will be automatically redirected when ready. Please do not close this tab."));
                pollStatus();
            } else {
                alert(result.error);
                this.state.isCloning = false;
            }
        }
    }

    async stopSandbox() {
        if (confirm(_t("Are you sure you want to exit the sandbox? You will be returned to the production database."))) {
            const result = await rpc("/sandbox/stop", {});
            if (result.success) {
                window.location.href = result.redirect_url;
            } else {
                alert(result.error);
            }
        }
    }
}

export const sandboxSystrayItem = {
    Component: SandboxSystray,
    isDisplayed: () => user.isAdmin,
};

registry.category("systray").add("SandboxSystray", sandboxSystrayItem, { sequence: 100 });

// Inject sandbox theming globally for all users so the UI stays red in the sandbox
// Must wait for DOM to be ready before accessing document.body
if (session.is_sandbox) {
    if (document.body) {
        document.body.classList.add('o_sandbox_mode');
    } else {
        document.addEventListener('DOMContentLoaded', () => {
            document.body.classList.add('o_sandbox_mode');
        });
    }
}
