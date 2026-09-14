# Voice input -- research notes

Status: research only, nothing implemented. Goal: dictate into the task description field with live
transcription -- text appears while speaking, tentative words may still be corrected by later context (the
behaviour of Google dictation or Claude Code's voice mode). Fully local, no cloud, German as the primary language.

## Architecture

Browser mic -> WebSocket -> local Python backend (FastAPI) -> streaming STT -> `partial` / `final` messages back.
The browser captures 16 kHz mono PCM via an `AudioWorklet` (no `MediaRecorder`, which would produce Opus and
need decoding server-side). The UI renders confirmed text plus a grey tentative tail.

Rejected: the Web Speech API (Chrome-only, ships audio to Google), pure in-browser Whisper (model download per
client, slow), cloud STT (external, paid). The Claude API has no public speech-to-text endpoint.

## Streaming flavours

- **Native streaming**: the model emits incremental, revisable hypotheses per audio frame.
- **Pseudo-streaming**: Whisper re-transcribes a growing window every ~1 s; words identical in two consecutive
  runs are confirmed (`LocalAgreement`), the rest stays tentative. Feels nearly the same at 0.5-1 s windows.

## Candidates (September 2026)

| Candidate | Streaming | German | CPU | Licence | Notes |
|---|---|---|---|---|---|
| Vosk (Kaldi) | native | yes, WER ~10-14 % | very light | Apache-2.0 | stable, quality behind modern models |
| WhisperLiveKit | pseudo (LocalAgreement) | yes (Whisper) | yes, 0.5-1 s | Apache-2.0 | pip package, WS server + browser demo |
| Nemotron-3.5-ASR-Streaming-0.6B | native, 80-1120 ms chunks | yes (40 langs) | yes, ONNX ~3.8x RT | OpenMDW | new 2026; ONNX wiring by hand |
| Qwen3-ASR 0.6B streaming | native | yes | yes | Apache-2.0 | very new, little field experience |
| Voxtral Mini 4B Realtime | native | yes, explicit | no, >=16 GB GPU | Apache-2.0 | state of the art, GPU only |
| Kyutai STT | native | no (en/fr only) | -- | -- | -- |
| sherpa-onnx Zipformer | native | no German model | -- | Apache-2.0 | -- |
| faster-whisper / whisper.cpp raw | none / chunked | yes | yes | MIT | backend for the Whisper-based options |

## Recommendation

1. **WhisperLiveKit** -- best quality-to-effort ratio, fits the stack, tentative/confirmed handling built in.
2. **Nemotron-3.5-ASR-Streaming (ONNX)** -- if latency matters more than convenience.
3. **Vosk** -- baseline; a throwaway demo exists outside the repo for comparison.

Ship as an optional extra (`ntasker[voice]`) so the base install stays lean.

## Sources

- <https://alphacephei.com/vosk/models>
- <https://github.com/ufal/whisper_streaming>, <https://github.com/ufal/SimulStreaming>
- <https://github.com/QuentinFuxa/WhisperLiveKit>
- <https://huggingface.co/nvidia/nemotron-3.5-asr-streaming-0.6b>,
  <https://github.com/codavidgarcia/nemotron-3.5-asr-streaming-onnx>
- <https://github.com/QwenLM/Qwen3-ASR>
- <https://kyutai.org/stt/>
- <https://github.com/k2-fsa/sherpa-onnx>
