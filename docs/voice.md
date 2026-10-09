# Voice input

Plugin `voice` (see [plugins.md](plugins.md)), **opt-in**. Dictate a task description -- in the create form, in
the edit modal and in the inbox field at the top -- with speech recognition that runs entirely on this machine. Text
appears while you speak: a grey, revisable hypothesis next to the field, and at each pause the recognised sentence is
appended to the description.
Nothing leaves the machine; there is no cloud service involved. Background and alternatives considered:
[voice-input-research.md](voice-input-research.md).

## Setup

```
ntasker enable voice
```

or `/settings` -> *Plugins* -> switch *Voice input* on and press *Install now* on its card. Either installs the missing
packages of the `ntasker[voice]` extra -- [Vosk](https://alphacephei.com/vosk/) (Apache-2.0, CPU only) and
[faster-whisper](https://github.com/SYSTRAN/faster-whisper) (MIT) -- into ntasker's own environment, the way `ntasker
self-update` upgrades the package (pip if the interpreter has it, else `uv pip`). `ntasker disable voice` or the switch
turns it off again.

In a `uv tool` home the install runs `uv tool install 'ntasker[voice]'` instead, which records the extra in the tool's
receipt so `uv tool upgrade ntasker` keeps it -- and which replaces ntasker's own files, so restart the server
afterwards (the card says so; *Maintenance* -> *Restart server*).

## Engines

Two speech recognition engines; the selected model decides which one runs.

| | Vosk | Whisper (faster-whisper) |
|---|---|---|
| Models | one per language, ~50 MB to ~2 GB | multilingual, `tiny` (~75 MB) to `large-v3` (~3 GB) |
| Live text | streams: a hypothesis after every chunk | re-transcribes the utterance about once a second |
| Punctuation | none -- dictate it (see below) | sets punctuation and capitals by itself |
| CPU | very light | heavier; `tiny`/`base`/`small` are fine on a laptop CPU |

Whisper runs on the CPU with int8 weights (`device="cpu"`, `compute_type="int8"`). It transcribes whole utterances:
while the microphone level says someone speaks, the audio is buffered; a pause of 0.7 s (or 25 s of speech)
transcribes it and appends the text. During speech the buffer is re-transcribed for the grey hypothesis -- after at
least 1 s of new audio, and the slower the model, the rarer (twice the time the last pass took). Its silence filter
(`vad_filter`) keeps breath and noise from turning into invented text. The language comes from the `language`
setting (`de`/`en`); with `auto` Whisper detects it itself.

## Models

`/settings` -> *Plugins* -> *Voice input* card. It lists the installed models (pick one with the radio button,
delete one with the trash button) and the catalog: Whisper's models (always shown, they cover every language),
then Vosk's catalog, filtered to the UI language by default. *Download* fetches the model in the background; the
card shows the progress and the model is selected once it is ready (first one installed becomes the default).

- **Vosk**: ntasker streams the zip, verifies the catalog's MD5, unpacks and deletes the archive. Store:
  `platformdirs.user_data_dir("nTasker")/vosk-models/<name>` (Linux: `~/.local/share/nTasker/vosk-models/`).
- **Whisper**: the CTranslate2 conversions on Hugging Face (`Systran/faster-whisper-*`,
  `mobiuslabsgmbh/faster-whisper-large-v3-turbo`); ntasker fetches the model files into `<name>.part/`, verifies
  each weight file's SHA-256 against the repository listing and renames the directory into place. Store:
  `.../nTasker/whisper-models/whisper-<size>`.

Small models answer fastest; the big ones (e.g. `vosk-model-de-0.21`, `whisper-large-v3-turbo`) are noticeably
more accurate.

**Deleting** moves the model into the store's `.trash/` and the card offers *Undo*; a minute later (or on the next
delete or download) the trash is emptied for good. Deleting the selected model unsets `voice_model`; *Undo*
selects it again.

Setting `voice_model` holds the choice: the name of an installed model, or the path of an unpacked Vosk or
faster-whisper model directory from elsewhere (`ntasker config set voice_model ~/models/vosk-model-de-0.21`). Unset uses
an installed model, preferring the UI language's. ENV `NTASKER_VOICE_MODEL` overrides. The first connection after a
change loads the new model (a few seconds for the big ones); the tray shows *Loading the speech model...* meanwhile. One
model is kept in memory per server process.

API: `GET /api/voice/models` (engine packages, store dirs, installed, catalog, current download),
`POST /api/voice/models/{name}` (start a download, 202; 409 while one runs), `GET /api/voice/models/job` (poll),
`DELETE /api/voice/models/{name}` (to the trash; `{was_current}`), `POST /api/voice/models/{name}/restore`
(Undo; body `{"use": true}` selects it again; 404 once purged).

## Using it

The tray under the description field has one microphone button that works both ways:

- **Hold** it to talk; releasing stops (push-to-talk).
- **Click** it to toggle; click again or press **Esc** to stop.

While live, the tray shows a level meter driven by the microphone and the current hypothesis in grey italics.
Recognised text is appended at the end of the description.

The inbox field in the navbar (see [inbox.md](inbox.md)) has the same microphone as an input-group button on its
left. The navbar has no room for a tray, so the button itself turns red while live and the meter plus hypothesis
drop below the field; recognised text is appended to the field, Enter sends the note as usual.

The reply line of a run's conversation view (see [claude-runs.md](claude-runs.md)) has the same button left of the field; the meter
and hypothesis show above the line. Recognised text is appended to the reply, which is sent with Enter or *Send*.

### Spoken punctuation

Applies to Vosk models only -- Whisper punctuates by itself. Vosk returns lowercase words without punctuation, so marks
are dictated the way classic dictation software expects, per model language (`src/ntasker/plugins/voice/punct.py`; the
language comes from the model's name, e.g. `vosk-model-de-0.21`, else from the `language` setting):

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
PCM and streams ~100 ms chunks over a WebSocket to `/api/voice/ws`. The server feeds them to the engine's recognizer
(`VoskRecognizer` / `WhisperRecognizer` in `routes.py`, both with `accept(chunk)` and `flush()`) and answers with
`{"type": "partial", "text"}` and `{"type": "final", "text"}` at each pause. On stop the client sends the text frame
`final` so the pending words are flushed before it closes (it waits up to 15 s -- Whisper transcribes the last
utterance only then). Without a model or the model's engine package the server answers
`{"type": "error", "code": "no_model" | "no_engine"}` and the toast offers a jump to the settings. Engine calls are
blocking and run in a worker thread; the routes 404 while the plugin is off.
