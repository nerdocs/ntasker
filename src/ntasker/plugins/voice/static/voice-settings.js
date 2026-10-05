// Voice input card on /settings: installed models, catalog, background download.
//
// A nested Alpine component inside settingsPage(). It talks to
// /api/voice/models directly: picking a model writes the voice_model
// setting, "Download" starts a server-side download and the card polls
// the job until it is done, then refreshes the installed list. Deleting a
// model offers an Undo while the server keeps it in its trash.

function voiceSettings() {
    'use strict';

    const t = (key, params) => {
        let s = (window.__i18n && window.__i18n[key]) || key;
        if (params) for (const [k, v] of Object.entries(params)) s = s.replace(new RegExp('\\{' + k + '\\}', 'g'), v);
        return s;
    };

    return {
        loaded: false,
        info: { engines: {}, dirs: {}, current: null, installed: [], catalog: [], catalog_error: null },
        job: null,
        deleted: null,      // { name, was_current } while Undo is offered
        _undoTimer: null,
        lang: '',
        error: '',
        _timer: null,

        async init() {
            await this.refresh();
            // Preselect the UI language once the <option>s exist (x-model
            // cannot pick a value that is not rendered yet).
            const lang = (document.documentElement.lang || '').slice(0, 2);
            await this.$nextTick();
            this.lang = this.info.catalog.some(m => m.lang === lang) ? lang : '';
            if (this.busy()) this.poll();
        },

        async refresh() {
            try {
                const r = await fetch('/api/voice/models');
                if (r.ok) {
                    this.info = await r.json();
                    this.job = this.info.job;
                }
            } catch (e) {
                // Leave the card as it is; the next action retries.
            }
            this.loaded = true;
        },

        languages() {
            const seen = new Map();
            for (const m of this.info.catalog) if (m.lang && !seen.has(m.lang)) seen.set(m.lang, m.lang_text);
            return [...seen].map(([code, text]) => ({ code, text })).sort((a, b) => a.text.localeCompare(b.text));
        },

        filtered() {
            // Multilingual (Whisper) entries carry no language and always show.
            return this.info.catalog.filter(m => !this.lang || !m.lang || m.lang === this.lang);
        },

        isInstalled(name) {
            return this.info.installed.some(m => m.name === name);
        },

        busy() {
            return !!this.job && (this.job.state === 'downloading' || this.job.state === 'unpacking');
        },

        percent() {
            if (!this.job || !this.job.total) return 0;
            return Math.min(100, Math.round(this.job.received / this.job.total * 100));
        },

        jobText() {
            const j = this.job;
            if (!j) return '';
            if (j.state === 'downloading') return t('voice_downloading', { name: j.name }) + ` ${this.percent()}%`;
            if (j.state === 'unpacking') return t('voice_unpacking', { name: j.name });
            if (j.state === 'done') return t('voice_download_done', { name: j.name });
            return t('voice_download_failed', { detail: j.error || '' });
        },

        async use(name) {
            this.error = '';
            const r = await fetch('/api/settings/voice_model', {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ value: name }),
            });
            if (!r.ok) {
                const body = await r.json().catch(() => ({}));
                this.error = body.detail || r.statusText;
            }
            await this.refresh();
        },

        async remove(name) {
            this.error = '';
            const r = await fetch(`/api/voice/models/${encodeURIComponent(name)}`, { method: 'DELETE' });
            const body = await r.json().catch(() => ({}));
            if (!r.ok) {
                this.error = body.detail || r.statusText;
                return;
            }
            this.deleted = { name, was_current: body.was_current };
            // The server purges its trash after a minute; offer Undo a bit shorter.
            clearTimeout(this._undoTimer);
            this._undoTimer = setTimeout(() => { this.deleted = null; }, 50000);
            await this.refresh();
        },

        async restore() {
            const d = this.deleted;
            this.deleted = null;
            clearTimeout(this._undoTimer);
            const r = await fetch(`/api/voice/models/${encodeURIComponent(d.name)}/restore`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ use: d.was_current }),
            });
            if (!r.ok) {
                const body = await r.json().catch(() => ({}));
                this.error = body.detail || r.statusText;
            }
            await this.refresh();
        },

        async download(name) {
            this.error = '';
            const r = await fetch(`/api/voice/models/${encodeURIComponent(name)}`, { method: 'POST' });
            if (!r.ok) {
                const body = await r.json().catch(() => ({}));
                this.error = body.detail || r.statusText;
                return;
            }
            this.job = (await r.json()).job;
            this.poll();
        },

        poll() {
            clearTimeout(this._timer);
            this._timer = setTimeout(async () => {
                const r = await fetch('/api/voice/models/job');
                if (r.ok) this.job = (await r.json()).job;
                if (this.busy()) this.poll(); else await this.refresh();
            }, 1000);
        },
    };
}
