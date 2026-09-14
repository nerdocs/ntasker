// Microphone frames -> 16 kHz int16 PCM chunks for the voice WebSocket.
//
// Runs on the audio thread. Float32 samples are converted to int16, batched
// to ~100 ms (1600 samples) so the socket sees ~10 messages a second, and
// posted together with the peak level of the batch (0..1) for the meter.
class PcmWorklet extends AudioWorkletProcessor {
    constructor() {
        super();
        this.buf = new Int16Array(1600);
        this.len = 0;
        this.peak = 0;
    }

    process(inputs) {
        const ch = inputs[0][0];
        if (!ch) return true;
        for (let i = 0; i < ch.length; i++) {
            const s = Math.max(-1, Math.min(1, ch[i]));
            const a = Math.abs(s);
            if (a > this.peak) this.peak = a;
            this.buf[this.len++] = s < 0 ? s * 0x8000 : s * 0x7fff;
            if (this.len === this.buf.length) {
                this.port.postMessage({ pcm: this.buf.buffer.slice(0), level: this.peak });
                this.len = 0;
                this.peak = 0;
            }
        }
        return true;
    }
}
registerProcessor('pcm-worklet', PcmWorklet);
