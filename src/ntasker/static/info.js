// Info / About page logic. Loaded by templates/info.html.
// Same shape as tags.js / settings page: a global factory for x-data plus the
// shared $i18n Alpine magic. Pulls the server-side (cached) PyPI update check.

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

function infoPage() {
    return {
        loading: true,
        current: '',
        latest: null,
        updateAvailable: false,
        error: null,

        async init() {
            // Honour the theme the user picked on the main page (the sibling
            // subpages stay light; the info page is new, so sync it).
            const theme = localStorage.getItem('ntasker.theme') || 'light';
            document.documentElement.setAttribute('data-bs-theme', theme);
            await this.load();
        },

        async load() {
            try {
                const r = await fetch('/api/update-check');
                const data = await r.json();
                this.current = data.current;
                this.latest = data.latest;
                this.updateAvailable = !!data.update_available;
                this.error = data.error;
            } catch (e) {
                this.error = String(e);
            } finally {
                this.loading = false;
            }
        },
    };
}
