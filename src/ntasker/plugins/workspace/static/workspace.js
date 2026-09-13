// Workspace page logic. Loaded by templates/workspace.html.
// Mirrors the structure of tags.js: a global factory used by
// `x-data="workspacePage()"` plus the shared $i18n Alpine magic.
//
// Everything that the task-list sidebar also needs -- inventory scaffold,
// file rendering, formatting -- lives in ws-common.js (window.ntaskerWs),
// loaded before this file.

const WS = window.ntaskerWs;

// Register the same $i18n magic the other pages use (data comes from the
// inline window.__i18n bridge the template injects).
document.addEventListener('alpine:init', () => {
    Alpine.magic('i18n', () => (key, params) => WS.t(key, params));
});

function workspacePage() {
    return {
        loading: true,
        active: 'skills',
        data: WS.emptyInventory(),

        // Per-tab search terms, kept separate so switching tabs does not
        // carry a stale filter over into a different data set.
        q: { skills: '', team: '', docs: '' },
        kindFilter: '',

        viewer: {
            open: false,
            loading: false,
            error: '',
            file: null,
            mode: 'none',   // markdown | csv | text | none
            html: '',
            rows: [],
            copied: false,
            // Editing is off until asked for: the viewer is used to look
            // things up far more often than to change them, and a textarea
            // that opens by default would make every peek feel risky.
            editing: false,
            draft: '',
            saving: false,
        },

        // Inline "create a note here" form, per section.
        creating: { open: false, parent: '', name: '', busy: '', error: '' },

        // Transient status line under the tab bar (delete confirmations).
        flash: '',
        _flashTimer: null,

        async init() {
            await this.load();
            // Deep link: /workspace#team opens that tab directly.
            const hash = (location.hash || '').replace('#', '');
            if (this.tabs.some((t) => t.id === hash)) this.active = hash;
            this.$watch('active', (v) => history.replaceState(null, '', '#' + v));
        },

        async load() {
            this.loading = true;
            try {
                const res = await fetch('/api/workspace');
                if (res.ok) this.data = await res.json();
            } catch (e) {
                // Leave the empty scaffold in place -- every section then
                // renders its "not configured" notice instead of blowing up.
            } finally {
                this.loading = false;
            }
        },

        get tabs() {
            const d = this.data;
            return [
                { id: 'skills', label: 'ws_skills', icon: 'ti-puzzle', count: d.skills.total },
                { id: 'wiki', label: 'ws_knowledge', icon: 'ti-book', count: d.wiki.total_notes },
                { id: 'team', label: 'ws_team', icon: 'ti-users', count: d.team.total },
                { id: 'docs', label: 'ws_documents', icon: 'ti-files', count: d.docs.total },
                { id: 'tooling', label: 'ws_tooling', icon: 'ti-plug', count: d.tooling.servers.length },
            ];
        },

        // ---- helpers (thin delegates to ws-common) --------------------

        i18n(key, params) { return WS.t(key, params); },
        filtered(items, query, fields) { return WS.filterItems(items, query, fields); },
        iconFor(kind) { return WS.iconFor(kind); },
        fmtSize(bytes) { return WS.fmtSize(bytes); },
        fmtDate(seconds) { return WS.fmtDate(seconds); },
        escape(text) { return WS.escapeHtml(text); },
        editable(file) { return WS.isEditable(file); },

        visibleDocs() {
            let docs = this.filtered(this.data.docs.docs, this.q.docs, ['stem', 'name', 'suffix']);
            if (this.kindFilter) docs = docs.filter((d) => d.kind === this.kindFilter);
            return docs;
        },

        // Notice shown when a section's directory is unset or gone. Returns
        // HTML because it carries a link into the settings page.
        missingNotice(section) {
            const settings = `<a href="/settings" class="btn btn-sm btn-primary mt-2">${this.i18n('ws_open_settings')}</a>`;
            if (!section.configured) {
                return `<div class="text-secondary">${this.i18n('ws_configure_hint')}</div>${settings}`;
            }
            const path = this.escape(section.path);
            return `<div class="text-secondary">${this.i18n('ws_missing_dir')}
                    <code>${path}</code></div>${settings}`;
        },

        // ---- preview -------------------------------------------------

        async preview(path) {
            this.viewer.open = true;
            this.viewer.loading = true;
            this.viewer.error = '';
            this.viewer.file = null;
            this.viewer.html = '';
            this.viewer.rows = [];
            this.viewer.mode = 'none';
            this.viewer.copied = false;

            try {
                const res = await fetch('/api/workspace/file?path=' + encodeURIComponent(path));
                if (!res.ok) {
                    const body = await res.json().catch(() => ({}));
                    this.viewer.error = body.detail || this.i18n('ws_preview_failed');
                    return;
                }
                const file = await res.json();
                this.viewer.file = file;
                this.render(file);
            } catch (e) {
                this.viewer.error = this.i18n('ws_preview_failed');
            } finally {
                this.viewer.loading = false;
            }
        },

        render(file) {
            const out = WS.renderFile(file);
            this.viewer.mode = out.mode;
            this.viewer.html = out.html;
            this.viewer.rows = out.rows;
        },

        // ---- mutations -----------------------------------------------
        //
        // Every one of these hits an endpoint that is confined to the
        // configured workspace directories server-side. The UI does not
        // repeat that check -- it would only ever disagree with the server
        // -- it just surfaces whatever the server refuses.

        // Toggle the editor. Entering copies the file's text into a draft so
        // Cancel is a real cancel; leaving throws the draft away.
        toggleEdit() {
            if (this.viewer.editing) {
                this.viewer.editing = false;
                this.viewer.draft = '';
                return;
            }
            this.viewer.draft = this.viewer.file?.text ?? '';
            this.viewer.editing = true;
        },

        async saveFile() {
            const file = this.viewer.file;
            if (!file || this.viewer.saving) return;
            this.viewer.saving = true;
            this.viewer.error = '';
            try {
                const res = await fetch('/api/workspace/file', {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ path: file.path, text: this.viewer.draft }),
                });
                if (!res.ok) {
                    this.viewer.error = await WS.errorDetail(res, this.i18n('ws_save_failed'));
                    return;
                }
                const fresh = await res.json();
                this.viewer.file = fresh;
                this.render(fresh);
                this.viewer.editing = false;
                this.viewer.draft = '';
                // A write changes size and mtime, both of which the document
                // list shows -- refetch rather than patching the row by hand.
                await this.load();
            } catch (e) {
                this.viewer.error = this.i18n('ws_save_failed');
            } finally {
                this.viewer.saving = false;
            }
        },

        async renameFile() {
            const file = this.viewer.file;
            if (!file) return;
            const next = prompt(this.i18n('ws_rename_prompt'), file.name);
            if (next === null || next.trim() === file.name) return;
            const res = await fetch('/api/workspace/rename', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ path: file.path, name: next }),
            });
            if (!res.ok) {
                this.viewer.error = await WS.errorDetail(res, this.i18n('ws_rename_failed'));
                return;
            }
            const moved = await res.json();
            await this.load();
            await this.preview(moved.path);
        },

        async deleteFile() {
            const file = this.viewer.file;
            if (!file) return;
            if (!confirm(this.i18n('ws_confirm_delete', { name: file.name }))) return;
            const res = await fetch('/api/workspace/delete', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ path: file.path }),
            });
            if (!res.ok) {
                this.viewer.error = await WS.errorDetail(res, this.i18n('ws_delete_failed'));
                return;
            }
            const result = await res.json();
            this.viewer.open = false;
            // Say where it went: "deleted" from a browser is alarming enough
            // that the user deserves to know it is recoverable, and from
            // which of the two trashes.
            this.toast(
                result.method === 'os'
                    ? this.i18n('ws_trashed_os', { name: result.name })
                    : this.i18n('ws_trashed_folder', { name: result.name }),
            );
            await this.load();
        },

        // Hand the file to the desktop -- the only sane answer for a .docx
        // or .pdf, which the in-page previewer deliberately does not render.
        async openExternally(path) {
            const target = path || this.viewer.file?.path;
            if (!target) return;
            const res = await fetch('/api/workspace/reveal', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ path: target }),
            });
            if (!res.ok) {
                this.viewer.error = await WS.errorDetail(res, this.i18n('ws_open_failed'));
            }
        },

        startCreate(parent) {
            this.creating = { open: true, parent, name: '', busy: '', error: '' };
            this.$nextTick(() => this.$refs.newName?.focus());
        },

        async createNote() {
            if (this.creating.busy) return;
            const { parent, name } = this.creating;
            if (!name.trim()) return;
            this.creating.busy = 'yes';
            this.creating.error = '';
            try {
                const res = await fetch('/api/workspace/entry', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ parent, name }),
                });
                if (!res.ok) {
                    this.creating.error = await WS.errorDetail(res, this.i18n('ws_create_failed'));
                    return;
                }
                const created = await res.json();
                this.creating.open = false;
                await this.load();
                // Straight into the editor: a brand-new empty note is only
                // ever created in order to write in it.
                await this.preview(created.path);
                this.toggleEdit();
            } finally {
                this.creating.busy = '';
            }
        },

        // Minimal toast -- the workspace page has no toast container of its
        // own, and pulling in the task list's machinery for two messages
        // would be far more code than the message is worth.
        toast(message) {
            this.flash = message;
            clearTimeout(this._flashTimer);
            this._flashTimer = setTimeout(() => { this.flash = ''; }, 4000);
        },

        async copyPath() {
            const path = this.viewer.file?.path;
            if (!path) return;
            try {
                await navigator.clipboard.writeText(path);
                this.viewer.copied = true;
                setTimeout(() => { this.viewer.copied = false; }, 1500);
            } catch (e) {
                // Clipboard blocked (non-secure context) -- silently ignore;
                // the path stays visible and selectable in the header.
            }
        },
    };
}
