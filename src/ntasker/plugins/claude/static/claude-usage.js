// Claude plugin: the subscription's usage limits (5-hour and weekly window)
// in the topbar, merged into tracker(). Fed by /api/claude/usage, which
// answers null without a claude.ai login -- then the widget stays hidden.

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

            async pluginInit() {
                if (prevInit) await prevInit.call(this);
                // Not awaited: the widget must never delay the task list.
                this.loadClaudeUsage();
                setInterval(() => this.loadClaudeUsage(), POLL_MS);
            },

            async loadClaudeUsage() {
                try {
                    const r = await fetch('/api/claude/usage');
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
