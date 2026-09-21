// Settings page logic. Loaded by templates/settings.html.
// Same shape as info.js / tags.js: a global factory for x-data plus the shared
// $i18n Alpine magic. The template hands over its static data through
// window.__settings (known keys, effective defaults, enum choices, plugins).

document.addEventListener('alpine:init', () => {
    Alpine.magic('i18n', () => (key, params) => {
        let s = (window.__i18n && window.__i18n[key]) || key;
        if (params) {
            for (const [k, v] of Object.entries(params)) {
                s = s.replace(new RegExp('\\{' + k + '\\}', 'g'), v);
            }
        }
        return s;
    });
});

// Section rail entries; the URL hash addresses one directly (/settings#plugins).
const SETTINGS_TABS = ['general', 'agents', 'plugins', 'maintenance'];

// Effective default of every boolean setting when its key is unset.
const BOOL_DEFAULTS = {
    claude_open_terminal: true,
    queue_enabled: true,
    dir_locks: true,
    require_clean: false,
    quicktasks_bypass_lanes: true,
    misc_no_memory: false,
    opencode_auto: false,
};
const TRUE_STRINGS = ['1', 'true', 'yes', 'on'];

function settingsPage() {
    const cfg = window.__settings || {knownKeys: [], fieldDefaults: {}, fieldChoices: {}, plugins: []};
    return {
        tab: 'general',
        rows: [],
        // Stored (or effective default) value per known key -- what the
        // controls reflect.
        known: {},
        // Text-field drafts; a field saves when its draft differs from known.
        draft: {},
        // Editor rows of the `quick_prompts` list ([{label, prompt}]); saved
        // as one JSON value via savePrompts().
        prompts: [],
        errors: {},
        // True while a restart is in flight -- disables the button and drives
        // the "Restarting..." label until the server answers /healthz again.
        restarting: false,
        // boot_id of the server we asked to restart, captured before the POST.
        // waitForServer reloads only once a *different* process answers.
        _bootId: null,
        // Task ids with a live session (running OR input-waiting). A restart
        // kills the whole cgroup, so while this is non-empty the button is
        // disabled and an info banner explains why. Polled by refreshSessions.
        activeTasks: [],
        // True while a VACUUM/optimize is in flight -- disables the button.
        cleaning: false,
        // Shell completion state (GET /api/completion): shell -> {installed, rc}.
        completion: {},
        // Shell whose install/remove request is in flight -- disables its button.
        completionBusy: null,
        // Agent whose integration install is in flight -- disables its button.
        assetsBusy: null,
        // Agent registry (GET /api/agents): enabled agents only, each with
        // {key,label,icon,available,assets}. Drives the agent cards.
        agents: [],
        agentDefault: 'claude',
        // Plugin list (GET /api/plugins), seeded server-side so the switches
        // and the rail badge render without a flash.
        plugins: cfg.plugins,
        // Running / last install of a plugin's extra (GET /api/plugins/install).
        installJob: null,

        async init() {
            this.readHash();
            window.addEventListener('hashchange', () => this.readHash());
            await this.refresh();
            await this.refreshPlugins();
            await this.refreshAgents();
            await this.refreshSessions();
            await this.refreshCompletion();
            // Keep the restart guard live -- a task may start or end while the
            // page is open. Cheap poll; the page reloads itself on restart.
            setInterval(() => this.refreshSessions(), 4000);
        },

        // ---- section rail -------------------------------------------------

        readHash() {
            const h = (location.hash || '').slice(1);
            this.tab = SETTINGS_TABS.includes(h) ? h : 'general';
        },

        go(tab) {
            this.tab = tab;
            history.replaceState(null, '', '#' + tab);
        },

        // ---- plugin helpers -----------------------------------------------

        pluginOn(name) {
            const p = this.plugins.find(p => p.name === name);
            return !!(p && p.enabled);
        },

        pluginsOn() {
            return this.plugins.filter(p => p.enabled).length;
        },

        agentFor(key) {
            return this.agents.find(a => a.key === key) || null;
        },

        // Any enabled agent whose CLI cannot be launched -- rail warning dot.
        agentsMissing() {
            return this.agents.some(a => !a.available);
        },

        // Any agent whose /task integration is missing or outdated -- red rail
        // dot on Plugins, where the install hint lives (agentAssetsTodo: topnav.js).
        assetsTodo() {
            return this.agents.some(agentAssetsTodo);
        },

        // ---- value helpers ------------------------------------------------

        isOn(key) {
            const v = (this.known[key] || '').trim().toLowerCase();
            if (!v) return !!BOOL_DEFAULTS[key];
            return TRUE_STRINGS.includes(v);
        },

        isDirty(key) {
            return (this.draft[key] || '') !== (this.known[key] || '');
        },

        revert(key) {
            this.draft[key] = this.known[key] || '';
        },

        // True when a value is stored for key (as opposed to its effective
        // default being shown).
        isStored(key) {
            return this.rows.some(r => r.key === key);
        },

        // Description of the active option of an enum setting.
        choiceDesc(key) {
            const opt = (cfg.fieldChoices[key] || []).find(o => o.value === this.known[key]);
            return opt ? opt.desc : '';
        },

        // Translate an i18n key with optional {placeholder} substitution.
        // Mirrors the Alpine `$i18n` magic so the same dict drives both.
        i18n(key, params) {
            let s = (window.__i18n && window.__i18n[key]) || key;
            if (params) {
                for (const [k, v] of Object.entries(params)) {
                    s = s.replace(new RegExp('\\{' + k + '\\}', 'g'), v);
                }
            }
            return s;
        },

        // ---- loading ------------------------------------------------------

        async refresh() {
            const r = await fetch('/api/settings');
            this.rows = await r.json();
            const byKey = {};
            for (const row of this.rows) byKey[row.key] = row.value;
            // Populate ALL known keys (incl. unset ones) so the bindings have a
            // slot. Unset keys fall back to their effective default so the
            // matching control shows the value actually in force.
            for (const k of cfg.knownKeys) {
                this.known[k] = byKey[k] || cfg.fieldDefaults[k] || '';
                this.draft[k] = this.known[k];
            }
            this.prompts = JSON.parse(this.known['quick_prompts'] || '[]');
        },

        // Refresh the live-session list that gates the restart button.
        async refreshSessions() {
            try {
                const r = await fetch('/api/claude/sessions', {cache: 'no-store'});
                if (!r.ok) return;
                const body = await r.json();
                this.activeTasks = body.active || [];
            } catch (e) {
                // Best-effort -- leave the previous value on a transient error.
            }
        },

        async refreshAgents() {
            try {
                const r = await fetch('/api/agents');
                if (!r.ok) return;
                const data = await r.json();
                this.agents = data.agents || [];
                this.agentDefault = data.default || 'claude';
                // Reflect the effective default in the picker when unset, so it
                // shows the agent tasks actually fall back to.
                if (!this.known['default_agent']) this.known['default_agent'] = this.agentDefault;
            } catch (e) {
                // Best-effort -- the cards stay empty.
            }
        },

        async refreshPlugins() {
            try {
                const r = await fetch('/api/plugins');
                if (r.ok) this.plugins = await r.json();
                const j = await fetch('/api/plugins/install');
                if (j.ok) this.installJob = (await j.json()).job;
            } catch (e) {
                // Best-effort -- the seeded list stays.
            }
        },

        // ---- plugin extras (ntasker[<extra>] packages) --------------------

        pluginMissing(name) {
            const p = this.plugins.find(p => p.name === name);
            return (p && p.missing) || [];
        },

        installRunning(name) {
            const j = this.installJob;
            return !!j && j.state === 'running' && (!name || j.plugin === name);
        },

        installFailed(name) {
            const j = this.installJob;
            return !!j && j.state === 'failed' && j.plugin === name;
        },

        // A finished install in a `uv tool` home replaced ntasker's own files
        // too: the running server must restart before the plugin can use them.
        installRestartDue(name) {
            const j = this.installJob;
            return !!j && j.state === 'done' && j.restart && j.plugin === name && !this.pluginMissing(name).length;
        },

        // The card's "Install now": POST starts the install, then poll the job
        // until it ends. Success reloads the page so the plugin's own settings
        // slot (rendered server-side) sees the packages.
        async installExtra(name) {
            if (this.installRunning()) return;
            try {
                const r = await fetch(`/api/plugins/${name}/install`, {method: 'POST'});
                if (!r.ok) { this.toast((await r.json()).detail || this.i18n('plugin_extra_failed', {extra: name})); return; }
                this.installJob = (await r.json()).job;
            } catch (e) {
                return;
            }
            while (this.installRunning()) {
                await new Promise(res => setTimeout(res, 1500));
                try {
                    const j = await fetch('/api/plugins/install');
                    if (j.ok) this.installJob = (await j.json()).job;
                } catch (e) { /* keep polling */ }
            }
            await this.refreshPlugins();
            if (this.installJob && this.installJob.state === 'done') {
                const p = this.plugins.find(p => p.name === name);
                this.toast(this.i18n('plugin_extra_done', {extra: p ? p.extra : name}));
                if (!this.installJob.restart) location.reload();
            }
        },

        // ---- writing ------------------------------------------------------

        // Set + save a value chosen from a control (segmented, switch, picker).
        async setKnown(key, value) {
            this.known[key] = value;
            this.draft[key] = value;
            await this.saveKnown(key);
        },

        // Save a text draft (Enter / the Save button).
        async saveDraft(key) {
            this.known[key] = this.draft[key];
            await this.saveKnown(key);
        },

        async saveKnown(key) {
            this.errors[key] = '';
            const r = await fetch(`/api/settings/${encodeURIComponent(key)}`, {
                method: 'PUT',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({value: this.known[key]}),
            });
            if (!r.ok) {
                const body = await r.json().catch(() => ({}));
                this.errors[key] = body.detail || `HTTP ${r.status}`;
                // Put the control back on the stored value; keep the draft so
                // the user can fix the text instead of retyping it.
                const draft = this.draft[key];
                await this.refresh();
                this.draft[key] = draft;
                return;
            }
            await this.refresh();
            // A CLI path override changes whether the agent is launchable.
            if (key.endsWith('_bin')) await this.refreshAgents();
            this.toast(this.i18n('saved'));
        },

        promptsDirty() {
            return JSON.stringify(this.prompts) !== JSON.stringify(JSON.parse(this.known['quick_prompts'] || '[]'));
        },

        // Save the prompt rows as one JSON value. On a validation error the
        // rows stay as typed so the offending entry can be fixed in place.
        async savePrompts() {
            const rows = this.prompts.map(p => ({...p}));
            await this.setKnown('quick_prompts', JSON.stringify(rows));
            if (this.errors['quick_prompts']) this.prompts = rows;
        },

        async unsetKnown(key) {
            const r = await fetch(`/api/settings/${encodeURIComponent(key)}`, {method: 'DELETE'});
            if (r.status === 204) {
                this.errors[key] = '';
                await this.refresh();
                if (key.endsWith('_bin')) await this.refreshAgents();
                this.toast(this.i18n('removed'));
            }
        },

        // Flip one plugin switch: rewrite the plugins_disabled list (default-on
        // plugins) or the plugins_enabled list (opt-in plugins) and let the
        // server validate it (unknown name, last agent). The agent cards
        // follow, since /api/agents lists only enabled agents.
        async setPluginEnabled(name, enabled) {
            const plugin = this.plugins.find(p => p.name === name);
            const key = plugin.default_on ? 'plugins_disabled' : 'plugins_enabled';
            const listed = plugin.default_on ? !enabled : enabled;
            const names = this.plugins
                .filter(p => p.default_on === plugin.default_on && p.name !== name && p.enabled !== plugin.default_on)
                .map(p => p.name);
            if (listed) names.push(name);
            this.known[key] = JSON.stringify(names);
            await this.saveKnown(key);
            await this.refreshPlugins();
            await this.refreshAgents();
            // A plugin with its own settings template renders server-side.
            if (plugin.settings && !this.errors[key]) location.reload();
        },

        // Permission mode, read from the known-key slot refresh() fills. Falls
        // back to the legacy claude_auto_mode boolean (true -> bypass) and then
        // to 'default' so the radios always reflect the effective mode.
        get permissionMode() {
            const m = (this.known['claude_permission_mode'] || '').trim();
            if (['default', 'auto', 'plan', 'bypassPermissions'].includes(m)) return m;
            if (TRUE_STRINGS.includes((this.known['claude_auto_mode'] || '').toLowerCase())) return 'bypassPermissions';
            return 'default';
        },

        async setPermissionMode(mode) {
            await this.setKnown('claude_permission_mode', mode);
        },

        // ---- maintenance --------------------------------------------------

        // Restart the supervised server, then poll /healthz until it answers
        // again and reload. Only reachable when the service is installed -- the
        // button is server-rendered behind a can_restart guard.
        async restartServer() {
            if (this.restarting) return;
            this.restarting = true;
            // Remember which process we are talking to. A restart that failed to
            // replace it (e.g. the supervisor cannot bind because something else
            // holds the port) would otherwise answer /healthz straight away and
            // look like a success -- while still serving the old code.
            this._bootId = await this.serverBootId();
            try {
                const r = await fetch('/api/service/restart', {method: 'POST'});
                if (!r.ok) {
                    this.restarting = false;
                    // 409 tasks_running: a task slipped in between poll and click.
                    // Refresh the guard so the button disables, and explain.
                    const body = await r.json().catch(() => ({}));
                    if (body.reason === 'tasks_running') {
                        this.activeTasks = body.tasks || [];
                        this.toast(this.i18n('restart_blocked_tasks', {n: this.activeTasks.length}));
                    } else {
                        this.toast(this.i18n('restart_failed'));
                    }
                    return;
                }
            } catch (e) {
                // The socket may drop as the process goes down -- expected.
            }
            this.waitForServer();
        },

        // The running server's process identity, or null when it is unreachable.
        async serverBootId() {
            try {
                const r = await fetch('/healthz', {cache: 'no-store'});
                if (!r.ok) return null;
                return (await r.json()).boot_id || null;
            } catch (e) {
                return null;
            }
        },

        // Poll /healthz after a restart; reload once a *different* process
        // answers. Waiting for any answer is not enough -- see restartServer.
        waitForServer(tries = 0) {
            setTimeout(async () => {
                const id = await this.serverBootId();
                if (id && id !== this._bootId) { location.reload(); return; }
                if (tries < 40) {
                    this.waitForServer(tries + 1);
                    return;
                }
                this.restarting = false;
                // Still the same process: the restart was accepted but never
                // took effect. Say that instead of reloading into old code.
                this.toast(this.i18n(id ? 'restart_unchanged' : 'restart_timeout'));
            }, tries === 0 ? 1000 : 500);
        },

        // Compact the database (VACUUM + optimize) and report the bytes freed.
        async cleanupDatabase() {
            if (this.cleaning) return;
            this.cleaning = true;
            try {
                const r = await fetch('/api/maintenance/cleanup', {method: 'POST'});
                if (!r.ok) { this.toast(this.i18n('db_cleanup_failed')); return; }
                const body = await r.json();
                if (body.bytes_freed > 0) {
                    this.toast(this.i18n('db_cleanup_done', {freed: this.fmtBytes(body.bytes_freed)}));
                } else {
                    this.toast(this.i18n('db_cleanup_compact'));
                }
            } catch (e) {
                this.toast(this.i18n('db_cleanup_failed'));
            } finally {
                this.cleaning = false;
            }
        },

        async refreshCompletion() {
            try {
                const r = await fetch('/api/completion');
                if (r.ok) this.completion = await r.json();
            } catch (e) { /* leave the table empty */ }
        },

        // Install (or force-update) one agent's /task integration -- the
        // card's button; POST /api/agents/<key>/assets/install. `force` is
        // what the drift case needs: drifted files are backed up, then replaced.
        async installAgentAssets(key, force) {
            if (this.assetsBusy) return;
            this.assetsBusy = key;
            try {
                const r = await fetch(`/api/agents/${key}/assets/install`, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({force: !!force}),
                });
                const data = r.ok ? await r.json() : null;
                if (!data || !data.success) { this.toast(this.i18n('agent_install_failed')); return; }
                this.toast(this.i18n('agent_install_done'));
                await this.refreshAgents();
            } catch (e) {
                this.toast(this.i18n('agent_install_failed'));
            } finally {
                this.assetsBusy = null;
            }
        },

        // Install or remove the completion script for one shell.
        async toggleCompletion(shell) {
            if (this.completionBusy) return;
            this.completionBusy = shell;
            const installed = this.completion[shell]?.installed;
            try {
                const r = await fetch(`/api/completion/${shell}`, {method: installed ? 'DELETE' : 'POST'});
                if (!r.ok) { this.toast(this.i18n('completion_failed')); return; }
                this.toast(this.i18n(installed ? 'completion_removed' : 'completion_done'));
                await this.refreshCompletion();
            } catch (e) {
                this.toast(this.i18n('completion_failed'));
            } finally {
                this.completionBusy = null;
            }
        },

        // Human-readable byte count for the cleanup toast (e.g. "12.3 KB").
        fmtBytes(n) {
            if (n < 1024) return `${n} B`;
            const units = ['KB', 'MB', 'GB'];
            let v = n / 1024, i = 0;
            while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
            return `${v.toFixed(1)} ${units[i]}`;
        },

        toast(msg) {
            const c = document.getElementById('toast-container');
            const el = document.createElement('div');
            el.className = 'toast show align-items-center text-bg-secondary border-0';
            el.setAttribute('role', 'alert');
            el.innerHTML = `<div class="d-flex"><div class="toast-body">${msg}</div></div>`;
            c.appendChild(el);
            setTimeout(() => el.remove(), 3000);
        },
    };
}
