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
// View-mode + kanban Done-column collapsed flag live in localStorage so a
// user's last choice survives navigation; the server-supplied default
// (`default_view` setting) only kicks in on a fresh browser.
const LS_KEY_VIEW_MODE = 'ntasker.viewMode';
const LS_KEY_KANBAN_DONE_COLLAPSED = 'ntasker.kanbanDoneCollapsed';

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

// i18n helper -- mirrors the Alpine $i18n magic property registered in
// the template. Falls back to the key itself when no translation exists,
// so ad-hoc usage during development stays visible (and obvious).
function _i(key, params) {
    let s = (window.__i18n && window.__i18n[key]) || key;
    if (params) {
        for (const [k, v] of Object.entries(params)) {
            s = s.replace(new RegExp('\\{' + k + '\\}', 'g'), v);
        }
    }
    return s;
}

// BCP-47 locale picked up from <html lang="..."> for Intl.* APIs (date
// formatting). Defaults to 'en' if the attribute is missing.
function _locale() {
    const html = document.documentElement;
    return (html && html.getAttribute('lang')) || 'en';
}

// Sentinel for cross-project tasks (matches PROJECT_NONE_SENTINEL in app.py).
const PROJECT_NONE = '__none__';

// Valid phase values (matches PHASE_ORDER / PHASE_VALID in app.py).
// Used to silently drop stale entries from localStorage.
const PHASE_VALUES = ['planned', 'wip', 'review'];

// Valid priority values (matches PRIORITY_ORDER / PRIORITY_VALID in app.py).
const PRIORITY_VALUES = ['critical', 'high', 'normal', 'low'];

// Valid view modes. Sync with DEFAULT_VIEW_ALLOWED in settings.py and the
// `default_view` validator.
const VIEW_MODES = ['list', 'kanban'];

