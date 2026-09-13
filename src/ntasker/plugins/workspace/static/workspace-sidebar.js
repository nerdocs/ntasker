// Workspace plugin: sidebar sections + file viewer, merged into tracker().
//
// Registered as a mixin factory on window.ntaskerPlugins (see index.html
// and the end of tracker() in app.js). Provides the inventory (`ws`), the
// collapsible sidebar sections, the viewer modal (`wsViewer`) and
// wsShowContext(), which the task-context plugin calls to open an
// attachment when this plugin is present.

(function () {
    'use strict';

    const WS = window.ntaskerWs;
    // Collapse state of the workspace sidebar sections, as {key: bool}.
    const LS_KEY_WS_SECTIONS = 'ntasker.wsSections';
    // How many entries a sidebar section shows before deferring to the
    // full /workspace page. The sidebar is a jumping-off point, not a
    // file manager: 481 knowledge-base notes must never render in it.
    const WS_SIDEBAR_MAX = 12;

    const freshViewer = () => ({
        open: true, loading: true, error: '', file: null, dir: null,
        mode: 'none', html: '', rows: [], editing: false, draft: '',
    });

    window.ntaskerPlugins.push(function (base) {
        // Chain onto an init a previous mixin may already have registered.
        const prevInit = base.pluginInit;

        return {
            ws: WS.emptyInventory(),   // inventory scaffold until the first fetch
            wsLoaded: false,
            wsLoading: false,
            wsQuery: { team: '', skills: '', wiki: '', docs: '' },
            // Sections start closed so the page looks the way it always did
            // until the user goes looking; the choice is remembered.
            wsOpen: { team: false, skills: false, wiki: false, docs: false },
            wsViewer: {
                open: false, loading: false, error: '',
                file: null, mode: 'none', html: '', rows: [],
                editing: false, draft: '', saving: false,
                // Set while browsing a directory instead of showing a file.
                dir: null,
            },

            async pluginInit() {
                if (prevInit) await prevInit.call(this);
                try {
                    const saved = JSON.parse(localStorage.getItem(LS_KEY_WS_SECTIONS) || '{}');
                    for (const k of Object.keys(this.wsOpen)) if (k in saved) this.wsOpen[k] = !!saved[k];
                } catch (e) {
                    // Corrupt or unavailable storage -- keep the defaults.
                }
                // Not awaited: whether the block appears depends on the scan,
                // but the scan must never delay the task list.
                this.loadWorkspace();
            },

            toggleWsSection(key) {
                this.wsOpen[key] = !this.wsOpen[key];
                try { localStorage.setItem(LS_KEY_WS_SECTIONS, JSON.stringify(this.wsOpen)); } catch (e) { /* quota */ }
                // Expanding after the initial fetch failed (server restarting,
                // say) is a natural moment to retry.
                if (!this.wsLoaded && this.wsOpen[key]) this.loadWorkspace();
            },

            async loadWorkspace(force) {
                if (this.wsLoading || (this.wsLoaded && !force)) return;
                this.wsLoading = true;
                try {
                    const res = await fetch('/api/workspace');
                    if (res.ok) {
                        this.ws = await res.json();
                        this.wsLoaded = true;
                    }
                } catch (e) {
                    // Leave the empty scaffold -- every section then renders
                    // its "not configured" hint instead of blowing up.
                } finally {
                    this.wsLoading = false;
                }
            },

            // True when at least one workspace directory exists. Drives
            // whether the sidebar shows the workspace block at all.
            get wsConfigured() {
                return ['skills', 'wiki', 'team', 'docs'].some(k => this.ws[k] && this.ws[k].exists);
            },

            wsSlice(items, query, fields) { return WS.filterItems(items, query, fields).slice(0, WS_SIDEBAR_MAX); },
            wsMore(items, query, fields) { return Math.max(0, WS.filterItems(items, query, fields).length - WS_SIDEBAR_MAX); },
            wsIcon(kind) { return WS.iconFor(kind); },
            wsSize(bytes) { return WS.fmtSize(bytes); },
            wsEditable(file) { return WS.isEditable(file); },

            // ---- viewer ----
            async wsPreview(path) {
                Object.assign(this.wsViewer, freshViewer());
                try {
                    const res = await fetch('/api/workspace/file?path=' + encodeURIComponent(path));
                    if (!res.ok) {
                        this.wsViewer.error = await WS.errorDetail(res, WS.t('ws_preview_failed'));
                        return;
                    }
                    const file = await res.json();
                    this.wsViewer.file = file;
                    Object.assign(this.wsViewer, WS.renderFile(file));
                } catch (e) {
                    this.wsViewer.error = WS.t('ws_preview_failed');
                } finally {
                    this.wsViewer.loading = false;
                }
            },

            async wsBrowse(path) {
                Object.assign(this.wsViewer, freshViewer());
                try {
                    const res = await fetch('/api/workspace/browse?path=' + encodeURIComponent(path));
                    if (!res.ok) {
                        this.wsViewer.error = await WS.errorDetail(res, WS.t('ws_preview_failed'));
                        return;
                    }
                    this.wsViewer.dir = await res.json();
                } catch (e) {
                    this.wsViewer.error = WS.t('ws_preview_failed');
                } finally {
                    this.wsViewer.loading = false;
                }
            },

            // One click handler for both kinds of row in a directory listing.
            wsOpen(entry) {
                if (entry.directory) return this.wsBrowse(entry.path);
                if (entry.previewable) return this.wsPreview(entry.path);
                return this.wsRevealFile(entry.path);
            },

            wsToggleEdit() {
                if (this.wsViewer.editing) {
                    this.wsViewer.editing = false;
                    this.wsViewer.draft = '';
                    return;
                }
                this.wsViewer.draft = this.wsViewer.file?.text ?? '';
                this.wsViewer.editing = true;
            },

            async wsSave() {
                const file = this.wsViewer.file;
                if (!file || this.wsViewer.saving) return;
                this.wsViewer.saving = true;
                try {
                    const res = await fetch('/api/workspace/file', {
                        method: 'PUT',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ path: file.path, text: this.wsViewer.draft }),
                    });
                    if (!res.ok) {
                        this.showToast(await WS.errorDetail(res, WS.t('ws_save_failed')), 'danger');
                        return;
                    }
                    const fresh = await res.json();
                    this.wsViewer.file = fresh;
                    Object.assign(this.wsViewer, WS.renderFile(fresh));
                    this.wsViewer.editing = false;
                    this.wsViewer.draft = '';
                    this.showToast(WS.t('ws_saved'), 'success');
                } finally {
                    this.wsViewer.saving = false;
                }
            },

            async wsRename(path, currentName) {
                const next = prompt(WS.t('ws_rename_prompt'), currentName);
                if (next === null || next.trim() === currentName) return;
                const res = await fetch('/api/workspace/rename', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ path, name: next }),
                });
                if (!res.ok) {
                    this.showToast(await WS.errorDetail(res, WS.t('ws_rename_failed')), 'danger');
                    return;
                }
                const moved = await res.json();
                await this.loadWorkspace(true);
                if (this.wsViewer.dir) await this.wsBrowse(this.wsViewer.dir.path);
                else await this.wsPreview(moved.path);
            },

            async wsDelete(path, name) {
                if (!confirm(WS.t('ws_confirm_delete', { name }))) return;
                const res = await fetch('/api/workspace/delete', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ path }),
                });
                if (!res.ok) {
                    this.showToast(await WS.errorDetail(res, WS.t('ws_delete_failed')), 'danger');
                    return;
                }
                const result = await res.json();
                // Name the trash the file landed in -- "deleted" from a browser
                // is alarming enough that the user deserves to know it is
                // recoverable, and from where.
                this.showToast(
                    result.method === 'os' ? WS.t('ws_trashed_os', { name: result.name }) : WS.t('ws_trashed_folder', { name: result.name }),
                    'success',
                );
                await this.loadWorkspace(true);
                if (this.wsViewer.dir) await this.wsBrowse(this.wsViewer.dir.path);
                else this.wsViewer.open = false;
            },

            async wsRevealFile(path) {
                // A "file" attachment lives outside the workspace roots; its
                // open goes through the attachment row instead.
                const ext = this.wsViewer.file && this.wsViewer.file.path === path && this.wsViewer.file.external;
                const res = ext
                    ? await fetch(`/api/tasks/${ext.taskId}/context/${ext.contextId}/reveal`, { method: 'POST' })
                    : await fetch('/api/workspace/reveal', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ path }),
                    });
                if (!res.ok) this.showToast(await WS.errorDetail(res, WS.t('ws_open_failed')), 'danger');
            },

            async wsCreateNote(parent) {
                const name = prompt(WS.t('ws_new_note_prompt'), '');
                if (!name || !name.trim()) return;
                const res = await fetch('/api/workspace/entry', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ parent, name }),
                });
                if (!res.ok) {
                    this.showToast(await WS.errorDetail(res, WS.t('ws_create_failed')), 'danger');
                    return;
                }
                const created = await res.json();
                await this.loadWorkspace(true);
                await this.wsPreview(created.path);
                this.wsToggleEdit();
            },

            // ---- task-context integration ----
            //
            // Open an attachment chip: workspace kinds go to the viewer, a
            // "file" attachment through its attachment row (outside the
            // roots), an MCP server as a short fact sheet.
            wsShowContext(c) {
                if (c.kind === 'mcp') return this.wsMcpPreview(c.path);
                if (c.kind === 'file') return this.wsContextFilePreview(c);
                return this.wsPreview(c.path);
            },

            async wsContextFilePreview(c) {
                const taskId = c.task_id || (this.editing && this.editing.id);
                if (!c.id || !taskId) {
                    this.showToast(WS.t('ws_file_preview_after_create'), 'info');
                    return;
                }
                Object.assign(this.wsViewer, freshViewer());
                try {
                    const res = await fetch(`/api/tasks/${taskId}/context/${c.id}/file`);
                    if (!res.ok) {
                        this.wsViewer.error = await WS.errorDetail(res, WS.t('ws_preview_failed'));
                        return;
                    }
                    const file = await res.json();
                    // `external` keeps the viewer read-only (no rename/delete/
                    // edit -- those endpoints are root-confined) and routes
                    // "open" through the attachment endpoint.
                    file.external = { taskId, contextId: c.id };
                    this.wsViewer.file = file;
                    Object.assign(this.wsViewer, WS.renderFile(file));
                } catch (e) {
                    this.wsViewer.error = WS.t('ws_preview_failed');
                } finally {
                    this.wsViewer.loading = false;
                }
            },

            // An MCP server has no body to show -- the viewer gets a short
            // fact sheet from the inventory (never the config's env values).
            async wsMcpPreview(path) {
                const name = String(path || '').replace(/^mcp:\/\//, '');
                Object.assign(this.wsViewer, freshViewer());
                try {
                    await this.loadWorkspace();
                    const s = ((this.ws.tooling && this.ws.tooling.servers) || []).find(x => x.name === name);
                    if (!s) {
                        this.wsViewer.error = WS.t('ws_mcp_gone');
                        return;
                    }
                    const lines = [`**${WS.t('ws_mcp_transport')}:** ${s.transport}`];
                    if (s.command) lines.push(`**${WS.t('ws_mcp_command')}:** \`${s.command}\``);
                    if (s.runtime && !s.runtime_ok) lines.push(`_${WS.t('ws_mcp_runtime_missing')}: ${s.runtime}_`);
                    if (s.env && s.env.length) lines.push('**env:** ' + s.env.map(e => e.key).join(', '));
                    const file = { name, path, kind: 'markdown', suffix: '', text: lines.join('\n\n'), remote: true };
                    this.wsViewer.file = file;
                    Object.assign(this.wsViewer, WS.renderFile(file));
                } finally {
                    this.wsViewer.loading = false;
                }
            },
        };
    });
})();
