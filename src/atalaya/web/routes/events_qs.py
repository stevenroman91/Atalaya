"""Reescritura de la cadena de consulta conservando lo demás.

Un contador que anuncia «7 a revisar a mano» y no se puede pinchar obliga
a leer veinte filas para encontrar cuáles son. Poder pinchar la cifra es
la respuesta directa a la pregunta que la cifra plantea.
"""
from __future__ import annotations

from urllib.parse import urlencode


def with_params(query: dict, **cambios) -> str:
    """URL del panel con `query` más los cambios. Un valor None quita la clave."""
    params = {k: v for k, v in query.items() if v not in (None, "")}
    for k, v in cambios.items():
        if v is None:
            params.pop(k, None)
        else:
            params[k] = v
    return "/dashboard" + ("?" + urlencode(params) if params else "")
