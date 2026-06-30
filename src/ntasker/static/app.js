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
const LS_KEY_SHOW_EMPTY_PROJECTS = 'ntasker.showEmptyProjects';

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

// Open Claude terminals, keyed by task id: taskId -> {ws, term, fit}.
// Kept at module scope (not in Alpine state) so the xterm Terminal instances
// never land inside Alpine's reactive proxy. The run view shows one tab per
// live session; each tab owns its own Terminal + WebSocket here, while the
// server-side PTY sessions persist in the background.
const _claudeTerms = new Map();

// Decode a base64 string (PTY bytes from the server) into a Uint8Array that
// xterm's write() accepts -- avoids UTF-8 corruption when multibyte sequences
// are split across PTY reads.
function _b64ToBytes(b64) {
    const bin = atob(b64);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    return bytes;
}

function tracker(serverDefaultView, claudeOpenTerminal = true, defaultAgent = 'claude') {
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
        // New-task form accordion: collapsed by default so more of the task
        // list stays visible; the card header toggles it (toggleNewTaskForm).
        formOpen: false,
        // Sidebar: hide projects with 0 open tasks by default; this switch
        // (persisted) flips them back into view.
        showEmptyProjects: localStorage.getItem(LS_KEY_SHOW_EMPTY_PROJECTS) === '1',
        // Drag&drop state. ``draggedTaskId`` is captured on dragstart so the
        // drop handler can identify the moving task without parsing dataTransfer
        // (Firefox is picky about reading text/plain mid-drag). ``dragOverColumn``
        // drives the column-highlight CSS class.
        draggedTaskId: null,
        dragOverColumn: null,
        // Intra-group reordering indicator: the card/row the cursor hovers and
        // whether the drop would land above (true) or below (false) it. Drives
        // the insertion-line CSS (.drop-before / .drop-after).
        dragOverTaskId: null,
        dragOverBefore: false,

        // ---- Agent registry (Claude / OpenCode / Pi) ----
        // ntasker is agent-agnostic: each task carries an ``agent`` and the run
        // button shows that agent's icon. ``agents`` is the /api/agents feed --
        // one entry per agent with {key,label,icon,available,assets}.
        // ``defaultAgent`` is the agent a task without an explicit one runs on.
        agents: [],
        defaultAgent: defaultAgent || 'claude',

        // Configured projects base dir (expanded) or ''. Injected server-side;
        // drives the "new project will be created at <path>" notice.
        projectsBase: (typeof window !== 'undefined' && window.__projectsBase) || '',

        // ---- Agent run (interactive terminal session) ----
        // claudeAvailable = at least one agent's CLI is launchable (any run is
        // possible). Per-task runnability is decided by taskRunnable(task).
        // claudeView = task id of the currently ACTIVE run tab (or null when the
        // run view is closed). claudeTabs = [{taskId, taskTitle, status}], one per
        // open session tab; claudeSessions is the set of task ids with a live
        // server-side session (busy spinners). The xterm Terminals + WebSockets
        // live in the module-level _claudeTerms map (out of Alpine's proxy).
        claudeAvailable: false,
        claudeReason: null,
        claudeView: null,
        claudeTabs: [],
        // When false (the `claude_open_terminal` setting), starting a session
        // (Create + Run or the per-task run button) attaches it in the
        // background and keeps the board on screen instead of opening the run
        // view. Clicking an already-running task still surfaces its terminal.
        claudeOpenTerminal: claudeOpenTerminal !== false,
        claudeSessions: [],
        // Subset of claudeSessions that has gone silent long enough to look
        // blocked on a prompt -- drives the "waiting for input" highlight.
        claudeWaiting: [],
        // Project of each active session, keyed by task id (string keys, as
        // they arrive from JSON). Feeds the running-projects chips and the
        // same-project parallel-run warning.
        claudeSessionProjects: {},

        // Multi-value project filter. Empty list = no filter (all tasks).
        // Special value '__none__' = include cross-project tasks (project IS NULL).
        projectFilter: [],

        // Multi-value tag filter (OR-combined). Empty list = no filter.
        tagFilter: [],

        // Type-ahead query that narrows the sidebar tag list (display only --
        // it does not change which tasks are shown).
        tagSearch: '',

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
            agent: '',         // '' = use the default agent
            tags: [],          // committed tag list (lowercase strings)
            tagInput: '',      // current text in the tag-input
            depends: [],       // committed dependencies: [{id, title, done}]
            depInput: '',      // current text in the dependency-input
        },
        // Dependency autocomplete suggestions for the currently focused
        // input (form or edit -- only one is open at a time).
        depSuggest: [],
        // Highlighted suggestion index per tag-input ('form' | 'edit'); -1 = none.
        tagHighlight: { form: -1, edit: -1 },
        projectHighlight: { form: -1, edit: -1 },
        // Caret position among the tag chips per input. -1 = the text input
        // (caret after the last chip); 0..len-1 = a chip has focus and the
        // caret sits to its LEFT. Backspace removes the chip left of the
        // caret, Delete the chip right of it. See the chip-nav helpers below.
        tagCaret: { form: -1, edit: -1 },
        editing: null,               // task object or null
        counts: { open: 0, done: 0, archive: 0 },

        async init() {
            this.restoreProjectFilter();
            this.restoreTagFilter();
            this.restorePhaseFilter();
            this.restorePriorityFilter();
            await Promise.all([
                this.loadProjects(),
                this.loadTags(),
                this.loadPhases(),
                this.loadPriorities(),
                this.loadClaudeStatus(),
                this.loadClaudeSessions(),
            ]);
            // After loading projects/tags, drop stale entries silently.
            this.pruneStaleProjectFilter();
            this.pruneStaleTagFilter();
            // A restored single-project filter prefills the new-task form.
            this.syncFormProjectFromFilter();
            // Phase / priority filters are validated against fixed value lists at restore time.
            await this.loadTasks();
            // Start the live-update poll last, once the initial state is in
            // place -- it then refetches whenever a CLI/API change is detected.
            this.startChangePolling();
            // Hash routing: the run view lives at #/run/<id> so the browser
            // history / Back button work and a run is shareable / reloadable.
            window.addEventListener('hashchange', () => this._applyRoute());
            // A single window-resize listener refits whichever terminal is on
            // screen (per-terminal listeners would also fit hidden tabs to 0).
            window.addEventListener('resize', () => {
                if (this.claudeView !== null) this._fitAndSync(this.claudeView);
            });
            // Honor a deep-linked / reloaded #/run/<id> once everything is up.
            this._applyRoute();
        },

        // Convenience: just the project names (for <select> in forms).
        // Excludes the __none__ sentinel.
        get projectNames() {
            return this.projects
                .filter(p => p.name !== PROJECT_NONE)
                .map(p => p.name);
        },

        // The single real project currently in the sidebar filter, or null when
        // zero or 2+ are active (the cross-project sentinel never counts).
        get singleFilteredProject() {
            const real = this.projectFilter.filter(n => n !== PROJECT_NONE);
            return real.length === 1 ? real[0] : null;
        },

        // Prefill the new-task form's project from the sidebar filter: exactly
        // one filtered project becomes the default, any other count clears it.
        // Driven by the filter so adding a 2nd project empties the field again.
        syncFormProjectFromFilter() {
            this.form.project = this.singleFilteredProject || '';
        },

        // Projects shown in the sidebar. By default rows with no open tasks are
        // hidden to cut clutter; the "show empty projects" switch reveals them.
        // A project currently in the filter stays visible even when empty, so
        // the user can always un-check it.
        get visibleProjects() {
            if (this.showEmptyProjects) return this.projects;
            return this.projects.filter(p =>
                p.open_count > 0 || this.projectFilter.includes(p.name)
            );
        },

        // True when at least one project has no open tasks -- gates the switch
        // so it only appears when it would actually do something.
        get hasEmptyProjects() {
            return this.projects.some(p => p.open_count === 0);
        },

        // Sidebar tag list: narrowed by the type-ahead query and sorted
        // alphabetically (the API's open-count order is overridden here).
        get visibleTags() {
            const q = this.tagSearch.trim().toLowerCase();
            const list = q ? this.tags.filter(t => t.name.includes(q)) : this.tags;
            return [...list].sort((a, b) => a.name.localeCompare(b.name));
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

        persistShowEmptyProjects() {
            localStorage.setItem(LS_KEY_SHOW_EMPTY_PROJECTS, this.showEmptyProjects ? '1' : '0');
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
            this.syncFormProjectFromFilter();
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
            this.syncFormProjectFromFilter();
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

        // New-task accordion toggle. On expand, move focus into the Project
        // field. Queried by id, not $refs: the input lives inside a nested
        // x-data combobox, so its x-ref would not register on this root
        // component. The focus is deferred via $nextTick *and* rAF: at
        // $nextTick the x-show'd form body is still display:none (focus() on a
        // hidden element is a no-op), so we wait one frame for it to paint.
        toggleNewTaskForm() {
            this.formOpen = !this.formOpen;
            if (this.formOpen) {
                this.$nextTick(() => requestAnimationFrame(
                    () => document.getElementById('projectinput-form')?.focus()));
            }
        },

        // Sidebar "+" on a project row: open the new-task form pre-filled with
        // that project and drop the caret straight into the Title field. Leaves
        // the run view first (the form lives on the board) so the focus lands.
        newTaskForProject(name) {
            this.form.project = name;
            this.formOpen = true;
            if (this.claudeView !== null) location.hash = '#/';
            this.$nextTick(() => {
                const el = this.$refs.titleInput;
                if (!el) return;
                el.focus();
                el.scrollIntoView({ block: 'nearest' });
            });
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
            this.dragOverTaskId = null;
            // A change detected mid-drag was deferred (re-rendering would abort
            // the drag); apply it now that the drag is over.
            if (this._liveRefreshPending) {
                this._liveRefreshPending = false;
                this.refreshAll();
            }
        },

        // ---- Drag & Drop: intra-group reordering (kanban column + list) ----
        // Manual order lives in each task's fractional ``sort_order`` (rows are
        // served ``sort_order DESC`` -- larger = nearer the top). Dropping a
        // task between two neighbours stores the average of their values, so a
        // single PATCH rewrites only the moved row. ``colKey`` is the kanban
        // column key, or ``null`` in the flat list view.

        // The visible group for a drop target, in display order, minus the
        // task being dragged (it is leaving its old slot).
        _dropGroup(colKey, excludeId) {
            const base = colKey == null ? this.tasks : this.kanbanTasksFor(colKey);
            return base.filter(t => t.id !== excludeId);
        },

        // Fractional ``sort_order`` that lands the dragged task at ``index``
        // within ``group`` (display order, sort_order DESC, dragged removed).
        _sortOrderForInsert(group, index) {
            const above = index > 0 ? group[index - 1].sort_order : null;        // larger
            const below = index < group.length ? group[index].sort_order : null; // smaller
            if (above === null && below === null) return 0;   // empty group
            if (above === null) return below + 1;             // top
            if (below === null) return above - 1;             // bottom
            return (above + below) / 2;                       // between neighbours
        },

        onCardDragOver(event, task, colKey) {
            if (this.draggedTaskId == null) return;
            // Hovering the dragged card itself: no insertion line, but keep the
            // column highlighted and the drop allowed (a no-op drop is fine).
            if (this.draggedTaskId === task.id) {
                this.dragOverTaskId = null;
                if (colKey != null) this.dragOverColumn = colKey;
                if (event.dataTransfer) event.dataTransfer.dropEffect = 'move';
                return;
            }
            // Kanban: a blocked task may not advance into Review/Done.
            if (colKey != null) {
                const dragged = this.tasks.find(t => t.id === this.draggedTaskId);
                if (dragged && !this.canDropOn(dragged, colKey)) {
                    if (event.dataTransfer) event.dataTransfer.dropEffect = 'none';
                    this.dragOverTaskId = null;
                    this.dragOverColumn = null;
                    return;
                }
                this.dragOverColumn = colKey;
            }
            const rect = event.currentTarget.getBoundingClientRect();
            this.dragOverBefore = (event.clientY - rect.top) < rect.height / 2;
            this.dragOverTaskId = task.id;
            if (event.dataTransfer) event.dataTransfer.dropEffect = 'move';
        },

        async onCardDrop(event, task, colKey) {
            const id = this.draggedTaskId;
            const before = this.dragOverBefore;
            this.dragOverTaskId = null;
            this.dragOverColumn = null;
            this.draggedTaskId = null;
            if (id == null || id === task.id) return; // dropped on itself
            const group = this._dropGroup(colKey, id);
            let idx = group.findIndex(t => t.id === task.id);
            if (idx < 0) idx = group.length;
            else if (!before) idx += 1;
            await this._applyDrop(id, colKey, group, idx);
        },

        // Shared drop committer: builds the PATCH body (sort_order, plus the
        // phase/status flip when a kanban drop crosses columns) and reloads.
        async _applyDrop(id, colKey, group, insertIndex) {
            const task = this.tasks.find(t => t.id === id);
            if (!task) return;
            const body = {};
            if (colKey != null) {
                if (!this.canDropOn(task, colKey)) {
                    this.showToast(_i('blocked_hint'), 'danger');
                    return;
                }
                if (colKey === 'done') {
                    if (task.status !== 'done') body.status = 'done';
                } else {
                    if (task.status === 'done') body.status = 'open';
                    if (task.phase !== colKey) body.phase = colKey;
                }
            }
            const idx = Math.max(0, Math.min(insertIndex, group.length));
            const newOrder = this._sortOrderForInsert(group, idx);
            if (task.sort_order !== newOrder) body.sort_order = newOrder;
            if (Object.keys(body).length === 0) return; // no actual move
            const r = await this.patch(id, body);
            if (r && r.ok) await this.refreshAll();
        },

        // A blocked task (open dependencies) must not advance to Review or Done.
        canDropOn(task, colKey) {
            if ((colKey === 'review' || colKey === 'done') && this.isBlocked(task)) {
                return false;
            }
            return true;
        },

        onColumnDragOver(event, colKey) {
            const task = this.draggedTaskId != null
                ? this.tasks.find(t => t.id === this.draggedTaskId)
                : null;
            // Reject the drop visually (no-drop cursor + no highlight) when a
            // blocked task is dragged onto Review/Done.
            if (task && !this.canDropOn(task, colKey)) {
                if (event.dataTransfer) event.dataTransfer.dropEffect = 'none';
                this.dragOverColumn = null;
                return;
            }
            // Required to allow the drop event to fire.
            if (event.dataTransfer) event.dataTransfer.dropEffect = 'move';
            this.dragOverColumn = colKey;
            // Over the column's empty area (between/below cards) -- no specific
            // insertion line. Card-level dragover sets this again when hovered.
            this.dragOverTaskId = null;
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
            this.dragOverTaskId = null;
            this.draggedTaskId = null;
            if (id == null) return;
            const task = this.tasks.find(t => t.id === id);
            if (!task) return;
            // Safety net behind onColumnDragOver: a blocked task can't move to
            // Review/Done. The drop event can still fire in some browsers, so
            // re-check here and explain via a toast.
            if (!this.canDropOn(task, colKey)) {
                this.showToast(_i('blocked_hint'), 'danger');
                return;
            }
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

        // Reset the search box (clear-button in the search field) and reload.
        clearSearch() {
            this.filter.search = '';
            this.loadTasks();
        },

        // True iff any search/filter is narrowing the visible tasks.
        get hasActiveFilters() {
            return !!this.filter.search ||
                this.projectFilter.length > 0 ||
                this.tagFilter.length > 0 ||
                this.phaseFilter.length > 0 ||
                this.priorityFilter.length > 0;
        },

        // "Show all tasks": clear the search box and every filter (project,
        // tag, phase, priority) in one go, persist the empty filters, reload.
        clearAllFilters() {
            this.filter.search = '';
            this.projectFilter = [];
            this.tagFilter = [];
            this.phaseFilter = [];
            this.priorityFilter = [];
            this.persistProjectFilter();
            this.persistTagFilter();
            this.persistPhaseFilter();
            this.persistPriorityFilter();
            this.syncFormProjectFromFilter();
            this.loadTasks();
            this.loadCounts();
        },

        // ---- Task CRUD ----
        async createTask(run = false) {
            // Commit any pending tag input before submit.
            this.commitTagInput('form');
            // A project is optional (empty = cross-project) and may be brand
            // new -- only a title is required.
            if (!this.form.title.trim()) return;
            const body = {
                project: this.form.project || null,
                title: this.form.title.trim(),
                description: this.form.description.trim() || null,
                phase: this.form.phase || null,
                priority: this.form.priority || 'normal',
                agent: this.form.agent || null,
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
            this.form.agent = '';
            this.form.tags = [];
            this.form.tagInput = '';
            this.form.depends = [];
            this.form.depInput = '';
            // Keep project selection for rapid same-project entry.
            await this.refreshAll();
            // Create + Run: hand the fresh task straight to its agent. The run
            // view replaces the page, so skip the create toast below.
            if (run && this.taskRunnable(created)) {
                this.openClaudeRun(created);
                return;
            }
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
                agent: t.agent || null,
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

        // Enter in a dialog triggers its default action. Bound on the modal
        // root so it catches Enter bubbling up from any field. Skips a
        // <textarea> on plain Enter (that's a newline -- use Ctrl/Cmd+Enter to
        // submit) and bails when a child handler already consumed the event
        // (combobox inputs preventDefault to accept a suggestion / commit a tag).
        onDialogEnter(event, action) {
            if (event.defaultPrevented) return;
            const tag = (event.target.tagName || '').toLowerCase();
            if (tag === 'textarea' && !(event.ctrlKey || event.metaKey)) return;
            event.preventDefault();
            action();
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

        // ---- Live updates (poll a cheap change token) ----
        // The CLI and the API both write straight to SQLite, so a write from
        // any source -- another browser tab, the CLI, or Claude via the CLI --
        // bumps /api/changes (the DB file's mtime). We poll it on an interval
        // and refetch only when the token changed, so phase transitions
        // (planned -> wip -> review -> done) and every other change reflect
        // within ~5s without a manual reload and without repeatedly pulling
        // the full task list. Own actions still refresh eagerly (see refreshAll).
        startChangePolling() {
            this._changeToken = null;
            this._pollChanges();   // establish the baseline immediately
            this._changeTimer = setInterval(() => {
                this._pollChanges();
                // Busy indicators must self-heal. A worker restart (e.g.
                // `serve --reload`, a crash) wipes the in-memory session
                // registry without touching the DB, so the change token never
                // bumps -- _pollChanges alone would never notice. Re-poll the
                // live-session set on its own cadence so a spinner for a
                // vanished session clears within ~5s instead of forever.
                if (this.claudeAvailable) this.loadClaudeSessions();
            }, 5000);
        },

        async _pollChanges() {
            let token;
            try {
                const r = await fetch('/api/changes');
                if (!r.ok) return;
                token = (await r.json()).v;
            } catch {
                return;   // server momentarily unreachable (restart) -- retry next tick
            }
            if (this._changeToken == null) {   // first successful read = baseline
                this._changeToken = token;
                return;
            }
            if (token === this._changeToken) return;
            this._changeToken = token;
            // Don't re-render mid-drag (an HTML5 drag aborts if its node is
            // replaced); onCardDragEnd applies the deferred refresh.
            if (this.draggedTaskId != null) {
                this._liveRefreshPending = true;
                return;
            }
            this.refreshAll();
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
            // Left arrow at the very start of an empty/anchored input steps the
            // caret into the chip row (focus the last chip).
            if (event.key === 'ArrowLeft'
                && event.target.selectionStart === 0
                && event.target.selectionEnd === 0
                && bucket.tags.length > 0) {
                event.preventDefault();
                this._setTagCaret(which, bucket.tags.length - 1);
                return;
            }
            // Backspace on empty input removes the last tag (caret stays at input).
            if (event.key === 'Backspace' && !bucket[key]) {
                if (bucket.tags.length > 0) bucket.tags.pop();
            }
        },

        // ---- Chip caret navigation (keyboard tag editing) ----
        // Move the caret and pull DOM focus onto the matching element. idx = -1
        // focuses the text input; otherwise the chip at idx (caret to its left).
        _setTagCaret(which, idx) {
            this.tagCaret[which] = idx;
            this.$nextTick(() => this._focusTagCaret(which));
        },

        _focusTagCaret(which) {
            const idx = this.tagCaret[which];
            if (idx === -1) {
                const inp = document.getElementById('taginput-' + which);
                if (inp) inp.focus();
                return;
            }
            const chips = document.querySelectorAll(
                '[data-chips="' + which + '"] [data-chip]');
            const el = chips[idx];
            if (el) {
                el.focus();
            } else {
                // Caret ran past the last chip -> fall back to the input.
                this.tagCaret[which] = -1;
                const inp = document.getElementById('taginput-' + which);
                if (inp) inp.focus();
            }
        },

        // Keydown on a focused chip (idx = the chip right of the caret).
        onChipKeydown(event, which, idx) {
            const bucket = this._tagBucket(which);
            if (!bucket) return;
            const len = bucket.tags.length;
            const k = event.key;
            if (k === 'ArrowLeft') {
                event.preventDefault();
                this._setTagCaret(which, Math.max(0, idx - 1));
            } else if (k === 'ArrowRight') {
                event.preventDefault();
                this._setTagCaret(which, idx + 1 >= len ? -1 : idx + 1);
            } else if (k === 'Backspace') {
                // Remove the chip LEFT of the caret.
                event.preventDefault();
                if (idx > 0) {
                    bucket.tags.splice(idx - 1, 1);
                    this._setTagCaret(which, idx - 1);
                }
            } else if (k === 'Delete') {
                // Remove the chip RIGHT of the caret (the focused one).
                event.preventDefault();
                bucket.tags.splice(idx, 1);
                this._setTagCaret(which, idx >= bucket.tags.length ? -1 : idx);
            } else if (k === 'Escape') {
                event.preventDefault();
                this._setTagCaret(which, -1);
            } else if (k.length === 1 && !event.ctrlKey && !event.metaKey && !event.altKey) {
                // Any printable key resumes typing in the text input.
                event.preventDefault();
                bucket[this._tagInputProp(which)] += k;
                this._setTagCaret(which, -1);
            }
        },

        removeTagFromForm(idx) {
            this.form.tags.splice(idx, 1);
            this.tagCaret.form = -1;
        },

        removeTagFromEditing(idx) {
            if (this.editing) this.editing.tags.splice(idx, 1);
            this.tagCaret.edit = -1;
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
            this.tagHighlight[which] = -1;
        },

        // Move the dropdown highlight (dir = +1 down / -1 up), wrapping around.
        // Resets to -1 when there are no suggestions.
        onTagArrow(which, dir) {
            const n = this.tagSuggestions(which).length;
            if (!n) { this.tagHighlight[which] = -1; return; }
            let i = this.tagHighlight[which] + dir;
            if (i < 0) i = n - 1;
            else if (i >= n) i = 0;
            this.tagHighlight[which] = i;
        },

        // Enter in a tag-input. If a suggestion is highlighted, pick it.
        // Otherwise let the event fall through: in the new-task form that
        // submits the form (createTask commits any typed tag first); in the
        // edit modal -- which has no <form> -- commit the typed tag instead.
        onTagEnter(event, which) {
            const sugg = this.tagSuggestions(which);
            const hi = this.tagHighlight[which];
            if (sugg.length && hi >= 0 && hi < sugg.length) {
                event.preventDefault();
                this.selectSuggestion(which, sugg[hi]);
                return;
            }
            if (which === 'edit') {
                event.preventDefault();
                this.commitTagInput(which);
            }
        },

        // Tab in a tag-input with a partial query: complete to a suggestion and
        // add it straight away (highlighted one if any, else the first match),
        // instead of letting Tab move focus out of the field.
        onTagTab(event, which) {
            const sugg = this.tagSuggestions(which);
            if (!sugg.length) return;
            const hi = this.tagHighlight[which];
            const pick = (hi >= 0 && hi < sugg.length) ? sugg[hi] : sugg[0];
            event.preventDefault();
            this.selectSuggestion(which, pick);
        },

        // ---- Project input autocomplete (new-task form & edit modal) ----
        // Single-value combobox over existing project names. Mirrors the tag
        // input's keyboard handling: arrows highlight, Tab/Enter accept.
        // `which` = 'form' | 'edit'.
        _projectBucket(which) {
            return which === 'edit' ? this.editing : this.form;
        },

        // Matching project names for the current query, ranked: exact match
        // first, then startswith, then contains. Non-matches are dropped.
        projectSuggestions(which) {
            const bucket = this._projectBucket(which);
            if (!bucket) return [];
            const q = (bucket.project || '').trim().toLowerCase();
            if (!q) return [];
            return this.projectNames
                .map(name => {
                    const n = name.toLowerCase();
                    let rank = -1;
                    if (n === q) rank = 0;
                    else if (n.startsWith(q)) rank = 1;
                    else if (n.includes(q)) rank = 2;
                    return { name, rank };
                })
                .filter(e => e.rank >= 0)
                .sort((a, b) => a.rank - b.rank || a.name.localeCompare(b.name))
                .map(e => e.name)
                .slice(0, 8);
        },

        // True iff the field holds the exact name of an existing project.
        // Empty / partial / unknown names are invalid (no cross-project here).
        isProjectValid(which) {
            const bucket = this._projectBucket(which);
            if (!bucket) return false;
            const q = (bucket.project || '').trim();
            if (!q) return false;
            return this.projectNames.includes(q);
        },

        // True iff the field holds a non-empty name that is NOT an existing
        // project -- i.e. a brand-new project. Such a name is accepted (the
        // task is creatable); its directory is created on first run.
        isNewProject(which) {
            const bucket = this._projectBucket(which);
            if (!bucket) return false;
            const q = (bucket.project || '').trim();
            if (!q) return false;
            return !this.projectNames.includes(q);
        },

        // Notice shown under the project input for a new project: where the
        // directory will be created (when a projects base is configured and the
        // name is relative), or a hint to configure one otherwise.
        newProjectMessage(which) {
            const bucket = this._projectBucket(which);
            if (!bucket) return '';
            const q = (bucket.project || '').trim();
            if (!q || this.projectNames.includes(q)) return '';
            if (this.projectsBase && !q.startsWith('/')) {
                const sep = this.projectsBase.endsWith('/') ? '' : '/';
                return _i('project_will_create', { path: this.projectsBase + sep + q });
            }
            return _i('project_no_base_hint');
        },

        selectProject(which, name) {
            const bucket = this._projectBucket(which);
            if (!bucket) return;
            bucket.project = name;
            this.projectHighlight[which] = -1;
        },

        // Move the dropdown highlight (dir = +1 down / -1 up), wrapping around.
        onProjectArrow(which, dir) {
            const n = this.projectSuggestions(which).length;
            if (!n) { this.projectHighlight[which] = -1; return; }
            let i = this.projectHighlight[which] + dir;
            if (i < 0) i = n - 1;
            else if (i >= n) i = 0;
            this.projectHighlight[which] = i;
        },

        // Move focus to the next focusable control after `el`, skipping the
        // suggestion dropdown so Tab lands on the next form field, not a
        // dropdown item. Scoped to the enclosing form / modal.
        _focusNextFrom(el) {
            if (!el) return;
            const scope = el.closest('form, .modal-content') || document;
            const focusables = Array.from(
                scope.querySelectorAll('input, select, textarea, button'))
                .filter(n => !n.disabled && n.tabIndex !== -1
                    && n.offsetParent !== null && !n.closest('.dropdown-menu'));
            const i = focusables.indexOf(el);
            if (i >= 0 && i + 1 < focusables.length) focusables[i + 1].focus();
        },

        // Tab behaviour:
        //  - an arrow-highlighted suggestion always wins (accept it);
        //  - otherwise, if the field is already an exact match, leave it be and
        //    let Tab move focus on (never silently swap to a contains-match);
        //  - otherwise complete to the top-ranked suggestion.
        // When Tab makes a selection it suppresses the native focus move, so we
        // advance focus to the next field manually afterwards.
        onProjectTab(event, which) {
            const sugg = this.projectSuggestions(which);
            const hi = this.projectHighlight[which];
            if (hi >= 0 && hi < sugg.length) {
                event.preventDefault();
                this.selectProject(which, sugg[hi]);
                this._focusNextFrom(event.target);
                return;
            }
            if (this.isProjectValid(which)) return;
            if (!sugg.length) return;
            event.preventDefault();
            this.selectProject(which, sugg[0]);
            this._focusNextFrom(event.target);
        },

        // Enter accepts a highlighted suggestion; otherwise it falls through
        // (submits the new-task form; no-op in the edit modal, which has no form).
        onProjectEnter(event, which) {
            const sugg = this.projectSuggestions(which);
            const hi = this.projectHighlight[which];
            if (sugg.length && hi >= 0 && hi < sugg.length) {
                event.preventDefault();
                this.selectProject(which, sugg[hi]);
            }
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

        // The still-open dependencies that block this task ([] = not blocked).
        blockingDeps(task) {
            return (task.depends || []).filter(d => !d.done);
        },

        // A task is blocked while any dependency is not yet done.
        isBlocked(task) {
            return this.blockingDeps(task).length > 0;
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
            if (this.hasActiveFilters) {
                return _i('empty_filtered');
            }
            if (this.tab === 'open') return _i('empty_open');
            if (this.tab === 'done') return _i('empty_done');
            return _i('empty_archive');
        },

        // ---- Tag cleanup (header action) ----
        // POSTs to /api/tags/cleanup, then refreshes the tag list and shows a toast.
        // Idempotent: clicking again on a clean DB just toasts "Keine ungenutzten Tags."
        // ---- Claude run ("Run with Claude") ----
        // The run view embeds the *real* interactive `claude` TUI via xterm.js
        // over a WebSocket. The PTY process lives server-side and is persistent:
        // it keeps running when you go Back (or reload), and reattaching replays
        // the recent output to reconstruct the screen. See claude_runner.py.

        // Load the agent registry (GET /api/agents): which agents exist, whether
        // each CLI is launchable, and the resolved default agent. Non-fatal --
        // run buttons just stay hidden when nothing is available.
        async loadClaudeStatus() {
            try {
                const r = await fetch('/api/agents');
                if (!r.ok) return;
                const data = await r.json();
                this.agents = data.agents || [];
                if (data.default) this.defaultAgent = data.default;
                this.claudeAvailable = this.agents.some(a => a.available);
            } catch (_e) {
                this.claudeAvailable = false;
            }
        },

        // ---- Agent helpers (per-task icon + runnability) ----
        // The registry entry for ``key`` (or null when unknown).
        agentByKey(key) {
            return this.agents.find(a => a.key === key) || null;
        },
        // The effective agent key for a task: its own ``agent`` or the default.
        taskAgentKey(task) {
            return (task && task.agent) || this.defaultAgent;
        },
        // Whether a given agent key's CLI is currently launchable.
        agentAvailable(key) {
            const a = this.agentByKey(key);
            return !!(a && a.available);
        },
        // Whether a task can be run right now (its agent's CLI is available).
        taskRunnable(task) {
            return this.agentAvailable(this.taskAgentKey(task));
        },
        // Static URL of a task's agent icon (for the run button <img>).
        agentIconUrl(task) {
            const a = this.agentByKey(this.taskAgentKey(task));
            return a && a.icon ? ('/static/' + a.icon) : '';
        },
        // Human label for an agent key (for tooltips / the picker).
        agentLabel(key) {
            const a = this.agentByKey(key);
            return a ? a.label : (key || '');
        },

        // Refresh the set of task ids with a live session (busy indicators) and
        // reconcile the run-view tab strip against it -- a tab exists for every
        // *active* session (running OR waiting for input), wherever it was
        // started (this browser, another tab, or the CLI).
        async loadClaudeSessions() {
            try {
                const r = await fetch('/api/claude/sessions');
                if (r.ok) {
                    const d = await r.json();
                    this.claudeSessions = d.active || [];
                    this.claudeWaiting = d.waiting || [];
                    this.claudeSessionProjects = d.projects || {};
                    this._syncTabsFromSessions();
                }
            } catch (_e) { /* leave the last known set */ }
        },

        // Mirror the live-session set into claudeTabs: add a tab for every active
        // session we don't track yet, refresh each tab's status (waiting/running)
        // and drop tabs whose session has ended -- except the one you're viewing
        // (kept as 'exited' so you can read the final output) and a just-opened
        // tab that hasn't registered as a session yet ('connecting').
        _syncTabsFromSessions() {
            const waiting = new Set(this.claudeWaiting);
            const active = new Set(this.claudeSessions);
            for (const id of this.claudeSessions) {
                if (!this.claudeTabs.some(t => t.taskId === id)) {
                    this.claudeTabs.push({ taskId: id, taskTitle: '', status: waiting.has(id) ? 'waiting' : 'running' });
                    this._fetchTabTitle(id);
                }
            }
            this.claudeTabs = this.claudeTabs.filter(tab => {
                if (active.has(tab.taskId)) {
                    tab.status = waiting.has(tab.taskId) ? 'waiting' : 'running';
                    return true;
                }
                if (tab.taskId === this.claudeView) { tab.status = 'exited'; return true; }
                if (tab.status === 'connecting') return true;
                this._teardownTerm(tab.taskId);
                return false;
            });
        },

        // Fill in a tab's title once we know its id (auto-added session tabs).
        async _fetchTabTitle(id) {
            try {
                const r = await fetch(`/api/tasks/${id}`);
                if (!r.ok) return;
                const t = await r.json();
                const tab = this.claudeTabs.find(x => x.taskId === id);
                if (tab) tab.taskTitle = t.title || '';
            } catch (_e) { /* leave blank */ }
        },

        // 'waiting' (blocked on a prompt) > 'running' (live session) > null.
        taskRunPhase(taskId) {
            if (this.claudeWaiting.includes(taskId)) return 'waiting';
            return this.claudeSessions.includes(taskId) ? 'running' : null;
        },

        // Tooltip/aria for the per-task run button. A live session means "switch
        // to it" (not "run"); an idle session blocked on a prompt keeps the
        // "waiting for input" hint; no session means "run".
        runButtonTitle(task) {
            const phase = this.taskRunPhase(task.id);
            if (phase === 'waiting') return _i('claude_waiting');
            const agent = ' (' + this.agentLabel(this.taskAgentKey(task)) + ')';
            if (phase === 'running') return _i('claude_switch_session') + agent;
            return _i('claude_run') + agent;
        },

        // One chip per project that currently has at least one live Claude
        // session -- "what's running in parallel right now". Cross-project
        // sessions (no project) are grouped under the PROJECT_NONE sentinel.
        // ``count`` = sessions in that project; ``waiting`` = any is blocked on
        // a prompt. Sorted by name so chip order is stable across polls.
        get runningProjectChips() {
            const waiting = new Set(this.claudeWaiting);
            const byProject = new Map();
            for (const id of this.claudeSessions) {
                const proj = this.claudeSessionProjects[String(id)] || PROJECT_NONE;
                const chip = byProject.get(proj) || { name: proj, count: 0, waiting: false };
                chip.count += 1;
                if (waiting.has(id)) chip.waiting = true;
                byProject.set(proj, chip);
            }
            return [...byProject.values()].sort((a, b) => a.name.localeCompare(b.name));
        },

        // True when ``project`` already has a live session for a DIFFERENT task
        // -- two agents in one project can collide. Cross-project (null) tasks
        // never count: they share no working dir to clobber.
        _projectHasOtherSession(project, exceptTaskId) {
            if (!project) return false;
            return this.claudeSessions.some(id =>
                id !== exceptTaskId &&
                (this.claudeSessionProjects[String(id)] || null) === project
            );
        },

        // The tab object for the currently active run, or null.
        get activeTab() {
            return this.claudeTabs.find(t => t.taskId === this.claudeView) || null;
        },

        // ---- Hash routing (#/run/<id>) ----
        // The hash is the single source of truth for which run tab is shown, so
        // the browser Back/Forward buttons and deep links / reloads all work.
        // Navigation helpers write the hash; _applyRoute (on hashchange and at
        // boot) reconciles the on-screen view to it.
        _applyRoute() {
            const m = location.hash.match(/^#\/run\/(\d+)$/);
            if (!m) { this.claudeView = null; return; }
            const id = parseInt(m[1], 10);
            if (this.claudeTabs.some(t => t.taskId === id)) {
                this._showTab(id);   // existing tab -> connect on demand + reveal
                return;
            }
            // Deep-link / forward to a session we have no tab for yet: open it
            // (the socket reattaches the server-side PTY if one is still live).
            if (this.claudeAvailable) this._openRunById(id);
            else this.claudeView = null;
        },

        // Reveal a tab: make it active, connect its terminal if needed (a tab
        // mirrored from a background session is connected lazily on first view),
        // then fit + focus once the host is on screen.
        _showTab(id) {
            this.claudeView = id;
            this.$nextTick(() => {
                this._ensureTabConnected(id);
                this._fitAndSync(id);
                _claudeTerms.get(id)?.term.focus();
            });
        },

        // Attach a terminal to a tab that has none yet -- reattaches the live
        // server-side PTY (no seed; seed is ignored on reattach anyway).
        _ensureTabConnected(id) {
            if (!_claudeTerms.has(id)) this._claudeConnect(id, '', '');
        },

        // Launch (or re-focus) a run tab for a task. Fetches the guessed cwd +
        // `/task <id>` seed for a fresh session, then drives the #/run/<id> hash
        // which shows the tab and attaches the socket -- starting the server-side
        // session if one isn't already running.
        async openClaudeRun(task) {
            const id = task.id;
            if (this.claudeTabs.some(t => t.taskId === id)) {
                this.activateTab(id);   // already open -> just switch to its tab
                return;
            }
            // Same-project collision warning: another agent is already live in
            // this project. Warn, but don't prevent -- the user may proceed.
            if (this._projectHasOtherSession(task.project, id) &&
                !confirm(_i('confirm_parallel_run', { project: task.project }))) {
                return;
            }
            let cwd = '', seed = '';
            try {
                const r = await fetch(`/api/tasks/${id}/claude-run/defaults`);
                if (r.ok) { const d = await r.json(); cwd = d.cwd || ''; seed = d.seed || ''; }
            } catch (_e) { /* defaults are best-effort */ }
            this._addTab(id, task.title || '');
            // Background start: attach the session but stay on the board. The
            // tab's xterm host still renders (hidden) via the claudeTabs x-for,
            // so the socket attaches and the server-side PTY starts; opening the
            // tab later (clicking the running task) reattaches and fits it.
            if (!this.claudeOpenTerminal) {
                this.$nextTick(() => this._claudeConnect(id, cwd, seed));
                this.showToast(_i('claude_started_background', { id }), 'success');
                return;
            }
            this.claudeView = id;
            location.hash = '#/run/' + id;   // record in history (idempotent _applyRoute)
            this.$nextTick(() => this._claudeConnect(id, cwd, seed));
        },

        // Open a tab from just an id (deep link / browser-forward / reload).
        // Looks up the title + run defaults, then connects like openClaudeRun.
        async _openRunById(id) {
            let title = '', cwd = '', seed = '';
            try {
                const r = await fetch(`/api/tasks/${id}`);
                if (r.ok) title = (await r.json()).title || '';
            } catch (_e) { /* best-effort */ }
            try {
                const r = await fetch(`/api/tasks/${id}/claude-run/defaults`);
                if (r.ok) { const d = await r.json(); cwd = d.cwd || ''; seed = d.seed || ''; }
            } catch (_e) { /* best-effort */ }
            this._addTab(id, title);
            this.claudeView = id;
            this.$nextTick(() => this._claudeConnect(id, cwd, seed));
        },

        // Switch to an already-open tab. Goes through the hash so the switch
        // lands in browser history; if the hash already matches (no event would
        // fire) reconcile directly.
        activateTab(id) {
            if (location.hash === '#/run/' + id) {
                this._showTab(id);   // same hash -> no event would fire; reveal directly
            } else {
                location.hash = '#/run/' + id;
            }
        },

        _addTab(id, title) {
            if (this.claudeTabs.some(t => t.taskId === id)) return;
            this.claudeTabs.push({ taskId: id, taskTitle: title, status: 'connecting' });
        },

        _setTabStatus(id, status) {
            const t = this.claudeTabs.find(x => x.taskId === id);
            if (t) t.status = status;
        },

        // Create the xterm terminal + WebSocket bridge for a task's tab. Attaches
        // to the per-tab host node (#claude-term-<id>) and stores the live handles
        // in the module-level _claudeTerms map.
        _claudeConnect(taskId, cwd, seed) {
            if (_claudeTerms.has(taskId)) return;   // don't double-connect a tab
            const el = document.getElementById('claude-term-' + taskId);
            if (!el || typeof Terminal === 'undefined') return;
            const term = new Terminal({
                cursorBlink: true,
                fontSize: 13,
                fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
                scrollback: 8000,
                theme: { background: '#161616' },
            });
            const fit = new FitAddon.FitAddon();
            term.loadAddon(fit);
            term.open(el);
            try { fit.fit(); } catch (_e) { /* host not laid out yet */ }

            const proto = location.protocol === 'https:' ? 'wss' : 'ws';
            const ws = new WebSocket(`${proto}://${location.host}/ws/claude/${taskId}`);
            _claudeTerms.set(taskId, { ws, term, fit });

            ws.onopen = () => {
                ws.send(JSON.stringify({ type: 'attach', cwd, seed }));
                this._fitAndSync(taskId);
                this._setTabStatus(taskId, 'running');
                this.loadClaudeSessions();
            };
            ws.onmessage = (ev) => {
                const msg = JSON.parse(ev.data);
                if (msg.type === 'output') {
                    term.write(_b64ToBytes(msg.data));
                } else if (msg.type === 'exit') {
                    this._setTabStatus(taskId, 'exited');
                    this.loadClaudeSessions();
                } else if (msg.type === 'error') {
                    term.write(`\r\n\x1b[31m[ntasker] ${msg.error}\x1b[0m\r\n`);
                    this._setTabStatus(taskId, 'exited');
                }
            };
            ws.onclose = () => { this.loadClaudeSessions(); };
            term.onData((data) => {
                if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: 'input', data }));
            });
            // Auto-copy on selection: mimics the Linux terminal habit of
            // "select = copied". Lets the user paste back via middle-click
            // without ever pressing Ctrl+C (which stays the Claude interrupt).
            // Writes to the CLIPBOARD (the X11 PRIMARY selection is not
            // reachable from browser JS), best-effort and silent on failure.
            term.onSelectionChange(() => {
                const sel = term.getSelection();
                if (sel && navigator.clipboard) {
                    navigator.clipboard.writeText(sel).catch(() => { /* ignore */ });
                }
            });
            // Focus once the browser has painted the just-shown terminal, so
            // keystrokes land in the PTY immediately -- but only if this tab is
            // still the active one by then.
            requestAnimationFrame(() => { if (this.claudeView === taskId) term.focus(); });
        },

        // Fit a tab's terminal to its (now visible) host and tell the PTY the new
        // size. No-op for a tab whose terminal isn't built yet / is hidden.
        _fitAndSync(id) {
            const s = _claudeTerms.get(id);
            if (!s) return;
            try { s.fit.fit(); } catch (_e) { return; }
            if (s.ws.readyState === WebSocket.OPEN) {
                s.ws.send(JSON.stringify({ type: 'resize', cols: s.term.cols, rows: s.term.rows }));
            }
        },

        // Drop one tab's on-screen terminal + socket. The *server* PTY keeps
        // running, so reopening reattaches (or a stopped/exited one is reaped).
        _teardownTerm(id) {
            const s = _claudeTerms.get(id);
            if (!s) return;
            try { s.ws.close(); } catch (_e) { /* already closing */ }
            try { s.term.dispose(); } catch (_e) { /* already disposed */ }
            _claudeTerms.delete(id);
        },

        // Drop a tab immediately (tear down its terminal + remove it) and move to
        // a neighbor or the board. Used when a session has ended (done/stop) so
        // the strip updates without waiting for the next session poll.
        _dropTab(id) {
            const idx = this.claudeTabs.findIndex(t => t.taskId === id);
            this._teardownTerm(id);
            if (idx >= 0) this.claudeTabs.splice(idx, 1);
            if (this.claudeView !== id) return;
            const next = this.claudeTabs[idx] || this.claudeTabs[idx - 1] || null;
            if (next) this.activateTab(next.taskId);
            else location.hash = '#/';
        },

        // Back to the board. Sessions + their tabs keep streaming in the
        // background; reopening any run re-shows the strip.
        backFromClaudeRun() {
            location.hash = '#/';
            this.loadClaudeSessions();
        },

        // Ask the server to terminate the active session (kills the process group).
        stopClaudeRun() {
            const s = _claudeTerms.get(this.claudeView);
            if (s && s.ws.readyState === WebSocket.OPEN) s.ws.send(JSON.stringify({ type: 'stop' }));
        },

        // Mark the active task done straight from the run header. The PATCH tears
        // down the server-side session (the reaper stops it on status=done), so
        // we just drop its tab and refresh.
        async markDoneFromClaudeRun() {
            const id = this.claudeView;
            if (id == null) return;
            const r = await this.patch(id, { status: 'done' });
            if (!r || !r.ok) return;
            this._dropTab(id);
            await this.refreshAll();
            this.loadClaudeSessions();
        },
    };
}
