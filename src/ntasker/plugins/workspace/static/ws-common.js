// Workspace helpers shared by the standalone /workspace page (workspace.js)
// and the sidebar + context picker on the task list (app.js).
//
// Plain functions on a namespace object, no Alpine and no module system --
// ntasker has no build step, so this is loaded with a bare <script> before
// its two consumers and read off `window.ntaskerWs`.

(function (global) {
    'use strict';

    // Tabler icon per file kind, matching workspace.classify() on the server.
    const KIND_ICONS = {
        markdown: 'ti-markdown',
        csv: 'ti-table',
        text: 'ti-file-text',
        pdf: 'ti-file-type-pdf',
        doc: 'ti-file-type-doc',
        sheet: 'ti-file-spreadsheet',
        slides: 'ti-presentation',
        image: 'ti-photo',
        folder: 'ti-folder',
        other: 'ti-file',
    };

    // Icon per context kind (what an attachment points at).
    const CONTEXT_ICONS = {
        member: 'ti-user',
        skill: 'ti-puzzle',
        note: 'ti-book',
        doc: 'ti-file-text',
        file: 'ti-file',
        folder: 'ti-folder',
        brain: 'ti-brain',
        mcp: 'ti-plug-connected',
    };

    // Empty inventory used until the first fetch lands, so every template
    // expression can dereference .exists / .skills without guards.
    function emptyInventory() {
        const section = { configured: false, exists: false, path: '' };
        return {
            skills: { ...section, skills: [], total: 0, loading: 0, broken: 0 },
            wiki: { ...section, areas: [], indexes: [], total_notes: 0 },
            team: { ...section, members: [], total: 0 },
            docs: { ...section, docs: [], total: 0, kinds: {} },
            tooling: {
                config_found: false, config_path: '',
                servers: [], tools: [], missing_runtimes: [],
            },
        };
    }

    function iconFor(kind) {
        return KIND_ICONS[kind] || KIND_ICONS.other;
    }

    function contextIcon(kind) {
        return CONTEXT_ICONS[kind] || 'ti-paperclip';
    }

    // HTML-escape via the DOM, so there is exactly one escaping
    // implementation and it is the browser's.
    function escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text == null ? '' : String(text);
        return div.innerHTML;
    }

    function fmtSize(bytes) {
        if (!bytes) return '0 B';
        const units = ['B', 'kB', 'MB', 'GB'];
        const i = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
        const value = bytes / Math.pow(1024, i);
        return `${value < 10 && i > 0 ? value.toFixed(1) : Math.round(value)} ${units[i]}`;
    }

    function fmtDate(epochSeconds) {
        if (!epochSeconds) return '';
        return new Date(epochSeconds * 1000).toISOString().slice(0, 10);
    }

    // Case-insensitive substring match across the given fields.
    function filterItems(items, query, fields) {
        const q = (query || '').trim().toLowerCase();
        if (!q) return items || [];
        return (items || []).filter((item) =>
            fields.some((f) => String(item[f] || '').toLowerCase().includes(q))
        );
    }

    // marked produces raw HTML; DOMPurify strips anything active before it
    // reaches x-html. Never skip that step -- a note pasted from the web can
    // carry a <script>. Falls back to escaped plain text if either library
    // is missing, which is also what happens with the CDN blocked.
    function renderMarkdown(text) {
        if (!global.marked) return `<pre>${escapeHtml(text)}</pre>`;
        const raw = global.marked.parse(text);
        return global.DOMPurify ? global.DOMPurify.sanitize(raw) : escapeHtml(text);
    }

    // Minimal RFC-4180 reader: quoted fields, doubled quotes, and newlines
    // inside quotes. The generated exports use ';' (Excel-DE) as often as
    // ',', so the delimiter is sniffed from the header when not given.
    function parseDelimited(text, delimiter) {
        const sample = text.slice(0, 4096).split('\n')[0] || '';
        const delim = delimiter || ((sample.split(';').length > sample.split(',').length) ? ';' : ',');

        const rows = [];
        let row = [];
        let field = '';
        let quoted = false;

        for (let i = 0; i < text.length; i++) {
            const ch = text[i];
            if (quoted) {
                if (ch === '"') {
                    if (text[i + 1] === '"') { field += '"'; i++; }
                    else quoted = false;
                } else field += ch;
                continue;
            }
            if (ch === '"') { quoted = true; continue; }
            if (ch === delim) { row.push(field); field = ''; continue; }
            if (ch === '\n') {
                row.push(field.replace(/\r$/, ''));
                rows.push(row);
                row = []; field = '';
                continue;
            }
            field += ch;
        }
        if (field !== '' || row.length) {
            row.push(field.replace(/\r$/, ''));
            rows.push(row);
        }
        return rows.filter((r) => r.some((c) => c !== ''));
    }

    // Decide how a fetched file renders and produce the payload for it.
    // Returns {mode, html, rows} -- mode is markdown | csv | text | none.
    function renderFile(file) {
        if (!file || file.text == null) return { mode: 'none', html: '', rows: [] };
        if (file.kind === 'markdown') {
            return { mode: 'markdown', html: renderMarkdown(file.text), rows: [] };
        }
        if (file.kind === 'csv') {
            const rows = parseDelimited(file.text, file.suffix === 'tsv' ? '\t' : null);
            return { mode: rows.length ? 'csv' : 'text', html: '', rows };
        }
        return { mode: 'text', html: '', rows: [] };
    }

    // ---- front matter editing ----------------------------------------
    //
    // A Markdown file with a leading `---` block is edited as one field per
    // top-level key plus the body, not as one big textarea. Nothing in the
    // block is interpreted: a field's value is the raw text after `key:` up
    // to the next top-level key -- block scalars, nested mappings, list
    // items and comments included -- so joinFrontMatter() reproduces
    // whatever YAML we did not understand byte for byte.

    // Leading `---` block; the `m` flag lets `^` find the closing line.
    const FRONT_MATTER_RE = /^---[ \t]*\r?\n([\s\S]*?)^---[ \t]*(?:\r?\n|$)/m;
    const FIELD_KEY_RE = /^([A-Za-z0-9_.-]+):(.*)$/;

    // A single-line scalar is edited as text: a quoted one is unescaped so
    // the field shows the words, not the YAML around them, and gets quoted
    // again on save. Escapes we do not handle (\x41, \u00e9) keep the raw
    // form so a save cannot change their meaning. Block scalars, nested
    // mappings, lists and flow collections stay raw as well.
    const DOUBLE_QUOTED_RE = /^"((?:[^"\\]|\\.)*)"$/;
    const SINGLE_QUOTED_RE = /^'((?:[^']|'')*)'$/;
    const UNSUPPORTED_ESCAPE_RE = /\\[^\\"ntr/]/;
    const RAW_START_RE = /^[>|[{&*!]/;
    // Plain scalars YAML would misread: a leading indicator, a newline, a
    // ` #` comment start, a trailing colon. A mid-string `: ` is left alone
    // on purpose -- skill descriptions are full of them and Claude Code
    // reads those files fine, so quoting would only churn the file.
    const NEEDS_QUOTES_RE = /^(?:[\s"'#&*!|>%@`[\]{},?]|- |: |\? )|\n|\s#|:$|\s$/;
    const DECODE = { n: '\n', t: '\t', r: '\r', '"': '"', '\\': '\\', '/': '/' };
    const ENCODE = { '\n': '\\n', '\t': '\\t', '\r': '\\r', '"': '\\"', '\\': '\\\\' };

    // {value, quoted} for a scalar the editor may decode, else null (raw).
    function decodeScalar(raw) {
        if (raw.includes('\n') || RAW_START_RE.test(raw)) return null;
        let match = DOUBLE_QUOTED_RE.exec(raw);
        if (match) {
            if (UNSUPPORTED_ESCAPE_RE.test(match[1])) return null;
            return { value: match[1].replace(/\\(.)/g, (_, c) => DECODE[c]), quoted: true };
        }
        match = SINGLE_QUOTED_RE.exec(raw);
        if (match) return { value: match[1].replace(/''/g, "'"), quoted: true };
        if (raw.startsWith('"') || raw.startsWith("'")) return null;
        return { value: raw, quoted: false };
    }

    function encodeScalar(value, quoted) {
        if (!quoted && !NEEDS_QUOTES_RE.test(value)) return value;
        return '"' + value.replace(/[\n\t\r"\\]/g, (c) => ENCODE[c]) + '"';
    }

    // Returns {preamble, fields: [{key, value, text, quoted}], body, newKey},
    // or null when the text has no front matter. `text` marks a decoded
    // scalar (see decodeScalar); the others are edited verbatim.
    function splitFrontMatter(text) {
        const match = FRONT_MATTER_RE.exec(text || '');
        if (!match) return null;
        const fields = [];
        const preamble = [];
        let current = null;
        const block = match[1].replace(/\r?\n$/, '');
        for (const line of block ? block.split(/\r?\n/) : []) {
            const key = FIELD_KEY_RE.exec(line);
            if (key) {
                current = { key: key[1], value: key[2].replace(/^ /, '') };
                fields.push(current);
            } else if (current) {
                current.value += '\n' + line;
            } else {
                preamble.push(line);
            }
        }
        for (const field of fields) {
            const scalar = decodeScalar(field.value);
            field.text = !!scalar;
            field.quoted = !!scalar && scalar.quoted;
            if (scalar) field.value = scalar.value;
        }
        return { preamble: preamble.join('\n'), fields, body: text.slice(match[0].length), newKey: '' };
    }

    function joinFrontMatter(form) {
        const lines = [];
        if (form.preamble) lines.push(form.preamble);
        for (const field of form.fields) {
            const key = field.key.trim();
            const value = field.text ? encodeScalar(field.value, field.quoted) : field.value;
            if (!key) continue;
            if (value === '') lines.push(`${key}:`);
            else if (value.startsWith('\n')) lines.push(`${key}:${value}`);
            else lines.push(`${key}: ${value}`);
        }
        const block = lines.join('\n');
        return '---\n' + (block ? block + '\n' : '') + '---\n' + form.body;
    }

    function addField(form) {
        const key = (form.newKey || '').trim();
        if (key && !form.fields.some((f) => f.key === key)) {
            form.fields.push({ key, value: '', text: true, quoted: false });
        }
        form.newKey = '';
    }

    // Textarea height for a field: one row per line, more for a long
    // single-line value (quoted descriptions run to thousands of chars).
    function fieldRows(field) {
        const value = field.value || '';
        return Math.min(12, Math.max(value.split('\n').length, Math.ceil(value.length / 90), 1));
    }

    // Suffixes the server will accept a write for (workspace.EDITABLE).
    const EDITABLE_SUFFIXES = ['md', 'markdown', 'txt', 'csv', 'tsv', 'json', 'log'];

    function isEditable(file) {
        return !!file && EDITABLE_SUFFIXES.includes(String(file.suffix || '').toLowerCase());
    }

    // Pull the server's `detail` out of a failed response; falls back to the
    // given already-translated message.
    async function errorDetail(response, fallback) {
        try {
            const body = await response.json();
            if (body && typeof body.detail === 'string') return body.detail;
        } catch (e) {
            // Non-JSON body (proxy error page, empty 500) -- use the fallback.
        }
        return fallback;
    }

    // Translate with {placeholder} substitution against window.__i18n.
    function t(key, params) {
        let s = (global.__i18n && global.__i18n[key]) || key;
        if (params) {
            for (const [k, v] of Object.entries(params)) {
                s = s.replace(new RegExp('\\{' + k + '\\}', 'g'), v);
            }
        }
        return s;
    }

    global.ntaskerWs = {
        KIND_ICONS, CONTEXT_ICONS, EDITABLE_SUFFIXES,
        emptyInventory, iconFor, contextIcon, escapeHtml, fmtSize, fmtDate,
        filterItems, renderMarkdown, parseDelimited, renderFile, isEditable,
        splitFrontMatter, joinFrontMatter, addField, fieldRows,
        errorDetail, t,
    };
})(window);
