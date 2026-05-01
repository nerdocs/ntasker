/* ntasker -- Alpine state. */

// localStorage keys (namespaced under 'ntasker.*').
// NOTE: legacy keys from the pre-1.0.0 'nerdocs-tracker' package name are
// still read once on first boot and silently migrated; see
// migrateLegacyLocalStorage(). Kept in place so existing installs keep
// their filter selections across the rename.
const LS_KEY_PROJECT_FILTER = 'ntasker.projectFilter';
const LS_KEY_TAG_FILTER = 'ntasker.tagFilter';
const LS_KEY_PHASE_FILTER = 'ntasker.phaseFilter';
const LS_KEY_PRIORITY_FILTER = 'ntasker.priorityFilter';
const LS_KEY_THEME = 'ntasker.theme';

// Legacy keys used pre-1.0. Migrated to the ntasker.* namespace once.
const LEGACY_KEYS = {
    'nerdocs.tracker.projectFilter': LS_KEY_PROJECT_FILTER,
    'nerdocs.tracker.tagFilter': LS_KEY_TAG_FILTER,
    'nerdocs.tracker.phaseFilter': LS_KEY_PHASE_FILTER,
    'nerdocs.tracker.priorityFilter': LS_KEY_PRIORITY_FILTER,
    'tracker.theme': LS_KEY_THEME,
};

function migrateLegacyLocalStorage() {
    for (const [old, current] of Object.entries(LEGACY_KEYS)) {
        if (localStorage.getItem(current) === null) {
            const v = localStorage.getItem(old);
            if (v !== null) localStorage.setItem(current, v);
        }
        // Keep the legacy key in place for one release for safety; harmless dead weight.
    }
}
migrateLegacyLocalStorage();

// Sentinel for cross-project tasks (matches PROJECT_NONE_SENTINEL in app.py).
const PROJECT_NONE = '__none__';

// Valid phase values (matches PHASE_ORDER / PHASE_VALID in app.py).
// Used to silently drop stale entries from localStorage.
const PHASE_VALUES = ['wip', 'planned', 'later', '__none__'];

// Valid priority values (matches PRIORITY_ORDER / PRIORITY_VALID in app.py).
const PRIORITY_VALUES = ['critical', 'high', 'normal', 'low'];