function tracker(serverDefaultView) {
    // Resolve initial viewMode: localStorage > server-supplied default > 'list'.
    // The server value comes from the `default_view` setting and is injected
    // into the Alpine root in index.html.
    let initialView = localStorage.getItem(LS_KEY_VIEW_MODE);
    if (!VIEW_MODES.includes(initialView)) {
        initialView = VIEW_MODES.includes(serverDefaultView) ? serverDefaultView : 'list';
    }
    return {
        // Sidebar feeds.
        // projects/tags: [{name, open_count}]; phases/priorities: [{value, label, open_count}].
        projects: [],
        tags: [],
        phases: [],
        priorities: [],
        tasks: [],
        tab: 'open',                 // 'open' | 'done' | 'archive' (list view only)
        viewMode: initialView,       // 'list' | 'kanban'
        // Done column in kanban defaults to collapsed so the workflow columns
        // get the real estate; user can expand it via the column header.
        doneCollapsed: (localStorage.getItem(LS_KEY_KANBAN_DONE_COLLAPSED) ?? '1') === '1',
        // Drag&drop state. ``draggedTaskId`` is captured on dragstart so the
        // drop handler can identify the moving task without parsing dataTransfer
        // (Firefox is picky about reading text/plain mid-drag). ``dragOverColumn``
        // drives the column-highlight CSS class.
        draggedTaskId: null,
        dragOverColumn: null,
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
            depends: [],       // committed dependencies: [{id, title, done}]
            depInput: '',      // current text in the dependency-input
        },
        // Dependency autocomplete suggestions for the currently focused
        // input (form or edit -- only one is open at a time).
        depSuggest: [],
        editing: null,               // task object or null
        counts: { open: 0, done: 0, archive: 0 },

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

        // ---- View mode (list / kanban) ----
        setViewMode(mode) {
            if (!VIEW_MODES.includes(mode)) return;
            if (this.viewMode === mode) return;
            this.viewMode = mode;
            localStorage.setItem(LS_KEY_VIEW_MODE, mode);
            // Switching views changes what we need to load: list-view honors
            // the status tab, kanban shows open + done together.
            this.loadTasks();
        },

        toggleDoneCollapsed() {
            this.doneCollapsed = !this.doneCollapsed;
            localStorage.setItem(LS_KEY_KANBAN_DONE_COLLAPSED, this.doneCollapsed ? '1' : '0');
        },

        // Static column definitions for the kanban board. ``key`` is either
        // a phase value or the literal 'done'. Labels resolve at render time
        // via $i18n. Icons use only glyphs known to be in the vendored
        // tabler-icons subset (see comments in index.html).
        get kanbanColumns() {
            return [
                {key: 'planned', label: _i('phase_planned'),    icon: 'ti-clock'},
                {key: 'wip',     label: _i('phase_wip'),        icon: 'ti-progress'},
                {key: 'review',  label: _i('phase_review'),     icon: 'ti-eye'},
                {key: 'done',    label: _i('kanban_col_done'),  icon: 'ti-check'},
            ];
        },

        // Group ``tasks`` by kanban column key. Done-column = status==='done';
        // phase columns only get status==='open' tasks (a done task in phase
        // 'wip' belongs in Done, not in WIP).
        kanbanTasksFor(colKey) {
            if (colKey === 'done') {
                return this.tasks.filter(t => t.status === 'done');
            }
            return this.tasks.filter(t => t.status === 'open' && t.phase === colKey);
        },

        // ---- Drag & Drop (kanban) ----
        onCardDragStart(event, task) {
            this.draggedTaskId = task.id;
            // dataTransfer.setData is required for Firefox to even initiate
            // the drag; the value itself is unused (we keep the id in state).
            try {
                event.dataTransfer.setData('text/plain', String(task.id));
                event.dataTransfer.effectAllowed = 'move';
            } catch {
                // Some embed contexts deny dataTransfer access; ignore.
            }
        },

        onCardDragEnd() {
            this.draggedTaskId = null;
            this.dragOverColumn = null;
        },

        onColumnDragOver(event, colKey) {
            // Required to allow the drop event to fire.
            if (event.dataTransfer) event.dataTransfer.dropEffect = 'move';
            this.dragOverColumn = colKey;
        },

        onColumnDragLeave(event, colKey) {
            // dragleave fires when crossing into a child too -- only clear
            // the highlight if we left the column for real.
            const related = event.relatedTarget;
            if (!related || !event.currentTarget.contains(related)) {
                if (this.dragOverColumn === colKey) this.dragOverColumn = null;
            }
        },

        async onColumnDrop(event, colKey) {
            const id = this.draggedTaskId;
            this.dragOverColumn = null;
            this.draggedTaskId = null;
            if (id == null) return;
            const task = this.tasks.find(t => t.id === id);
            if (!task) return;
            // Compute the patch: cross-column move = phase change (and
            // status flip when crossing Done<->open columns).
            const body = {};
            if (colKey === 'done') {
                if (task.status !== 'done') body.status = 'done';
                // Keep the task's current phase so re-opening lands it back
                // in its previous workflow column instead of "Planned".
            } else {
                if (task.status === 'done') body.status = 'open';
                if (task.phase !== colKey) body.phase = colKey;
            }
            if (Object.keys(body).length === 0) return; // dropped on same column
            const r = await this.patch(id, body);
            if (r && r.ok) await this.refreshAll();
        },

        // ---- Sidebar data ----
        async loadProjects() {
            // Projects are derived from tasks since v2.0: the response is a
            // plain list with __none__ first, then every name currently
            // referenced by at least one task.
            const r = await fetch('/api/projects');
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
            // Kanban view always shows non-archived tasks (open + done) so
            // the Done column has content; status tabs are irrelevant here.
            if (this.viewMode === 'kanban') {
                params.set('archived', 'false');
            } else if (this.tab === 'open') {
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
                depends: this.form.depends.map(d => d.id),
            };
            const r = await fetch('/api/tasks', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
            if (!r.ok) {
                this.showToast(await this._errorDetail(r, 'create_failed'), 'danger');
                return;
            }
            const created = await r.json();
            this.form.title = '';
            this.form.description = '';
            this.form.phase = '';
            this.form.priority = 'normal';
            this.form.tags = [];
            this.form.tagInput = '';
            this.form.depends = [];
            this.form.depInput = '';
            // Keep project selection for rapid same-project entry.
            await this.refreshAll();
            // The task is saved, but an active filter (project/phase/tag/
            // priority/status tab) may exclude it from the refreshed list --
            // without feedback that looks like a silent failure. Confirm the
            // save, and flag it when the new task is hidden by a filter.
            const visible = this.tasks.some(t => t.id === created.id);
            this.showToast(
                visible
                    ? _i('create_ok', {id: created.id})
                    : _i('create_ok_hidden', {id: created.id}),
                visible ? 'success' : 'info',
            );
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
                this.showToast(_i('delete_only_archived'), 'danger');
                return;
            }
            if (!confirm(_i('confirm_delete', {title: task.title}))) return;
            const r = await fetch(`/api/tasks/${task.id}`, { method: 'DELETE' });
            if (!r.ok) {
                this.showToast(_i('delete_failed'), 'danger');
                return;
            }
            await this.refreshAll();
        },

        // Modal-side delete: archived-or-not, always confirms with the title.
        // The list-view delete button stays archived-only (safety against
        // accidental clicks). The modal is a deliberate user action, so we
        // let the user delete from any state.
        async deleteFromEdit() {
            if (!this.editing) return;
            const t = this.editing;
            if (!confirm(_i('confirm_delete', {title: t.title}))) return;
            const r = await fetch(`/api/tasks/${t.id}`, { method: 'DELETE' });
            if (!r.ok) {
                this.showToast(_i('delete_failed'), 'danger');
                return;
            }
            this.editing = null;
            await this.refreshAll();
        },

        startEdit(task) {
            // Clone (incl. tags + depends arrays) so cancel doesn't leak edits into the row.
            this.editing = {
                ...task,
                tags: [...(task.tags || [])],
                _tagInput: '',
                depends: (task.depends || []).map(d => ({ ...d })),
                _depInput: '',
            };
            this.depSuggest = [];
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
                depends: (t.depends || []).map(d => d.id),
            };
            const r = await fetch(`/api/tasks/${t.id}`, {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
            if (!r.ok) {
                this.showToast(await this._errorDetail(r, 'save_failed'), 'danger');
                return;
            }
            this.editing = null;
            await this.refreshAll();
        },

        // Pull a server-supplied error message out of a failed response,
        // falling back to a generic localized key. Used so dependency
        // validation errors (cycle / missing / self) surface verbatim.
        async _errorDetail(response, fallbackKey) {
            try {
                const body = await response.json();
                if (body && typeof body.detail === 'string') return body.detail;
            } catch (_e) { /* not JSON */ }
            return _i(fallbackKey);
        },

        async patch(id, body) {
            const r = await fetch(`/api/tasks/${id}`, {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
            if (!r.ok) {
                this.showToast(_i('update_failed'), 'danger');
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

        // ---- Dependency input helpers (shared by new-task form & edit-modal) ----
        // `which` = 'form' | 'edit'. Deps are stored as [{id, title, done}].
        _depBucket(which) {
            return which === 'edit' ? this.editing : this.form;
        },

        _depInputProp(which) {
            return which === 'edit' ? '_depInput' : 'depInput';
        },

        // Autocomplete by title or #id. Queries the unfiltered task list via
        // /api/tasks?search= so dependencies can point anywhere, not just the
        // currently filtered/visible rows. Excludes self + already-added.
        async loadDepSuggestions(which) {
            const bucket = this._depBucket(which);
            if (!bucket) { this.depSuggest = []; return; }
            const q = (bucket[this._depInputProp(which)] || '').trim();
            if (!q) { this.depSuggest = []; return; }
            const r = await fetch('/api/tasks?search=' + encodeURIComponent(q));
            if (!r.ok) { this.depSuggest = []; return; }
            const rows = await r.json();
            const ownId = which === 'edit' && this.editing ? this.editing.id : null;
            const present = new Set((bucket.depends || []).map(d => d.id));
            this.depSuggest = rows
                .filter(t => t.id !== ownId && !present.has(t.id))
                .slice(0, 8);
        },

        addDep(which, task) {
            const bucket = this._depBucket(which);
            if (!bucket) return;
            if (!(bucket.depends || []).some(d => d.id === task.id)) {
                bucket.depends.push({ id: task.id, title: task.title, done: task.status === 'done' });
            }
            bucket[this._depInputProp(which)] = '';
            this.depSuggest = [];
        },

        removeDepFromForm(idx) {
            this.form.depends.splice(idx, 1);
        },

        removeDepFromEditing(idx) {
            if (this.editing) this.editing.depends.splice(idx, 1);
        },

        // A task is blocked while any dependency is not yet done.
        isBlocked(task) {
            return (task.depends || []).some(d => !d.done);
        },

        // ---- ID copy + toast feedback ----
        // Copies the ready-to-paste Claude Code slash-command "/task #<id>"
        // so the user can hand the task off to an agent in one paste.
        // The slash-command name is hard-coded; users who installed the
        // assets with --command-name=foo need to patch this string.
        async copyId(id) {
            const text = `/task #${id}`;
            try {
                await navigator.clipboard.writeText(text);
                this.showToast(_i('copied', {text: text}), 'success');
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
                    this.showToast(_i('copied', {text: text}), 'success');
                } catch {
                    this.showToast(_i('copy_failed'), 'danger');
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
                    <button type="button" class="btn-close btn-close-white me-2 m-auto" aria-label="${_i('close')}"></button>
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
            // BCP-47 derived from <html lang="..."> -- locale-aware "vor 2 Stunden" / "2 hours ago".
            const rtf = new Intl.RelativeTimeFormat(_locale(), { numeric: 'auto' });
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
            return d.toLocaleString(_locale(), { dateStyle: 'medium', timeStyle: 'short' });
        },

        emptyHint() {
            if (this.filter.search ||
                this.projectFilter.length > 0 ||
                this.tagFilter.length > 0 ||
                this.phaseFilter.length > 0 ||
                this.priorityFilter.length > 0) {
                return _i('empty_filtered');
            }
            if (this.tab === 'open') return _i('empty_open');
            if (this.tab === 'done') return _i('empty_done');
            return _i('empty_archive');
        },

        // ---- Tag cleanup (header action) ----
        // POSTs to /api/tags/cleanup, then refreshes the tag list and shows a toast.
        // Idempotent: clicking again on a clean DB just toasts "Keine ungenutzten Tags."
        async cleanupTags() {
            const r = await fetch('/api/tags/cleanup', { method: 'POST' });
            if (!r.ok) {
                this.showToast(_i('cleanup_failed'), 'danger');
                return;
            }
            const data = await r.json();
            const removed = data.removed || 0;
            const names = Array.isArray(data.removed_names) ? data.removed_names : [];
            if (removed === 0) {
                this.showToast(_i('cleanup_none'), 'info');
            } else {
                // Render at most 5 names, append ", +N more" tail.
                const head = names.slice(0, 5).join(', ');
                const tail = names.length > 5
                    ? _i('cleanup_more', {n: names.length - 5})
                    : '';
                this.showToast(
                    _i('cleanup_removed', {n: removed, head: head, tail: tail}),
                    'success'
                );
            }
            // Tag-list may have shrunk -> refresh sidebar feed and prune stale filter.
            await this.loadTags();
            this.pruneStaleTagFilter();
        },
    };
}
