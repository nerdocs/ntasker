# Voice input

Plugin `voice` (see [plugins.md](plugins.md)), **opt-in**. Dictate a task description -- in the create form and in
the edit modal -- with speech recognition that runs entirely on this machine. Text appears while you speak: a grey,
revisable hypothesis next to the field, and at each pause the recognised sentence is appended to the description.
Nothing leaves the machine; there is no cloud service involved. Background and alternatives considered:
[voice-input-research.md](voice-input-research.md).

## Setup

```
ntasker enable voice
```

That is all: `enable` installs the missing packages of the `ntasker[voice]` extra --
[Vosk](https://alphacephei.com/vosk/) (Apache-2.0, CPU only) -- into ntasker's own environment, the way
`ntasker self-update` upgrades the package (pip if the interpreter has it, else `uv pip`), and switches the plugin
on. `/settings` -> *Plugins* toggles it too, but
cannot install packages; the card then says to run the command. `ntasker disable voice` switches it off.

Note for `uv tool` installs: `uv tool upgrade ntasker` re-resolves from the tool's receipt and drops packages added
this way -- `uv tool install 'ntasker[voice]'` records the extra durably.

## Models

`/settings` -> *Plugins* -> *Voice input* card. It lists the installed models (pick one with the radio button) and
Vosk's catalog, filtered to the UI language by default. *Download* fetches the model in the background -- ntasker
streams the zip, verifies the catalog's MD5, unpacks and deletes the archive; the card shows the progress and the model
is selected once it is ready (first one installed becomes the default). Store:
`platformdirs.user_data_dir("nTasker")/vosk-models/<name>` (Linux: `~/.local/share/nTasker/vosk-models/`).

Small models (~50 MB) answer fastest; the big ones (1-2 GB, e.g. `vosk-model-de-0.21`) are noticeably more
accurate.

Setting `voice_model` holds the choice: the name of an installed model, or the path of an unpacked Vosk model
directory from elsewhere (`ntasker config set voice_model ~/models/vosk-model-de-0.21`). Unset uses an installed
model, preferring the UI language's. ENV `NTASKER_VOICE_MODEL` overrides. The first connection after a change
loads the new model (a few seconds for the big ones); the tray shows *Loading the speech model...* meanwhile. One
model is kept in memory per server process.

API: `GET /api/voice/models` (package status, store dir, installed, catalog, current download),
`POST /api/voice/models/{name}` (start a download, 202; 409 while one runs), `GET /api/voice/models/job` (poll).

## Using it

The tray under the description field has one microphone button that works both ways:

- **Hold** it to talk; releasing stops (push-to-talk).
- **Click** it to toggle; click again or press **Esc** to stop.

While live, the tray shows a level meter driven by the microphone and the current hypothesis in grey italics.
Recognised text is appended at the end of the description.

### Spoken punctuation

Vosk itself returns lowercase words without punctuation, so marks are dictated the way classic dictation software
expects, per model language (`src/ntasker/plugins/voice/punct.py`; the language comes from the model's name, e.g.
`vosk-model-de-0.21`, else from the `language` setting):

| German | English | Result |
|---|---|---|
| Punkt | period, full stop | `.` |
| Komma | comma | `,` |
| Doppelpunkt | colon | `:` |
| Strichpunkt, Semikolon | semicolon | `;` |
| Fragezeichen | question mark | `?` |
| Rufzeichen, Ausrufezeichen | exclamation mark, exclamation point | `!` |
| neue Zeile | new line | line break |
| Absatz, neuer Absatz | new paragraph | empty line |

The mark is glued to the preceding word, the word after `.`, `?`, `!` or a line break is capitalised, and so is
the first word of a segment that starts a sentence. A genuine "Punkt" or "period" in a sentence is replaced too --
type it instead.

The browser asks for microphone permission once per origin. Since ntasker binds to `127.0.0.1`, the page counts as a
secure context and `getUserMedia` is available without HTTPS.

## How it works

`voice.js` captures the microphone through an `AudioWorklet` (`pcm-worklet.js`) at 16 kHz mono, converts to int16
PCM and streams ~100 ms chunks over a WebSocket to `/api/voice/ws`. The server feeds them to a `KaldiRecognizer`
and answers with `{"type": "partial", "text"}` after each chunk and `{"type": "final", "text"}` at each pause. On
stop the client sends the text frame `final` so the pending words are flushed before it closes. Without the
package or a model the server answers `{"type": "error", "code": "no_vosk" | "no_model"}` and the toast offers a
jump to the settings. Vosk calls are blocking and run in a worker thread; the routes 404 while the plugin is off.
