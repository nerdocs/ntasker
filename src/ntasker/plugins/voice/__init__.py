"""Voice plugin: dictate a task description with local speech recognition.

Opt-in (``default_on=False``): it needs the ``vosk`` package from the
``ntasker[voice]`` extra, and a speech model. The browser streams 16 kHz
PCM from the microphone over a WebSocket (:mod:`~ntasker.plugins.voice.routes`);
Vosk answers with a *partial* hypothesis after every chunk -- shown grey
next to the field and revised as more audio arrives -- and a *final*
result at each pause, which is appended to the description. Nothing
leaves the machine.

The model comes from the ``voice_model`` setting: a language code makes
Vosk download its small model for that language on first use; a path to
an unpacked model directory (from https://alphacephei.com/vosk/models)
uses that one, e.g. a large model for better accuracy.
"""

from __future__ import annotations

import os
import re

from ntasker.i18n import _, _lazy
from ntasker.plugins import PluginContext, PluginSpec
from ntasker.plugins.voice.routes import router

SPEC = PluginSpec(
    name="voice",
    label=_lazy("Voice input"),
    description=_lazy(
        "Dictate task descriptions with local speech recognition (Vosk). "
        "Needs `pip install ntasker[voice]`."
    ),
    default_on=False,
)

#: Vosk language codes look like ``de``, ``en-us``, ``cn``.
_LANG_RE = re.compile(r"^[a-z]{2,3}(-[a-z]{2,4})?$")


def validate_voice_model(value: str) -> str:
    """``voice_model``: a Vosk language code, or the path of an unpacked model dir."""
    value = value.strip()
    if _LANG_RE.match(value):
        return value
    path = os.path.expanduser(value)
    if not os.path.isdir(path):
        raise ValueError(
            _(
                "voice_model must be a Vosk language code (e.g. de, en-us) or the "
                "path of an unpacked model directory; {path!r} is not a directory."
            ).format(path=value)
        )
    return path


def _js_strings() -> dict[str, str]:
    return {
        "voice_dictate": _("Dictate"),
        "voice_hint": _("Hold to talk, click to toggle. Esc stops."),
        "voice_listening": _("Listening..."),
        "voice_loading": _("Loading the speech model..."),
        "voice_stop": _("Stop dictating"),
        "voice_mic_denied": _("Microphone access was refused. Allow it in the browser and try again."),
        "voice_unavailable": _("Voice input is not available: {detail}"),
    }


def register(ctx: PluginContext) -> None:
    ctx.add_router(router)
    ctx.add_setting(
        "voice_model",
        validate_voice_model,
        _lazy(
            "Speech model for dictation. A language code (de, en-us, fr, ...) "
            "downloads Vosk's small model for that language on first use; a path "
            "to an unpacked model directory uses that one instead -- larger "
            "models are more accurate. Download models from "
            "https://alphacephei.com/vosk/models. Unset follows the UI language."
        ),
    )
    ctx.add_js_strings(_js_strings)
    ctx.add_template_slot("head", "voice/templates/head.html")
    ctx.add_template_slot("task_form", "voice/templates/task_form.html")
    ctx.add_template_slot("task_edit", "voice/templates/task_edit.html")
    ctx.add_template_slot("scripts", "voice/templates/scripts.html")
