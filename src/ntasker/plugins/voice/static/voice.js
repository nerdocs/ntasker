// Voice plugin: dictation into the description field, merged into tracker().
//
// The mic button works both ways: hold it to talk (release stops), or click
// it to toggle (click again or Esc stops). Audio goes 16 kHz PCM over a
// WebSocket to /api/voice/ws; the server answers with a grey, revisable
// `partial` hypothesis and a `final` text at each pause, which is appended
// to the description of the target ('form' = create form, 'edit' = modal).

(function () {
    'use strict';

    const BARS = 28;         // level history shown as the waveform
    const HOLD_MS = 350;     // a press longer than this is push-to-talk
    const METER_MS = 50;     // waveform refresh interval

    function t(key, params) {
        let s = (window.__i18n && window.__i18n[key]) || key;
        if (params) for (const [k, v] of Object.entries(params)) s = s.replace(new RegExp('\\{' + k + '\\}', 'g'), v);
        return s;
    }

    window.ntaskerPlugins.push(function () {
        // Non-reactive audio handles live outside the Alpine state.
        let ctx = null, stream = null, ws = null, meter = null, peak = 0, downAt = 0;

        return {
            voice: { active: false, target: null, partial: '', status: '', bars: new Array(BARS).fill(0) },

            voiceDown(target) {
                if (this.voice.active) { this.voiceStop(); return; }
                downAt = Date.now();
                this.voiceStart(target);
            },

            // Release after a hold ends the dictation; a short press keeps it
            // running as a toggle.
            voiceUp() {
                if (this.voice.active && downAt && Date.now() - downAt > HOLD_MS) this.voiceStop();
                downAt = 0;
            },

            voiceToggle(target) {
                if (this.voice.active) this.voiceStop(); else this.voiceStart(target);
            },

            async voiceStart(target) {
                const v = this.voice;
                v.active = true;
                v.target = target;
                v.partial = '';
                v.status = t('voice_loading');
                v.bars = new Array(BARS).fill(0);
                try {
                    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
                    ws = new WebSocket(`${proto}://${location.host}/api/voice/ws`);
                    ws.binaryType = 'arraybuffer';
                    ws.onmessage = ev => this.voiceMessage(JSON.parse(ev.data));
                    ws.onclose = () => { if (v.active) this.voiceStop(); };
                    await new Promise((ok, fail) => { ws.onopen = ok; ws.onerror = fail; });
                    stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, sampleRate: 16000 } });
                    ctx = new AudioContext({ sampleRate: 16000 });
                    await ctx.audioWorklet.addModule('/static/plugins/voice/pcm-worklet.js');
                    const node = new AudioWorkletNode(ctx, 'pcm-worklet');
                    node.port.onmessage = ev => {
                        if (ev.data.level > peak) peak = ev.data.level;
                        if (ws && ws.readyState === 1) ws.send(ev.data.pcm);
                    };
                    ctx.createMediaStreamSource(stream).connect(node);
                    meter = setInterval(() => {
                        v.bars.push(Math.min(1, peak * 1.6));
                        v.bars.shift();
                        peak = 0;
                    }, METER_MS);
                    this._voiceEsc = ev => { if (ev.key === 'Escape') this.voiceStop(); };
                    window.addEventListener('keydown', this._voiceEsc);
                } catch (e) {
                    const denied = e && (e.name === 'NotAllowedError' || e.name === 'NotFoundError');
                    this.showToast(denied ? t('voice_mic_denied') : t('voice_unavailable', { detail: e && e.message || '' }), 'danger');
                    this.voiceStop();
                }
            },

            voiceMessage(msg) {
                const v = this.voice;
                if (msg.type === 'status') {
                    v.status = msg.text;
                } else if (msg.type === 'partial') {
                    v.partial = msg.text;
                } else if (msg.type === 'final') {
                    v.partial = '';
                    if (msg.text) this.voiceAppend(msg.text);
                } else if (msg.type === 'error') {
                    this.showToast(t('voice_unavailable', { detail: msg.text }), 'danger');
                    this.voiceStop();
                }
            },

            // Append confirmed text to the target's description, one space
            // after whatever is already there.
            voiceAppend(text) {
                const obj = this.voice.target === 'edit' ? this.editing : this.form;
                if (!obj) return;
                const cur = (obj.description || '').replace(/\s+$/, '');
                obj.description = cur ? `${cur} ${text}` : text;
            },

            voiceStop() {
                const v = this.voice;
                if (!v.active) return;
                v.active = false;
                v.partial = '';
                v.status = '';
                clearInterval(meter);
                meter = null;
                window.removeEventListener('keydown', this._voiceEsc);
                if (stream) stream.getTracks().forEach(tr => tr.stop());
                if (ctx) ctx.close();
                stream = ctx = null;
                if (ws) {
                    const sock = ws;
                    ws = null;
                    if (sock.readyState === 1) {
                        // Ask for the pending words, then close once they arrived.
                        sock.onmessage = ev => {
                            const msg = JSON.parse(ev.data);
                            if (msg.type === 'final') {
                                if (msg.text) this.voiceAppend(msg.text);
                                sock.close();
                            }
                        };
                        sock.send('final');
                        setTimeout(() => { if (sock.readyState === 1) sock.close(); }, 3000);
                    } else {
                        sock.close();
                    }
                }
            },
        };
    });
})();
