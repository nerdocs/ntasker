// Task-context plugin: picker + attachment chips, merged into tracker().
//
// Registered as a mixin factory on window.ntaskerPlugins (see index.html
// and the end of tracker() in app.js). The factory gets the base component
// so it can seed form.context before Alpine makes the state reactive.
//
// Optional integration with the workspace plugin: when its mixin is present
// (this.ws inventory, this.loadWorkspace(), this.wsShowContext()), the picker
// gains the Team / Skills / Knowledge / Documents tabs and chips open in the
// workspace viewer. Without it, chips open the entry on the desktop.

(function () {
    'use strict';

    // Icon per context kind (what an attachment points at).
    const CONTEXT_ICONS = {
        member: 'ti-user',
        skill: 'ti-puzzle',
        note: 'ti-book',
        doc: 'ti-file-text',
        file: 'ti-file',
        folder: 'ti-folder',
        mcp: 'ti-plug-connected',
    };

    // Pull the server's `detail` out of a failed response; falls back to the
    // given already-translated message.
    async function errorDetail(response, fallback) {
        try {
            const body = await response.json();
            if (body && typeof body.detail === 'string') return body.detail;
        } catch (e) {
            // Non-JSON body -- use the fallback.
        }
        return fallback;
    }

    // Case-insensitive substring match across the given fields.
    function filterItems(items, query, fields) {
        const q = (query || '').trim().toLowerCase();
        if (!q) return items || [];
        return (items || []).filter(item => fields.some(f => String(item[f] || '').toLowerCase().includes(q)));
    }

    function t(key, params) {
        let s = (window.__i18n && window.__i18n[key]) || key;
        if (params) for (const [k, v] of Object.entries(params)) s = s.replace(new RegExp('\\{' + k + '\\}', 'g'), v);
        return s;
    }

    const freshPicker = target => ({
        open: true, target: target || null, tab: 'files', q: '', note: '', busy: false, path: '', picking: false,
    });

    window.ntaskerPlugins.push(function (base) {
        // Entries to attach on create: [{kind, path, label, note}] -- sent
        // with POST /api/tasks, not attached one by one.
        base.form.context = [];

        return {
            // Picker state. `target` is the task the picked entry attaches to;
            // null = draft mode, picks collect in form.context until createTask
            // sends them along with the new task.
            picker: { open: false, target: null, tab: 'files', q: '', note: '', busy: false, path: '', picking: false },
            // Can the server open a native file dialog on this desktop? null =
            // not asked yet; the buttons stay hidden until it says yes.
            pickerNative: null,
            // MCP servers from ~/.claude.json (GET /api/context/mcp).
            mcpServers: [],

            // ---- core hooks ----
            pluginResetForm() { this.form.context = []; },
            pluginCreatePayload(payload) {
                payload.context = this.form.context.map(({ kind, path, label, note }) => ({ kind, path, label, note }));
            },
            pluginStartEdit(editing, task) {
                // Attachments are written straight through to the server (not
                // part of the PATCH payload), but the array is still cloned so
                // the list row behind the modal is not mutated before refresh.
                editing.context = (task.context || []).map(c => ({ ...c }));
            },
            pluginModalOpen() {
                return this.picker.open || !!(this.wsViewer && this.wsViewer.open);
            },

            // ---- picker ----
            openPicker(task) {
                this.picker = freshPicker(task);
                if (typeof this.loadWorkspace === 'function') this.loadWorkspace();
                this.loadPickerNative();
                this.loadMcpServers();
            },

            // "Files" is always there -- it needs no workspace directory, just
            // this machine. The workspace tabs only appear once the workspace
            // plugin reports their directory, so a fresh install is not a row
            // of empty lists. MCP lists whatever ~/.claude.json declares.
            get pickerTabs() {
                const ws = this.ws;
                const has = k => !!(ws && ws[k] && ws[k].exists);
                const tabs = [{ id: 'files', label: 'ctx_files', icon: 'ti-file' }];
                if (has('team')) tabs.push({ id: 'team', label: 'ws_team', icon: 'ti-users' });
                if (has('skills')) tabs.push({ id: 'skills', label: 'ws_skills', icon: 'ti-puzzle' });
                if (has('wiki')) tabs.push({ id: 'wiki', label: 'ws_knowledge', icon: 'ti-book' });
                if (has('docs')) tabs.push({ id: 'docs', label: 'ws_documents', icon: 'ti-files' });
                if (this.mcpServers.length) tabs.push({ id: 'mcp', label: 'ctx_mcp', icon: 'ti-plug-connected' });
                return tabs;
            },

            async loadPickerNative() {
                if (this.pickerNative !== null) return;
                try {
                    const res = await fetch('/api/fs/pick');
                    this.pickerNative = res.ok ? !!(await res.json()).available : false;
                } catch (e) {
                    this.pickerNative = false;
                }
            },

            async loadMcpServers() {
                try {
                    const res = await fetch('/api/context/mcp');
                    if (res.ok) this.mcpServers = await res.json();
                } catch (e) {
                    // Leave the list as it was.
                }
            },

            // Chip icon: a folder attachment shows as a folder, everything
            // else by its kind.
            contextChipIcon(c) {
                const kind = c && c.is_dir ? 'folder' : (c && c.kind);
                return CONTEXT_ICONS[kind] || 'ti-paperclip';
            },

            // Attach every non-empty line of the path box. Each path is
            // resolved server-side first so a typo shows up now, not at
            // Create time (draft mode sends nothing until then).
            async attachPaths(text) {
                const lines = String(text || '').split(/\r?\n/).map(l => l.trim()).filter(Boolean);
                if (!lines.length || this.picker.busy) return;
                this.picker.busy = true;
                // Lines that did not resolve stay in the box for a second look.
                const failed = [];
                try {
                    for (const raw of lines) {
                        const res = await fetch('/api/fs/resolve?path=' + encodeURIComponent(raw));
                        if (!res.ok) {
                            this.showToast(`${raw}: ${await errorDetail(res, t('ctx_attach_failed'))}`, 'danger');
                            failed.push(raw);
                            continue;
                        }
                        const info = await res.json();
                        this.picker.busy = false;   // attachContext has its own guard
                        await this.attachContext({ kind: 'file', path: info.path, label: info.name, is_dir: info.is_dir });
                        this.picker.busy = true;
                    }
                } finally {
                    this.picker.busy = false;
                }
                this.picker.path = failed.join('\n');
            },

            // Native OS dialog via the server; blocks until the user picks.
            async pickNative(folder) {
                if (this.picker.picking) return;
                this.picker.picking = true;
                try {
                    const res = await fetch('/api/fs/pick', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ folder: !!folder }),
                    });
                    if (!res.ok) {
                        this.showToast(await errorDetail(res, t('ctx_file_pick_failed')), 'danger');
                        if (res.status === 501) this.pickerNative = false;
                        return;
                    }
                    const paths = (await res.json()).paths || [];
                    if (paths.length) await this.attachPaths(paths.join('\n'));
                } catch (e) {
                    this.showToast(t('ctx_file_pick_failed'), 'danger');
                } finally {
                    this.picker.picking = false;
                }
            },

            get pickerItems() {
                const q = this.picker.q;
                if (this.picker.tab === 'files') return [];
                if (this.picker.tab === 'mcp') {
                    return filterItems(this.mcpServers, q, ['name', 'transport']).map(s => ({
                        kind: 'mcp',
                        label: s.name,
                        sub: s.transport + (s.runtime_ok ? '' : ' -- ' + t('ctx_mcp_runtime_missing')),
                        path: 'mcp://' + s.name,
                    }));
                }
                const ws = this.ws;
                if (!ws) return [];
                if (this.picker.tab === 'team') {
                    return filterItems(ws.team.members, q, ['name', 'role', 'title'])
                        .map(m => ({ kind: 'member', label: m.name, sub: m.role, path: m.path }));
                }
                if (this.picker.tab === 'skills') {
                    return filterItems(ws.skills.skills, q, ['name', 'description'])
                        .map(s => ({ kind: 'skill', label: s.name, sub: s.description, path: s.path }));
                }
                if (this.picker.tab === 'docs') {
                    return filterItems(ws.docs.docs, q, ['stem', 'name'])
                        .map(d => ({ kind: 'doc', label: d.stem, sub: d.name, path: d.path }));
                }
                // Knowledge base: areas and root index notes. Individual notes
                // are reached by browsing -- listing hundreds in a picker would
                // be a worse search than the one Obsidian already has.
                const areas = filterItems(ws.wiki.areas, q, ['name'])
                    .map(a => ({ kind: 'note', label: a.name, sub: t('ws_notes', { n: a.notes }), path: a.path }));
                const indexes = filterItems(ws.wiki.indexes, q, ['name'])
                    .map(i => ({ kind: 'note', label: i.name, sub: '', path: i.path }));
                return [...areas, ...indexes];
            },

            // Already attached to the picker's task (or, in draft mode, already
            // collected in the form)? Drives the checkmark.
            isAttached(path) {
                const list = this.picker.target ? (this.picker.target.context || []) : this.form.context;
                return list.some(c => c.path === path);
            },

            // A picker row toggles: the same click that attached an entry
            // detaches it again (by path -- the picker knows no entry ids).
            async toggleContext(item) {
                const task = this.picker.target;
                const list = task ? (task.context || []) : this.form.context;
                const attached = list.find(c => c.path === item.path);
                if (!attached) return this.attachContext(item);
                if (!task) {
                    this.form.context = this.form.context.filter(c => c.path !== item.path);
                    return;
                }
                if (this.picker.busy) return;
                this.picker.busy = true;
                try {
                    await this.detachContext(task, attached);
                } finally {
                    this.picker.busy = false;
                }
            },

            async attachContext(item) {
                const task = this.picker.target;
                // Draft mode: no task exists yet -- collect locally, createTask
                // sends the batch. Re-picking the same entry updates its note.
                if (!task) {
                    this.form.context = [
                        ...this.form.context.filter(c => c.path !== item.path),
                        { kind: item.kind, path: item.path, label: item.label, note: this.picker.note, is_dir: !!item.is_dir },
                    ];
                    this.picker.note = '';
                    return;
                }
                if (this.picker.busy) return;
                this.picker.busy = true;
                try {
                    const res = await fetch(`/api/tasks/${task.id}/context`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ kind: item.kind, path: item.path, label: item.label, note: this.picker.note }),
                    });
                    if (!res.ok) {
                        this.showToast(await errorDetail(res, t('ctx_attach_failed')), 'danger');
                        return;
                    }
                    const entry = await res.json();
                    // Patch the open task object in place: the edit modal holds
                    // a clone, so refreshAll() would not reach it.
                    task.context = [...(task.context || []).filter(c => c.path !== entry.path), entry];
                    this.picker.note = '';
                    await this.refreshAll();
                } finally {
                    this.picker.busy = false;
                }
            },

            async detachContext(task, entry) {
                const res = await fetch(`/api/tasks/${task.id}/context/${entry.id}`, { method: 'DELETE' });
                if (!res.ok && res.status !== 404) {
                    this.showToast(t('ctx_detach_failed'), 'danger');
                    return;
                }
                task.context = (task.context || []).filter(c => c.id !== entry.id);
                await this.refreshAll();
            },

            // Open an attachment. With the workspace plugin, its viewer shows
            // the file; otherwise the desktop's default application does.
            async openContext(c) {
                if (typeof this.wsShowContext === 'function') return this.wsShowContext(c);
                if (c.kind === 'mcp') {
                    this.showToast(t('ctx_mcp_hint', { name: c.label }), 'info');
                    return;
                }
                const taskId = c.task_id || (this.editing && this.editing.id);
                if (!c.id || !taskId) {
                    this.showToast(t('ctx_preview_after_create'), 'info');
                    return;
                }
                const res = await fetch(`/api/tasks/${taskId}/context/${c.id}/reveal`, { method: 'POST' });
                if (!res.ok) this.showToast(await errorDetail(res, t('ctx_open_failed')), 'danger');
            },
        };
    });
})();
