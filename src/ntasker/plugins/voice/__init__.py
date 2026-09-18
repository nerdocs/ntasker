"""Voice plugin: dictate a task description with local speech recognition.

Opt-in (``default_on=False``): it needs the ``vosk`` package from the
``ntasker[voice]`` extra -- ``ntasker enable voice`` installs it -- and a
speech model from the store (:mod:`~ntasker.plugins.voice.models`),
downloaded from Vosk's catalog on the /settings card. The browser streams
16 kHz PCM from the microphone over a WebSocket
(:mod:`~ntasker.plugins.voice.routes`); Vosk answers with a *partial*
hypothesis after every chunk -- shown grey next to the field and revised
as more audio arrives -- and a *final* result at each pause, which is
appended to the description. Nothing leaves the machine.

The ``voice_model`` setting names the model to use: an installed model's
name, or the path of an unpacked model directory from elsewhere. Unset
uses an installed model, preferring the UI language's.
"""

from __future__ import annotations

from ntasker.i18n import _, _lazy
from ntasker.plugins import PluginContext, PluginSpec
from ntasker.plugins.voice import models
from ntasker.plugins.voice.routes import router

SPEC = PluginSpec(
    name="voice",
    label=_lazy("Voice input"),
    description=_lazy(
        "Dictate task descriptions with local speech recognition (Vosk), "
        "fully offline. Needs the ntasker[voice] extra -- install it on this "
        "card or via `ntasker enable voice`."
    ),
    default_on=False,
    extra="voice",
    icon="ti-microphone",
)


def validate_voice_model(value: str) -> str:
    """``voice_model``: the name of an installed model, or a model directory path."""
    value = value.strip()
    if models.resolve(value) is None:
        raise ValueError(
            _(
                "voice_model must name an installed model or the path of an unpacked "
                "Vosk model directory; {value!r} is neither."
            ).format(value=value)
        )
    return value


def _js_strings() -> dict[str, str]:
    return {
        "voice_dictate": _("Dictate"),
        "voice_hint": _("Hold to talk, click to toggle. Esc stops."),
        "voice_listening": _("Listening..."),
        "voice_loading": _("Loading the speech model..."),
        "voice_stop": _("Stop dictating"),
        "voice_mic_denied": _("Microphone access was refused. Allow it in the browser and try again."),
        "voice_unavailable": _("Voice input is not available: {detail}"),
        "voice_open_settings": _("Open settings"),
        # settings card
        "voice_vosk_missing": _(
            "The speech recognition package is not installed -- use the install button above."
        ),
        "voice_installed": _("Installed models"),
        "voice_none_installed": _("No model installed yet -- download one below."),
        "voice_in_use": _("in use"),
        "voice_use": _("Use"),
        "voice_catalog": _("Download a model"),
        "voice_catalog_intro": _(
            "Models from the Vosk catalog. Small models (~50 MB) answer fastest; the big "
            "ones (1-2 GB) are noticeably more accurate. ntasker downloads in the "
            "background into {dir}."
        ),
        "voice_catalog_offline": _("The catalog could not be loaded: {detail}"),
        "voice_language": _("Language"),
        "voice_all_languages": _("All languages"),
        "voice_download": _("Download"),
        "voice_downloading": _("Downloading {name}..."),
        "voice_unpacking": _("Unpacking {name}..."),
        "voice_download_done": _("{name} is ready."),
        "voice_download_failed": _("Download failed: {detail}"),
        "voice_type_small": _("small"),
        "voice_type_big": _("big"),
    }


def register(ctx: PluginContext) -> None:
    ctx.add_router(router)
    ctx.add_setting(
        "voice_model",
        validate_voice_model,
        _lazy(
            "Speech model for dictation: the name of a model installed on the "
            "Voice input card below, or the path of an unpacked Vosk model "
            "directory from elsewhere. Unset uses an installed model, preferring "
            "the UI language's. ENV: NTASKER_VOICE_MODEL."
        ),
    )
    ctx.add_js_strings(_js_strings)
    ctx.add_template_slot("head", "voice/templates/head.html")
    ctx.add_template_slot("task_form", "voice/templates/task_form.html")
    ctx.add_template_slot("task_edit", "voice/templates/task_edit.html")
    ctx.add_template_slot("scripts", "voice/templates/scripts.html")
    ctx.add_template_slot("settings", "voice/templates/settings.html")
