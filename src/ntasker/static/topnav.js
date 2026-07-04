// Self-contained top-bar component shared by every page (index, settings,
// tags, info). Owns the dark/light theme toggle and the "update available"
// badge on the info icon -- independent of each page's main Alpine component
// so the _topnav.html partial drops in anywhere. Loaded as a classic (non
// -deferred) script ahead of Alpine, like app.js / info.js / tags.js.
//
// The theme localStorage key matches app.js' LS_KEY_THEME ('ntasker.theme');
// the literal is inlined here to avoid a duplicate top-level `const` when both
// scripts share the global scope on the index page.
function topnav(pageVersion = '') {
    return {
        theme: localStorage.getItem('ntasker.theme') || 'light',
        // True once /api/update-check reports a newer release on PyPI; drives
        // the red dot on the info icon. Stays false offline / on error.
        updateAvailable: false,
        // Version the running server reports on /healthz. When it differs
        // from the version this page was rendered with (a `self-update`
        // restarted the service underneath us), the stale-page banner shows.
        // Never reloads automatically -- a reload could lose form input.
        staleVersion: false,
        serverVersion: '',

        init() {
            this.applyTheme();
            this.loadUpdateInfo();
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

        // Poll the server-side (cached) PyPI check. Failures stay silent --
        // no badge is strictly better than a misleading one.
        async loadUpdateInfo() {
            try {
                const r = await fetch('/api/update-check');
                const data = await r.json();
                this.updateAvailable = !!data.update_available;
            } catch (e) {
                this.updateAvailable = false;
            }
        },
    };
}
