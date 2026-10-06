"""Language detection and the honest fallback when no translation is possible
(ADR 0019).

Detection is a small, deterministic stop-word count for the languages most
used in the Caribbean. It is good enough to pick a reply language; with an AI
service the model's own judgement is used as well.
"""

from __future__ import annotations

import re

NAMES = {
    "en": "English",
    "es": "Spanish",
    "fr": "French",
    "pt": "Portuguese",
    "nl": "Dutch",
    "ht": "Haitian Creole",
    "pap": "Papiamento",
}

_STOP = {
    "en": "the and is are you your what when where how can could would please thanks hello my our do does i".split(),
    "es": "el la los las de que y es son por para con una hola gracias quiero puedo cuando donde como mi su usted"
    " necesito tengo está".split(),
    "fr": "le la les des est et vous je une pour avec bonjour merci quand où comment mon votre sont pas suis"
    " voudrais".split(),
    "pt": "o os as de que e é são para com uma olá obrigado obrigada quero posso quando onde como meu você não"
    " está".split(),
    "nl": "de het een en is zijn van ik je u wat wanneer waar hoe mijn uw dank hallo graag niet".split(),
    "ht": "mwen ou li nou yo se ki pa ak pou bonjou mèsi kijan kote kilè vle gen konnen".split(),
    "pap": "mi bo nos e un ta di ku pa bon dia danki kon unda ki ora kier".split(),
}

_WORD = re.compile(r"[^\W\d_]+", re.U)

# Said in the customer's language when the AI can only answer in the business
# language (no AI service configured, so no translation).
FALLBACK = {
    "es": "Disculpe, por ahora solo puedo responder en inglés. Si lo prefiere, una persona del equipo le ayudará"
    " en español.",
    "fr": "Désolé, pour l'instant je ne peux répondre qu'en anglais. Si vous préférez, une personne de l'équipe"
    " vous aidera en français.",
    "pt": "Desculpe, por enquanto só posso responder em inglês. Se preferir, uma pessoa da equipa ajuda-o em"
    " português.",
    "nl": "Sorry, voorlopig kan ik alleen in het Engels antwoorden. Als u wilt, helpt iemand van het team u in het"
    " Nederlands.",
    "ht": "Eskize m, pou kounye a mwen ka reponn an anglè sèlman. Si ou vle, yon moun nan ekip la ap ede w an kreyòl.",
    "pap": "Despensá, pa awor mi por kontestá na ingles so. Si bo ke, un persona di e tim lo yuda bo na Papiamentu.",
}


def detect(text: str, default: str = "en") -> str:
    """The most likely language of `text`, or `default` when unsure."""
    words = [w.lower() for w in _WORD.findall(text or "")]
    if len(words) < 2:
        return default
    scores = {code: sum(1 for w in words if w in set(stop)) for code, stop in _STOP.items()}
    best = max(scores, key=lambda c: scores[c])
    ordered = sorted(scores.values(), reverse=True)
    if scores[best] < 2 or (len(ordered) > 1 and ordered[0] == ordered[1]):
        return default
    return best


def name(code: str) -> str:
    return NAMES.get(code, code or "unknown")


def fallback_note(code: str) -> str:
    return FALLBACK.get(code, "")
