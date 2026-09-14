# Voice input

Plugin `voice` (see [plugins.md](plugins.md)), **opt-in**. Dictate a task description -- in the create form and in
the edit modal -- with speech recognition that runs entirely on this machine. Text appears while you speak: a grey,
revisable hypothesis next to the field, and at each pause the recognised sentence is appended to the description.
Nothing leaves the machine; there is no cloud service involved. Background and alternatives considered:
[voice-input-research.md](voice-input-research.md).

## Setup

```
pip install 'ntasker[voice]'      # or: uv tool install 'ntasker[voice]'
ntasker enable voice
```

The extra pulls in [Vosk](https://alphacephei.com/vosk/) (Apache-2.0, CPU only, no GPU needed). `ntasker enable` /
`ntasker disable` switch any plugin; for an opt-in plugin like this one they write the `plugins_enabled` setting
(`/settings` -> *Plugins* does the same). Without the extra the plugin stays listed but the microphone reports that
`vosk` is missing.

## Models

Setting `voice_model` (`/settings` or `ntasker config set voice_model ...`):

| Value | Effect |
|---|---|
| unset | follows the `language` setting (`de` or `en`), small model of that language |
| language code (`de`, `en-us`, `fr`, ...) | Vosk downloads its small model (~50 MB) into `~/.cache/vosk` on first use |
| path to a directory | an unpacked model from <https://alphacephei.com/vosk/models> |

Larger models are noticeably more accurate (German: `vosk-model-de-0.21`, ~1.9 GB):

```
wget https://alphacephei.com/vosk/models/vosk-model-de-0.21.zip
unzip vosk-model-de-0.21.zip -d ~/.local/share/nTasker/
ntasker config set voice_model ~/.local/share/nTasker/vosk-model-de-0.21
```

ENV `NTASKER_VOICE_MODEL` overrides the setting. The first connection after a change loads the new model (a few
seconds for the large ones); the status shows *Loading the speech model...* meanwhile. One model is kept in memory
per server process.

## Using it

The tray under the description field has one microphone button that works both ways:

- **Hold** it to talk; releasing stops (push-to-talk).
- **Click** it to toggle; click again or press **Esc** to stop.

While live, the tray shows a level meter driven by the microphone and the current hypothesis in grey italics.
Recognised text is appended at the end of the description, separated by a space. Vosk gives lowercase text
without punctuation -- edit as needed.

The browser asks for microphone permission once per origin. Since ntasker binds to `127.0.0.1`, the page counts as a
secure context and `getUserMedia` is available without HTTPS.

## How it works

`voice.js` captures the microphone through an `AudioWorklet` (`pcm-worklet.js`) at 16 kHz mono, converts to int16
PCM and streams ~100 ms chunks over a WebSocket to `/api/voice/ws`. The server feeds them to a `KaldiRecognizer`
and answers with `{"type": "partial", "text"}` after each chunk and `{"type": "final", "text"}` at each pause. On
stop the client sends the text frame `final` so the pending words are flushed before it closes. Vosk calls are
blocking and run in a worker thread; the route 404s while the plugin is off.