function tracker() {
    return {
        // Sidebar feeds.
        // projects/tags: [{name, open_count}]; phases/priorities: [{value, label, open_count}].
        projects: [],
        tags: [],
        phases: [],
        priorities: [],
        tasks: [],
        tab: 'open',                 // 'open' | 'done' | 'archive'
        theme: localStorage.getItem(LS_KEY_THEME) || 'light',

        // Multi-value project filter. Empty list = no filter (all tasks).
        // Special value '__none__' = include cross-project tasks (project IS NULL).
        projectFilter: [],

        // Multi-value tag filter (OR-combined). Empty list = no filter.
        tagFilter: [],

        // Multi-value phase filter (OR-combined). Empty list = no filter.
        // Special value '__none__' = include tasks with phase IS NULL.
        phaseFilter: [],

        // Multi-value priority filter (OR-combined). Empty list = no filter.
        // priority is NOT NULL in the schema -- no __none__ sentinel.
        priorityFilter: [],

        filter: {
            search: '',
        },
        form: {
            project: '',
            title: '',
            description: '',
            phase: '',
            priority: 'normal',
            tags: [],          // committed tag list (lowercase strings)
            tagInput: '',      // current text in the tag-input
        },
        editing: null,               // task object or null
        counts: { open: 0, done: 0, archive: 0 },

        // True when the server reports `X-Settings-Missing: projects_dir`
        // on /api/projects -- shows the configuration banner.
        projectsDirMissing: false,

        async init() {
            this.applyTheme();
            this.restoreProjectFilter();
            this.restoreTagFilter();
            this.restorePhaseFilter();
            this.restorePriorityFilter();
            await Promise.all([
                this.loadProjects(),
                this.loadTags(),
                this.loadPhases(),
                this.loadPriorities(),
            ]);
            // After loading projects/tags, drop stale entries silently.
            this.pruneStaleProjectFilter();
            this.pruneStaleTagFilter();
            // Phase / priority filters are validated against fixed value lists at restore time.
            await this.loadTasks();
        },

        // Convenience: just the project names (for <select> in forms).
        // Excludes the __none__ sentinel.
        get projectNames() {
            return this.projects
                .filter(p => p.name !== PROJECT_NONE)
                .map(p => p.name);
        },

        // ---- Theme ----
        applyTheme() {
            document.documentElement.setAttribute('data-bs-theme', this.theme);
        },
        toggleTheme() {
            this.theme = this.theme === 'dark' ? 'light' : 'dark';
            localStorage.setItem(LS_KEY_THEME, this.theme);
            this.applyTheme();
        },

        // ---- Project filter ----
        restoreProjectFilter() {
            try {
                const raw = localStorage.getItem(LS_KEY_PROJECT_FILTER);
                if (!raw) return;
                const parsed = JSON.parse(raw);
                if (Array.isArray(parsed)) {
                    this.projectFilter = parsed.filter(v => typeof v === 'string');
                }
            } catch {
                this.projectFilter = [];
            }
        },

        persistProjectFilter() {
            localStorage.setItem(LS_KEY_PROJECT_FILTER, JSON.stringify(this.projectFilter));
        },

        pruneStaleProjectFilter() {
            // Drop project entries that no longer exist as projects.
            const valid = new Set(this.projects.map(p => p.name));
            const before = this.projectFilter.length;
            this.projectFilter = this.projectFilter.filter(v => valid.has(v));
            if (this.projectFilter.length !== before) this.persistProjectFilter();
        },

        // Toggle one project (or the cross-project sentinel) in the filter list.
        toggleProject(name) {
            const idx = this.projectFilter.indexOf(name);
            if (idx >= 0) this.projectFilter.splice(idx, 1);
            else this.projectFilter.push(name);
            this.persistProjectFilter();
            this.loadTasks();
            this.loadCounts();
        },

        // True iff every project (incl. '__none__') is currently active.
        get allProjectsActive() {
            return this.projects.length > 0 &&
                this.projectFilter.length === this.projects.length;
        },

        // Toggle: if all active, clear; else select all (incl. '__none__').
        toggleAllProjects() {
            if (this.allProjectsActive) {
                this.projectFilter = [];
            } else {
                this.projectFilter = this.projects.map(p => p.name);
            }
            this.persistProjectFilter();
            this.loadTasks();
            this.loadCounts();
        },

        // ---- Tag filter ----
        restoreTagFilter() {
            try {
                const raw = localStorage.getItem(LS_KEY_TAG_FILTER);
                if (!raw) return;
                const parsed = JSON.parse(raw);
                if (Array.isArray(parsed)) {
                    this.tagFilter = parsed.filter(v => typeof v === 'string');
                }
            } catch {
                this.tagFilter = [];
            }
        },

        persistTagFilter() {
            localStorage.setItem(LS_KEY_TAG_FILTER, JSON.stringify(this.tagFilter));
        },

        pruneStaleTagFilter() {
            const valid = new Set(this.tags.map(t => t.name));
            const before = this.tagFilter.length;
            this.tagFilter = this.tagFilter.filter(v => valid.has(v));
            if (this.tagFilter.length !== before) this.persistTagFilter();
        },

        toggleTag(name) {
            const norm = name.toLowerCase();
            const idx = this.tagFilter.indexOf(norm);
            if (idx >= 0) this.tagFilter.splice(idx, 1);
            else this.tagFilter.push(norm);
            this.persistTagFilter();
            this.loadTasks();
            this.loadCounts();
        },

        clearTagFilter() {
            this.tagFilter = [];
            this.persistTagFilter();
            this.loadTasks();
            this.loadCounts();
        },

        // ---- Phase filter ----
        restorePhaseFilter() {
            try {
                const raw = localStorage.getItem(LS_KEY_PHASE_FILTER);
                if (!raw) return;
                const parsed = JSON.parse(raw);
                if (Array.isArray(parsed)) {
                    // Drop any value not in the fixed PHASE_VALUES set.
                    this.phaseFilter = parsed.filter(v => PHASE_VALUES.includes(v));
                }
            } catch {
                this.phaseFilter = [];
            }
        },

        persistPhaseFilter() {
            localStorage.setItem(LS_KEY_PHASE_FILTER, JSON.stringify(this.phaseFilter));
        },

        // Multi-value toggle. Same shape as toggleTag/toggleProject.
        togglePhase(value) {
            const idx = this.phaseFilter.indexOf(value);
            if (idx >= 0) this.phaseFilter.splice(idx, 1);
            else this.phaseFilter.push(value);
            this.persistPhaseFilter();
            this.loadTasks();
            this.loadCounts();
        },

        clearPhaseFilter() {
            this.phaseFilter = [];
            this.persistPhaseFilter();
            this.loadTasks();
            this.loadCounts();
        },

        // ---- Priority filter ----
        restorePriorityFilter() {
            try {
                const raw = localStorage.getItem(LS_KEY_PRIORITY_FILTER);
                if (!raw) return;
                const parsed = JSON.parse(raw);
                if (Array.isArray(parsed)) {
                    // Drop any value not in the fixed PRIORITY_VALUES set.
                    this.priorityFilter = parsed.filter(v => PRIORITY_VALUES.includes(v));
                }
            } catch {
                this.priorityFilter = [];
            }
        },

        persistPriorityFilter() {
            localStorage.setItem(LS_KEY_PRIORITY_FILTER, JSON.stringify(this.priorityFilter));
        },

        togglePriority(value) {
            const idx = this.priorityFilter.indexOf(value);
            if (idx >= 0) this.priorityFilter.splice(idx, 1);
            else this.priorityFilter.push(value);
            this.persistPriorityFilter();
            this.loadTasks();
            this.loadCounts();
        },

        clearPriorityFilter() {
            this.priorityFilter = [];
            this.persistPriorityFilter();
            this.loadTasks();
            this.loadCounts();
        },

        // ---- Tabs ----
        setTab(tab) {
            this.tab = tab;
            this.loadTasks();
        },

        // ---- Sidebar data ----
        async loadProjects() {
            const r = await fetch('/api/projects');
            // The server flags an unconfigured projects_dir via response header
            // instead of changing the response shape -- the list always has at
            // least the __none__ sentinel. UI shows a configuration banner.
            this.projectsDirMissing = (r.headers.get('X-Settings-Missing') || '')
                .split(',').map(s => s.trim()).includes('projects_dir');
            this.projects = await r.json();
        },

        async loadTags() {
            const r = await fetch('/api/tags');
            this.tags = await r.json();
        },

        async loadPhases() {
            const r = await fetch('/api/phases');
            this.phases = await r.json();
        },

        async loadPriorities() {
            const r = await fetch('/api/priorities');
            this.priorities = await r.json();
        },

        // ---- Tasks ----
        // Build URLSearchParams shared by /api/tasks and /api/stats.
        // Repeats the `project`, `tag`, `phase`, `priority` keys for FastAPI list[str].
        _buildFilterParams() {
            const params = new URLSearchParams();
            for (const v of this.projectFilter) params.append('project', v);
            for (const v of this.tagFilter) params.append('tag', v);
            for (const v of this.phaseFilter) params.append('phase', v);
            for (const v of this.priorityFilter) params.append('priority', v);
            if (this.filter.search) params.set('search', this.filter.search);
            return params;
        },

        async loadTasks() {
            const params = this._buildFilterParams();
            if (this.tab === 'open') {
                params.set('status', 'open');
                params.set('archived', 'false');
            } else if (this.tab === 'done') {
                params.set('status', 'done');
                params.set('archived', 'false');
            } else if (this.tab === 'archive') {
                params.set('archived', 'true');
            }

            const r = await fetch('/api/tasks?' + params.toString());
            this.tasks = await r.json();
            this.tasks.forEach(t => { t._expanded = false; });

            await this.loadCounts();
        },

        async loadCounts() {
            // One roundtrip via /api/stats; honors current project + tag + phase + search filter.
            const params = this._buildFilterParams();
            const r = await fetch('/api/stats?' + params.toString());
            this.counts = await r.json();
        },

        // ---- Task CRUD ----
        async createTask() {
            // Commit any pending tag input before submit.
            this.commitTagInput('form');
            const body = {
                project: this.form.project || null,
                title: this.form.title.trim(),
                description: this.form.description.trim() || null,
                phase: this.form.phase || null,
                priority: this.form.priority || 'normal',
                tags: this.form.tags,
            };
            const r = await fetch('/api/tasks', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
            if (!r.ok) {
                this.showToast('Anlegen fehlgeschlagen.', 'danger');
                return;
            }
            this.form.title = '';
            this.form.description = '';
            this.form.phase = '';
            this.form.priority = 'normal';
            this.form.tags = [];
            this.form.tagInput = '';
            // Keep project selection for rapid same-project entry.
            await this.refreshAll();
        },

        async toggleStatus(task) {
            const newStatus = task.status === 'done' ? 'open' : 'done';
            await this.patch(task.id, { status: newStatus });
            await this.refreshAll();
        },

        async archiveTask(task) {
            await this.patch(task.id, { archived: true });
            await this.refreshAll();
        },

        async unarchiveTask(task) {
            await this.patch(task.id, { archived: false });
            await this.refreshAll();
        },

        async deleteTask(task) {
            // Defensive guard: hard-deletion is only valid for archived tasks.
            // The button is conditionally rendered for archived rows only, but
            // we re-check here in case a recycled DOM node ever fires the
            // handler from a non-archived row.
            if (!task.archived) {
                this.showToast('Nur archivierte Aufgaben können gelöscht werden.', 'danger');
                return;
            }
            if (!confirm(`"${task.title}" endgültig löschen?`)) return;
            const r = await fetch(`/api/tasks/${task.id}`, { method: 'DELETE' });
            if (!r.ok) {
                this.showToast('Löschen fehlgeschlagen.', 'danger');
                return;
            }
            await this.refreshAll();
        },

        startEdit(task) {
            // Clone (incl. tags array) so cancel doesn't leak edits into the row.
            this.editing = {
                ...task,
                tags: [...(task.tags || [])],
                _tagInput: '',
            };
        },

        async saveEdit() {
            // Commit any pending tag input before save.
            this.commitTagInput('edit');
            const t = this.editing;
            const body = {
                title: t.title,
                description: t.description,
                project: t.project || null,
                phase: t.phase || null,
                priority: t.priority || 'normal',
                tags: t.tags,
            };
            const r = await fetch(`/api/tasks/${t.id}`, {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
            if (!r.ok) {
                this.showToast('Speichern fehlgeschlagen.', 'danger');
                return;
            }
            this.editing = null;
            await this.refreshAll();
        },

        async patch(id, body) {
            const r = await fetch(`/api/tasks/${id}`, {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
            if (!r.ok) {
                this.showToast('Update fehlgeschlagen.', 'danger');
            }
            return r;
        },

        // After any write, refresh tasks + sidebar counts (projects, tags, phases, priorities).
        async refreshAll() {
            await Promise.all([
                this.loadProjects(),
                this.loadTags(),
                this.loadPhases(),
                this.loadPriorities(),
            ]);
            this.pruneStaleProjectFilter();
            this.pruneStaleTagFilter();
            await this.loadTasks();
        },

        // ---- Tag input helpers (shared by new-task form & edit-modal) ----
        // `which` = 'form' | 'edit' selects the target list.
        _tagBucket(which) {
            return which === 'edit' ? this.editing : this.form;
        },

        _tagInputProp(which) {
            return which === 'edit' ? '_tagInput' : 'tagInput';
        },

        commitTagInput(which) {
            const bucket = this._tagBucket(which);
            if (!bucket) return;
            const key = this._tagInputProp(which);
            const raw = (bucket[key] || '').trim().toLowerCase();
            if (!raw) return;
            // Allow comma-separated batch entry: "alpha, beta" -> two tags.
            const candidates = raw.split(',').map(s => s.trim()).filter(Boolean);
            for (const c of candidates) {
                if (!bucket.tags.includes(c)) bucket.tags.push(c);
            }
            bucket[key] = '';
        },

        onTagKeydown(event, which) {
            const bucket = this._tagBucket(which);
            if (!bucket) return;
            const key = this._tagInputProp(which);
            // Comma also commits.
            if (event.key === ',') {
                event.preventDefault();
                this.commitTagInput(which);
                return;
            }
            // Backspace on empty input removes the last tag.
            if (event.key === 'Backspace' && !bucket[key]) {
                if (bucket.tags.length > 0) bucket.tags.pop();
            }
        },

        removeTagFromForm(idx) {
            this.form.tags.splice(idx, 1);
        },

        removeTagFromEditing(idx) {
            if (this.editing) this.editing.tags.splice(idx, 1);
        },

        tagSuggestions(which) {
            const bucket = this._tagBucket(which);
            if (!bucket) return [];
            const key = this._tagInputProp(which);
            const q = (bucket[key] || '').trim().toLowerCase();
            if (!q) return [];
            const present = new Set(bucket.tags);
            return this.tags
                .map(t => t.name)
                .filter(name => name.includes(q) && !present.has(name))
                .slice(0, 8);
        },

        selectSuggestion(which, name) {
            const bucket = this._tagBucket(which);
            if (!bucket) return;
            const key = this._tagInputProp(which);
            if (!bucket.tags.includes(name)) bucket.tags.push(name);
            bucket[key] = '';
        },

        // ---- ID copy + toast feedback ----
        async copyId(id) {
            const text = `#${id}`;
            try {
                await navigator.clipboard.writeText(text);
                this.showToast(`ID ${text} kopiert`, 'success');
            } catch {
                // Fallback for non-secure contexts (Clipboard API unavailable).
                const ta = document.createElement('textarea');
                ta.value = text;
                ta.style.position = 'fixed';
                ta.style.opacity = '0';
                document.body.appendChild(ta);
                ta.select();
                try {
                    document.execCommand('copy');
                    this.showToast(`ID ${text} kopiert`, 'success');
                } catch {
                    this.showToast('Kopieren fehlgeschlagen', 'danger');
                }
                document.body.removeChild(ta);
            }
        },

        // Lightweight Tabler-style toast. Self-removes after 2.5s.
        // kind: 'success' | 'danger' | 'info'
        showToast(message, kind = 'success') {
            const container = document.getElementById('toast-container');
            if (!container) return;
            const div = document.createElement('div');
            const bgMap = { danger: 'bg-danger', info: 'bg-info', success: 'bg-success' };
            const bg = bgMap[kind] || 'bg-success';
            div.className = `toast align-items-center text-white border-0 show mb-2 ${bg}`;
            div.setAttribute('role', 'status');
            div.setAttribute('aria-live', 'polite');
            div.innerHTML = `
                <div class="d-flex">
                    <div class="toast-body">${message}</div>
                    <button type="button" class="btn-close btn-close-white me-2 m-auto" aria-label="Schließen"></button>
                </div>`;
            div.querySelector('.btn-close').addEventListener('click', () => div.remove());
            container.appendChild(div);
            setTimeout(() => div.remove(), 2500);
        },

        // ---- Datetime formatting ----
        // Server stores naive UTC timestamps via SQLite's datetime('now').
        // We append 'Z' so the JS Date parses them as UTC, then render local.
        _toDate(s) {
            if (!s) return null;
            let iso = s.replace(' ', 'T');
            if (!/[zZ]|[+-]\d{2}:?\d{2}$/.test(iso)) iso += 'Z';
            return new Date(iso);
        },

        formatRelative(s) {
            const d = this._toDate(s);
            if (!d) return '';
            const rtf = new Intl.RelativeTimeFormat('de-DE', { numeric: 'auto' });
            const diffMs = d - new Date();
            const diffSec = Math.round(diffMs / 1000);
            const abs = Math.abs(diffSec);
            if (abs < 60) return rtf.format(diffSec, 'second');
            if (abs < 3600) return rtf.format(Math.round(diffSec / 60), 'minute');
            if (abs < 86400) return rtf.format(Math.round(diffSec / 3600), 'hour');
            if (abs < 86400 * 30) return rtf.format(Math.round(diffSec / 86400), 'day');
            if (abs < 86400 * 365) return rtf.format(Math.round(diffSec / (86400 * 30)), 'month');
            return rtf.format(Math.round(diffSec / (86400 * 365)), 'year');
        },

        formatAbsolute(s) {
            const d = this._toDate(s);
            if (!d) return '';
            return d.toLocaleString('de-DE', { dateStyle: 'medium', timeStyle: 'short' });
        },

        emptyHint() {
            if (this.filter.search ||
                this.projectFilter.length > 0 ||
                this.tagFilter.length > 0 ||
                this.phaseFilter.length > 0 ||
                this.priorityFilter.length > 0) {
                return 'Keine Treffer für die aktiven Filter.';
            }
            if (this.tab === 'open') return 'Alles erledigt. Oder noch nichts angelegt.';
            if (this.tab === 'done') return 'Noch nichts erledigt.';
            return 'Archiv ist leer.';
        },

        // ---- Tag cleanup (header action) ----
        // POSTs to /api/tags/cleanup, then refreshes the tag list and shows a toast.
        // Idempotent: clicking again on a clean DB just toasts "Keine ungenutzten Tags."
        async cleanupTags() {
            const r = await fetch('/api/tags/cleanup', { method: 'POST' });
            if (!r.ok) {
                this.showToast('Aufräumen fehlgeschlagen.', 'danger');
                return;
            }
            const data = await r.json();
            const removed = data.removed || 0;
            const names = Array.isArray(data.removed_names) ? data.removed_names : [];
            if (removed === 0) {
                this.showToast('Keine ungenutzten Tags.', 'info');
            } else {
                // Render at most 5 names, append "+N weitere" tail.
                const head = names.slice(0, 5).join(', ');
                const tail = names.length > 5 ? `, +${names.length - 5} weitere` : '';
                this.showToast(
                    `${removed} ungenutzte Tags entfernt: ${head}${tail}`,
                    'success'
                );
            }
            // Tag-list may have shrunk -> refresh sidebar feed and prune stale filter.
            await this.loadTags();
            this.pruneStaleTagFilter();
        },
    };
}
