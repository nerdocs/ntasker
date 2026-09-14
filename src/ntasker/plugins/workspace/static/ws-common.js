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

    // Front matter as a key/value block above the body. Fed through marked
    // it comes out as a rule plus a heading (the closing --- is a setext
    // underline), with every quoted description shown escapes and all.
    function renderFrontMatter(form) {
        const rows = form.fields.map((f) => {
            const value = f.text ? escapeHtml(f.value) : `<code>${escapeHtml(f.value)}</code>`;
            return `<div class="ws-fm-key">${escapeHtml(f.key)}</div><div class="ws-fm-value">${value}</div>`;
        });
        return `<div class="ws-fm">${rows.join('')}</div>`;
    }

    // Decide how a fetched file renders and produce the payload for it.
    // Returns {mode, html, rows} -- mode is markdown | csv | text | none.
    function renderFile(file) {
        if (!file || file.text == null) return { mode: 'none', html: '', rows: [] };
        if (file.kind === 'markdown') {
            const form = splitFrontMatter(file.text);
            const html = form
                ? renderFrontMatter(form) + renderMarkdown(form.body)
                : renderMarkdown(file.text);
            return { mode: 'markdown', html, rows: [] };
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
    // top-level key plus the body, not as one big textarea. Claude Code
    // reads these blocks with a full YAML parser, so scalars are decoded
    // the YAML way for editing (quotes and escapes removed, block scalars
    // unwrapped) and encoded again on save. A field the user did not touch
    // is written back from its original raw text, so an unchanged file
    // round-trips byte for byte no matter how its YAML was styled.
    // Lists, nested mappings (hooks, mcpServers), flow collections and
    // escapes we do not handle are edited as raw YAML instead.

    // Leading `---` block; the `m` flag lets `^` find the closing line.
    const FRONT_MATTER_RE = /^---[ \t]*\r?\n([\s\S]*?)^---[ \t]*(?:\r?\n|$)/m;
    const FIELD_KEY_RE = /^([A-Za-z0-9_.-]+):(.*)$/;
    const DOUBLE_QUOTED_RE = /^"((?:[^"\\]|\\.)*)"$/;
    const SINGLE_QUOTED_RE = /^'((?:[^']|'')*)'$/;
    const UNSUPPORTED_ESCAPE_RE = /\\[^\\"ntr/]/;
    // `|` / `>` header: chomping (+ -) and indentation indicator, any order.
    const BLOCK_HEADER_RE = /^([|>])([+-]?)([1-9]?)([+-]?)[ \t]*(?:#.*)?$/;
    const RAW_START_RE = /^[[{&*!]/;
    // Plain scalars YAML would misread: a leading indicator, a ` #` comment
    // start, a `: ` that opens a nested mapping, a trailing colon.
    const NEEDS_QUOTES_RE = /^(?:[\s"'#&*!|>%@`[\]{},?]|- |: |\? )|\s#|: |:$|\s$/;
    const DECODE = { n: '\n', t: '\t', r: '\r', '"': '"', '\\': '\\', '/': '/' };
    const ENCODE = { '\n': '\\n', '\t': '\\t', '\r': '\\r', '"': '\\"', '\\': '\\\\' };

    // Unwrap a `|` or `>` block scalar. `lines` are the raw continuation
    // lines; returns the text, or null when the block is malformed.
    function decodeBlock(header, lines) {
        const [, kind, chompA, indentDigit, chompB] = header;
        const chomp = chompA || chompB;
        const first = lines.find((l) => l.trim());
        const indent = indentDigit ? Number(indentDigit) : (first ? first.match(/^ */)[0].length : 0);
        if (first && indent === 0) return null;
        const body = [];
        for (const line of lines) {
            if (line.trim() && !line.startsWith(' '.repeat(indent))) return null;
            body.push(line.slice(indent));
        }
        // Trailing blank lines are the chomping's business, not content.
        let trailing = 0;
        while (body.length && !body[body.length - 1].trim()) { body.pop(); trailing++; }

        let text;
        if (kind === '|') {
            text = body.join('\n');
        } else {
            // Folded: a break between two normal lines is a space, blank
            // lines in between count one newline each, and a break next to
            // a more-indented line is kept as a newline (plus the blanks).
            text = '';
            let prev = null;
            let blanks = 0;
            for (const line of body) {
                if (!line.trim()) { blanks++; continue; }
                if (prev === null) text = '\n'.repeat(blanks) + line;
                else if (line.startsWith(' ') || prev.startsWith(' ')) text += '\n'.repeat(blanks + 1) + line;
                else text += (blanks ? '\n'.repeat(blanks) : ' ') + line;
                prev = line;
                blanks = 0;
            }
        }
        if (!body.length) return chomp === '+' ? '\n'.repeat(trailing) : '';
        if (chomp === '+') text += '\n'.repeat(trailing + 1);
        else if (chomp !== '-') text += '\n';
        return text;
    }

    // {value, style} for a scalar the editor can decode as text, else null.
    // style is plain | double | single | literal | folded.
    function decodeScalar(raw) {
        const lines = raw.split('\n');
        const head = lines[0];
        const block = BLOCK_HEADER_RE.exec(head);
        if (block) {
            const value = decodeBlock(block, lines.slice(1));
            return value === null ? null : { value, style: block[1] === '|' ? 'literal' : 'folded' };
        }
        if (lines.length > 1 || RAW_START_RE.test(raw)) return null;
        let match = DOUBLE_QUOTED_RE.exec(raw);
        if (match) {
            if (UNSUPPORTED_ESCAPE_RE.test(match[1])) return null;
            return { value: match[1].replace(/\\(.)/g, (_, c) => DECODE[c]), style: 'double' };
        }
        match = SINGLE_QUOTED_RE.exec(raw);
        if (match) return { value: match[1].replace(/''/g, "'"), style: 'single' };
        if (raw.startsWith('"') || raw.startsWith("'")) return null;
        return { value: raw, style: 'plain' };
    }

    // Multi-line text becomes a `|` block (the documented way to write a
    // long description) unless the field was double-quoted before; a
    // single line stays plain when YAML allows it.
    function encodeScalar(value, style) {
        if (value.includes('\n') && style !== 'double') {
            const trailing = value.length - value.replace(/\n+$/, '').length;
            const lines = value.replace(/\n+$/, '').split('\n');
            const indicator = lines[0].startsWith(' ') ? '2' : '';
            const chomp = trailing === 0 ? '-' : trailing === 1 ? '' : '+';
            const body = lines.map((l) => (l ? '  ' + l : l)).join('\n');
            return `|${indicator}${chomp}\n${body}` + (trailing > 1 ? '\n'.repeat(trailing - 1) : '');
        }
        if (style === 'plain' && !NEEDS_QUOTES_RE.test(value)) return value;
        return '"' + value.replace(/[\n\t\r"\\]/g, (c) => ENCODE[c]) + '"';
    }

    // Returns {preamble, fields, body, newKey}, or null when the text has no
    // front matter. A field is {key, value, text, style, raw, orig}: `text`
    // marks a decoded scalar, `raw` / `orig` are what it was read from so
    // an untouched field is written back unchanged.
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
            field.raw = field.value;
            if (scalar) {
                field.value = scalar.value;
                field.orig = scalar.value;
                field.style = scalar.style;
            }
        }
        return { preamble: preamble.join('\n'), fields, body: text.slice(match[0].length), newKey: '' };
    }

    function joinFrontMatter(form) {
        const lines = [];
        if (form.preamble) lines.push(form.preamble);
        for (const field of form.fields) {
            const key = field.key.trim();
            if (!key) continue;
            let value = field.value;
            if (field.text) value = value === field.orig ? field.raw : encodeScalar(value, field.style);
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
            form.fields.push({ key, value: '', text: true, style: 'plain' });
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
