"""Clasificador de pertinencia y categoría (§5.5 bis).

Por qué existe. El léxico decide sin contexto, y llevamos seis mecanismos
de falso positivo corregidos en un solo día: la firma del medio tomada por
un ancla, la rubrica internacional ignorada, la categoría inventada por
defecto, dos cifras compartidas que agrupaban dos hechos, la gravedad leída
en el cuerpo, palabras que faltaban. Cada corrección era justa y ninguna
resuelve la siguiente. «Luck Ra se largó a llorar en pleno show» salió como
ALERTA crimen de alto impacto con dos fuentes: ninguna lista de palabras
arregla ese caso sin romper otro.

Lo que hace y lo que NO hace. Responde a dos preguntas —¿es un hecho de
seguridad? ¿de qué tipo?— y da su motivo en una frase. **No redacta nada.**
Los resúmenes siguen siendo estrictamente extractivos, frases recortadas de
las fuentes: la regla no negociable queda intacta porque clasificar no es
escribir.

Lo que decide y lo que no. No borra nunca. Un hecho que el modelo declara
ajeno a la seguridad baja a nota informativa con la categoría
«no_securitario» y su motivo a la vista — sigue en el panel, el analista lo
ve y puede contradecirlo. Descartar en silencio por decisión de un modelo
sería lo contrario de lo que este servicio promete.

Si no hay clave de API, si la llamada falla o si el modelo se niega, no pasa
nada: se conserva la clasificación léxica. Una colecta no se detiene nunca
por esto.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os

from atalaya.config import CATEGORIES, load_schedule

log = logging.getLogger(__name__)

# Categorías que el modelo puede devolver: las del proyecto, más la que
# solo él puede diagnosticar.
NO_SECURITARIO = "no_securitario"

_SCHEMA = {
    "type": "object",
    "properties": {
        "es_seguridad": {"type": "boolean"},
        "categoria": {
            "type": "string",
            # «sin_clasificar» es la confesión del léxico, no una respuesta
            # que tenga sentido pedir a un modelo: si no sabe, que lo diga
            # en el motivo. NO_SECURITARIO ya está en CATEGORIES.
            "enum": [c for c in CATEGORIES if c != "sin_clasificar"],
        },
        "motivo": {"type": "string", "maxLength": 160},
    },
    "required": ["es_seguridad", "categoria", "motivo"],
    "additionalProperties": False,
}

_SYSTEM = """Clasificas noticias para Atalaya, un servicio de vigilancia de \
seguridad usado por analistas de riesgos que asesoran a delegaciones \
diplomáticas y empresariales en América Latina.

Tu única tarea es responder DOS preguntas sobre el titular y el resumen que \
recibes. No reescribes, no resumes, no añades información.

1. ¿Es un hecho de seguridad? Lo es si afecta a la integridad física de las \
personas, a la seguridad de los desplazamientos o a la estabilidad del orden \
público: delitos, violencia armada, desastres naturales, accidentes graves, \
manifestaciones, operativos de seguridad, toques de queda.
NO lo son: política partidista, economía, deporte, espectáculos, cultura, \
religión, efemérides, necrológicas, salud individual, análisis o divulgación \
sobre un fenómeno (un artículo que EXPLICA los terremotos no es un terremoto), \
ni la cobertura de las secuelas administrativas o solidarias de un suceso \
antiguo (ayudas, homenajes, balances a un mes).

2. ¿De qué categoría es? Elige la que describe el hecho principal del \
titular, no un detalle del resumen.

Reglas no negociables:
- Juzgas SOLO por el texto recibido. Si no basta para decidir, marca \
es_seguridad=true: un hecho real que el analista no ve cuesta más caro que \
una etiqueta que puede corregir.
- Si es_seguridad es false, categoria debe ser «no_securitario».
- El motivo es una frase corta en español, factual, que cite lo que te hizo \
decidir. Nada de juicios sobre el medio ni sobre las personas."""


def backend() -> str:
    """«claude» o «none». La ausencia de clave no es un error: es el modo
    por defecto, y el pipeline funciona sin ella."""
    forced = os.environ.get("ATALAYA_CLASSIFY", "").lower()
    if forced in ("claude", "none"):
        return forced
    if not load_schedule().get("classifier", {}).get("enabled", True):
        return "none"
    return "claude" if os.environ.get("ANTHROPIC_API_KEY") else "none"


def fingerprint(title: str, summary: str | None) -> str:
    """Huella del texto juzgado: si no cambia, no se vuelve a pagar."""
    raw = f"{title}\n{summary or ''}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def classify(title: str, summary: str | None, country_name: str) -> dict | None:
    """Veredicto del modelo, o None si no se pudo obtener."""
    if backend() == "none" or not title:
        return None
    try:
        import anthropic
    except ImportError:                       # dependencia ausente: se sigue
        return None

    cfg = load_schedule().get("classifier", {})
    model = os.environ.get("ATALAYA_CLASSIFY_MODEL", cfg.get("model", "claude-opus-5"))
    payload = {"pais": country_name, "titular": title, "resumen": (summary or "")[:1500]}
    try:
        response = anthropic.Anthropic().messages.create(
            model=model,
            # 300 truncaba el JSON a media frase: «Unterminated string».
            # El motivo cabe en 160 caracteres, pero el modelo razona antes.
            max_tokens=1024,
            output_config={"format": {"type": "json_schema", "schema": _SCHEMA}},
            system=_SYSTEM,
            messages=[{"role": "user",
                       "content": json.dumps(payload, ensure_ascii=False)}],
        )
        if response.stop_reason == "refusal":
            log.warning("clasificación rechazada: %s", title[:80])
            return None
        if response.stop_reason == "max_tokens":
            # un JSON cortado no se parsea: mejor decirlo que dejar que
            # falle en json.loads con un mensaje incomprensible
            log.warning("clasificación truncada por max_tokens: %s", title[:80])
            return None
        text = next(b.text for b in response.content if b.type == "text")
        verdict = json.loads(text)
    except Exception as exc:
        log.warning("clasificación falló (%s): %s", title[:60], exc)
        return None

    # Coherencia: el esquema no puede imponer la relación entre los dos
    # campos, y una etiqueta contradictoria confundiría al analista.
    if not verdict.get("es_seguridad"):
        verdict["categoria"] = NO_SECURITARIO
    elif verdict.get("categoria") == NO_SECURITARIO:
        verdict["es_seguridad"] = False
    return verdict
