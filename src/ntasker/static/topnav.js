// Self-contained top-bar component shared by every page (index, settings,
// tags, info). Owns the dark/light theme toggle and the update check button
// (badge + install panel) -- independent of each page's main Alpine component
// so the _topnav.html partial drops in anywhere. Loaded as a classic (non
// -deferred) script ahead of Alpine, like app.js / info.js / tags.js.
//
// The theme localStorage key matches app.js' LS_KEY_THEME ('ntasker.theme');
// the literal is inlined here to avoid a duplicate top-level `const` when both
// scripts share the global scope on the index page.
// An agent whose /task integration needs a CLI step (`ntasker agent install`):
// its CLI works but the skill / slash command is missing or differs from the
// package. Shared with settings.js for the rail badge and the plugin cards.
function agentAssetsTodo(agent) {
    return !!agent && agent.available && (!agent.assets.installed || agent.assets.drift);
}

function topnav(pageVersion = '') {
    return {
        theme: localStorage.getItem('ntasker.theme') || 'light',
        // True once /api/update-check reports a newer release on PyPI; drives
        // the red dot on the update button. Stays false offline / on error.
        updateAvailable: false,
        // Update panel under the button: '' (hidden) | 'checking' | 'available'
        // | 'uptodate' | 'error' | 'installing' | 'blocked' (task sessions
        // running) | 'failed' | 'done' (server restarting) | 'done_manual'
        // (not supervised, or a task started meanwhile -- restart by hand).
        updateState: '',
        latest: '',
        updateOutput: '',
        // Version the running server reports on /healthz. When it differs
        // from the version this page was rendered with (a `self-update`
        // restarted the service underneath us), the stale-page banner shows.
        // Never reloads automatically -- a reload could lose form input.
        staleVersion: false,
        serverVersion: '',
        // True while some enabled agent with a working CLI has its /task
        // integration missing or outdated (GET /api/agents) -- drives the red
        // dot on the settings cog so the fix is found without going looking.
        settingsAttention: false,

        init() {
            this.applyTheme();
            this.loadUpdateInfo();
            this.loadSettingsAttention();
            if (pageVersion) {
                this._versionTimer = setInterval(() => this._checkServerVersion(pageVersion), 30000);
            }
        },

        // Compare the rendered page version against the live server's.
        // Failures stay silent (server restarting / unreachable); once the
        // mismatch is detected the poll stops -- the banner won't un-stale.
        async _checkServerVersion(pageVersion) {
            let v;
            try {
                const r = await fetch('/healthz');
                if (!r.ok) return;
                v = (await r.json()).version;
            } catch (e) {
                return;
            }
            if (!v || v === pageVersion) return;
            this.serverVersion = v;
            this.staleVersion = true;
            clearInterval(this._versionTimer);
        },

        applyTheme() {
            document.documentElement.setAttribute('data-bs-theme', this.theme);
        },

        toggleTheme() {
            this.theme = this.theme === 'dark' ? 'light' : 'dark';
            localStorage.setItem('ntasker.theme', this.theme);
            this.applyTheme();
        },

        async loadSettingsAttention() {
            try {
                const r = await fetch('/api/agents');
                const data = await r.json();
                this.settingsAttention = (data.agents || []).some(agentAssetsTodo);
            } catch (e) {
                this.settingsAttention = false;
            }
        },

        // Poll the server-side (cached) PyPI check. Failures stay silent --
        // no badge is strictly better than a misleading one.
        async loadUpdateInfo() {
            try {
                const r = await fetch('/api/update-check');
                const data = await r.json();
                this.updateAvailable = !!data.update_available;
                this.latest = data.latest || '';
            } catch (e) {
                this.updateAvailable = false;
            }
        },

        updateBusy() {
            return this.updateState === 'checking' || this.updateState === 'installing';
        },

        updateAlertClass() {
            switch (this.updateState) {
                case 'available': case 'blocked': return 'alert-warning';
                case 'uptodate': case 'done': case 'done_manual': return 'alert-success';
                case 'error': case 'failed': return 'alert-danger';
                default: return 'alert-info';
            }
        },

        updateMessage() {
            switch (this.updateState) {
                case 'checking': return this.$i18n('checking_updates');
                case 'available':
                    return this.$i18n('update_install_prompt', { latest: this.latest, current: pageVersion });
                case 'uptodate': return this.$i18n('up_to_date');
                case 'error': return this.$i18n('update_check_failed');
                case 'installing': return this.$i18n('update_installing');
                case 'blocked': return this.$i18n('update_blocked_tasks', { n: this.updateOutput });
                case 'failed': return this.$i18n('update_failed') + ' ' + this.updateOutput;
                case 'done': return this.$i18n('update_done_restart');
                case 'done_manual': return this.$i18n('update_done_manual');
                default: return '';
            }
        },

        // The top-bar button: force a fresh PyPI check (skips the 24h cache)
        // and open the panel with the result.
        async checkUpdates() {
            if (this.updateBusy()) return;
            this.updateState = 'checking';
            let data;
            try {
                data = await (await fetch('/api/update-check?force=true')).json();
            } catch (e) {
                data = { error: String(e) };
            }
            this.updateAvailable = !!data.update_available;
            this.latest = data.latest || '';
            this.updateState = data.error ? 'error' : (this.updateAvailable ? 'available' : 'uptodate');
        },

        // "Install update": POST starts the upgrade, then poll the job until it
        // ends. A supervised server restarts itself on success -- the job
        // vanishes with the old process, and the stale-page banner takes over
        // once /healthz reports the new version (polled fast until then).
        async installUpdate() {
            this.updateState = 'installing';
            this.updateOutput = '';
            let r;
            try {
                r = await fetch('/api/self-update', { method: 'POST' });
            } catch (e) {
                this.updateState = 'failed';
                this.updateOutput = String(e);
                return;
            }
            if (!r.ok) {
                const body = await r.json().catch(() => ({}));
                if (body.reason === 'tasks_running') {
                    this.updateState = 'blocked';
                    this.updateOutput = (body.tasks || []).length;
                } else {
                    this.updateState = 'failed';
                    this.updateOutput = body.detail || r.statusText;
                }
                return;
            }
            let job = (await r.json()).job;
            while (job && job.state === 'running') {
                await new Promise(res => setTimeout(res, 1500));
                try {
                    const j = await fetch('/api/self-update');
                    if (j.ok) job = (await j.json()).job;
                } catch (e) { /* server going down for the restart */ }
            }
            if (job && job.state === 'failed') {
                this.updateState = 'failed';
                this.updateOutput = job.output;
                return;
            }
            this.updateState = job && !job.restart ? 'done_manual' : 'done';
            if (this.updateState === 'done' && pageVersion) {
                clearInterval(this._versionTimer);
                this._versionTimer = setInterval(() => this._checkServerVersion(pageVersion), 3000);
            }
        },
    };
}
