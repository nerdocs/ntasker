// Claude plugin: the subscription's usage limits (5-hour and weekly window)
// in the topbar, merged into tracker(). Fed by /api/claude/usage, which
// answers null without a claude.ai login -- then the widget stays hidden.
//
// Refreshed on a minute tick while the tab is visible, plus whenever Claude
// was actually in contact: a session started or ended (the core polls the
// live-session set every 5s), and when a backgrounded tab comes back --
// browsers throttle its timer, so its numbers would be minutes stale.

(function () {
    'use strict';

    const POLL_MS = 60000;

    function t(key, params) {
        let s = (window.__i18n && window.__i18n[key]) || key;
        if (params) for (const [k, v] of Object.entries(params)) s = s.replace(new RegExp('\\{' + k + '\\}', 'g'), v);
        return s;
    }

    // Same thresholds as the Claude Code statusline: yellow at 50 %, red at 80 %.
    function level(pct) {
        return pct >= 80 ? 'hot' : pct >= 50 ? 'warn' : 'ok';
    }

    // Within a day: the time; further out (the weekly window): weekday + date.
    function resetsAt(iso) {
        if (!iso) return '';
        const d = new Date(iso);
        if (isNaN(d)) return '';
        const soon = d - Date.now() < 24 * 3600 * 1000;
        const when = soon
            ? d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
            : d.toLocaleDateString(undefined, { weekday: 'short', day: '2-digit', month: '2-digit' });
        return ' · ' + t('claude_usage_resets', { when });
    }

    window.ntaskerPlugins.push(function (base) {
        // Chain onto an init a previous mixin may already have registered.
        const prevInit = base.pluginInit;

        return {
            claudeUsage: null,

            // Set up once per component: index.html carries both x-data="tracker()"
            // and x-init="init()", so Alpine runs init() -- and with it every
            // pluginInit -- twice. Without the guard the widget would keep two
            // timers and two watchers, and every refresh would be two requests.
            _claudeUsageStarted: false,

            async pluginInit() {
                if (prevInit) await prevInit.call(this);
                if (this._claudeUsageStarted) return;
                this._claudeUsageStarted = true;
                // Not awaited: the widget must never delay the task list.
                this.loadClaudeUsage();
                setInterval(() => {
                    if (!document.hidden) this.loadClaudeUsage();
                }, POLL_MS);
                document.addEventListener('visibilitychange', () => {
                    if (!document.hidden) this.loadClaudeUsage();
                });
                // A session starting or ending is the moment the numbers move,
                // so ask past the server's cache window right then. The core
                // reassigns the array whenever any part of its session payload
                // changed (titles, projects, ...), so compare the ids and only
                // react to a real start or end.
                let seen = (this.claudeSessions || []).join(',');
                this.$watch('claudeSessions', (ids) => {
                    const sig = (ids || []).join(',');
                    if (sig === seen) return;
                    seen = sig;
                    this.loadClaudeUsage(true);
                });
            },

            async loadClaudeUsage(fresh = false) {
                try {
                    const r = await fetch('/api/claude/usage' + (fresh ? '?fresh=1' : ''));
                    this.claudeUsage = r.ok ? (await r.json()).usage : null;
                } catch {
                    this.claudeUsage = null;
                }
            },

            claudeUsageWindows() {
                const u = this.claudeUsage;
                if (!u) return [];
                const color = { ok: 'bg-green', warn: 'bg-yellow', hot: 'bg-red' };
                return [['five_hour', '5h'], ['seven_day', '7d']]
                    .filter(([key]) => u[key] && u[key].utilization != null)
                    .map(([key, label]) => {
                        const pct = Math.min(100, Math.round(u[key].utilization));
                        const lvl = level(pct);
                        return {
                            key,
                            label: t('claude_usage_' + label),
                            pct,
                            color: color[lvl],
                            text: lvl === 'ok' ? '' : 'is-' + lvl,
                            title: t('claude_usage_' + label + '_title') + resetsAt(u[key].resets_at),
                        };
                    });
            },
        };
    });
})();
