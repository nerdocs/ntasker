// Tag-management page logic. Loaded by templates/tags.html.
// Mirrors the structure of app.js / settings.html: a global factory used by
// `x-data="tagsPage()"` plus the shared $i18n Alpine magic.

// Register the same $i18n magic the other pages use (data comes from the
// inline window.__i18n bridge the template injects).
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

function tagsPage() {
    return {
        tags: [],          // [{name, open_count, total_count}]
        filter: '',

        // Rename/merge modal state. renameTarget === null -> closed.
        renameTarget: null,
        renameValue: '',

        // Delete modal state. deleteTarget === null -> closed.
        deleteTarget: null,
        deleteTasks: [],
        deleteLoading: false,

        async init() {
            await this.load();
        },

        // Translate with optional {placeholder} substitution (mirrors $i18n).
        i18n(key, params) {
            let s = (window.__i18n && window.__i18n[key]) || key;
            if (params) {
                for (const [k, v] of Object.entries(params)) {
                    s = s.replace(new RegExp('\\{' + k + '\\}', 'g'), v);
                }
            }
            return s;
        },

        async load() {
            const r = await fetch('/api/tags');
            this.tags = await r.json();
        },

        get filtered() {
            const q = this.filter.trim().toLowerCase();
            return q ? this.tags.filter(t => t.name.includes(q)) : this.tags;
        },

        // Normalised target name -- matches the backend's normalize_tag().
        get renameValueNorm() {
            return (this.renameValue || '').trim().toLowerCase();
        },

        // True when the typed name already exists as a different tag (merge).
        get renameIsMerge() {
            const n = this.renameValueNorm;
            return !!n && n !== this.renameTarget
                && this.tags.some(t => t.name === n);
        },

        // ---- Clean up unused tags ----
        async cleanup() {
            const r = await fetch('/api/tags/cleanup', { method: 'POST' });
            if (!r.ok) { this.toast(this.i18n('cleanup_failed'), 'danger'); return; }
            const data = await r.json();
            const removed = data.removed || 0;
            const names = Array.isArray(data.removed_names) ? data.removed_names : [];
            if (removed === 0) {
                this.toast(this.i18n('cleanup_none'));
            } else {
                const head = names.slice(0, 5).join(', ');
                const tail = names.length > 5
                    ? this.i18n('cleanup_more', { n: names.length - 5 }) : '';
                this.toast(this.i18n('cleanup_removed', { n: removed, head: head, tail: tail }), 'success');
            }
            await this.load();
        },

        // ---- Rename / merge ----
        openRename(name) {
            this.renameTarget = name;
            this.renameValue = name;
            this.$nextTick(() => {
                const el = this.$refs.renameInput;
                if (el) { el.focus(); el.select(); }
            });
        },

        closeRename() {
            this.renameTarget = null;
            this.renameValue = '';
        },

        async doRename() {
            const target = this.renameValueNorm;
            if (!target || target === this.renameTarget) return;
            const source = this.renameTarget;
            const r = await fetch('/api/tags/merge', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ sources: [source], target: target }),
            });
            if (!r.ok) { this.toast(this.i18n('merge_failed'), 'danger'); return; }
            const data = await r.json();
            this.closeRename();
            await this.load();
            this.toast(this.i18n('merge_done', { n: data.affected || 0, name: data.target || target }), 'success');
        },

        // ---- Delete ----
        async openDelete(name) {
            this.deleteTarget = name;
            this.deleteTasks = [];
            this.deleteLoading = true;
            try {
                const r = await fetch(`/api/tags/${encodeURIComponent(name)}/tasks`);
                this.deleteTasks = r.ok ? await r.json() : [];
            } finally {
                this.deleteLoading = false;
            }
        },

        closeDelete() {
            this.deleteTarget = null;
            this.deleteTasks = [];
        },

        async doDelete() {
            const name = this.deleteTarget;
            const r = await fetch('/api/tags/delete', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ names: [name] }),
            });
            if (!r.ok) { this.toast(this.i18n('delete_tag_failed'), 'danger'); return; }
            this.closeDelete();
            await this.load();
            this.toast(this.i18n('delete_tag_done', { name: name }), 'success');
        },

        toast(msg, kind) {
            const c = document.getElementById('toast-container');
            const el = document.createElement('div');
            const bg = kind === 'danger' ? 'text-bg-danger'
                : kind === 'success' ? 'text-bg-success' : 'text-bg-secondary';
            el.className = `toast show align-items-center ${bg} border-0`;
            el.setAttribute('role', 'alert');
            el.innerHTML = `<div class="d-flex"><div class="toast-body"></div></div>`;
            el.querySelector('.toast-body').textContent = msg;
            c.appendChild(el);
            setTimeout(() => el.remove(), 3000);
        },
    };
}
