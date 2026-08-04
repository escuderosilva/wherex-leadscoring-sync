"""Read-back de las propiedades que alimentan el scoring de marketing.

Cuenta contactos directamente en HubSpot; no confía en el log del sync.
Por defecto falla si las señales que quedaron vacías por el incidente del
2026-07-29 continúan sin datos.

Uso:
  python3 verificar_scoring.py
  python3 verificar_scoring.py --sin-validar
"""
import argparse
import os
import sys
import time

import requests

from sync_circle_brevo import load_env


BASE = "https://api.hubapi.com"

NUMERICAS = [
    "brevo_enviados",
    "brevo_aperturas",
    "brevo_clics",
    "brevo_clics_comerciales",
    "brevo_nl_aperturas_90d",
    "academico_asistencias_pagas_12m",
    "academico_registros_12m",
    "academico_asistencias_12m",
    "academico_certificados",
    "eventos_pl_asistencias_12m",
    "eventos_wherex_12m",
]

FECHAS = [
    "brevo_ultima_apertura",
    "brevo_ultimo_clic",
    "brevo_fecha_desuscripcion",
]

BOOLEANAS = [
    "brevo_desuscrito",
    "es_comunidad",
]

CRITICAS_CON_DATOS = [
    "brevo_ultimo_clic",
    "brevo_desuscripcion_tipo",
    "brevo_fecha_desuscripcion",
]

# Este read-back gatillaba sobre cuatro señales, TODAS de Brevo. No miraba una sola
# propiedad de Circle: `es_comunidad` se imprimía pero no gatillaba, y el resto ni se
# consultaba. Consecuencia concreta: si la fase de Circle se rompía o se salteaba
# entera, la verificación salía verde igual.
CIRCLE = [
    "circle_publicaciones",
    "circle_comentarios",
    "circle_activity_score",
    "circle_nivel",
    "circle_ultima_visita",
]

# Un piso de cobertura, no un "> 0". Circle devuelve ~390 miembros y el objeto anidado
# `activity_score` viene en ~337 de ellos, así que una caída real se ve como un número
# que se desploma, no como un cero. Con "distinto de cero" un solo contacto sobreviviente
# haría pasar la prueba.
PISOS_COBERTURA = {
    "es_comunidad": 300,
    "circle_publicaciones": 300,
    "circle_activity_score": 250,
}


def _headers():
    return {
        "Authorization": "Bearer " + os.environ["HUBSPOT_TOKEN"],
        "Content-Type": "application/json",
    }


def total(filtros):
    """Cuenta contactos con reintento.

    Son ~45 POST /search seguidos. El endpoint de search de HubSpot tiene un límite
    propio, más bajo que el resto de la API: sin reintento, un 429 acá tumbaba el
    workflow con un traceback que no se parece en nada a un problema de datos.
    Mismo patrón que `esquema/hs_client.py` del repo principal: honrar `Retry-After`.
    """
    body = {
        "filterGroups": [{"filters": filtros}],
        "limit": 1,
        "properties": ["email"],
    }
    for intento in range(6):
        try:
            r = requests.post(
                BASE + "/crm/v3/objects/contacts/search",
                headers=_headers(),
                json=body,
                timeout=45,
            )
        except requests.exceptions.RequestException as e:
            if intento == 5:
                raise RuntimeError("HubSpot search, error de red: {}".format(e))
            time.sleep(min(2 ** intento, 30))
            continue
        if r.ok:
            return r.json().get("total", 0)
        if r.status_code in (429, 500, 502, 503, 504) and intento < 5:
            time.sleep(float(r.headers.get("Retry-After", min(2 ** intento, 30))))
            continue
        raise RuntimeError(
            "HubSpot search -> {}: {}".format(r.status_code, r.text[:400])
        )
    raise RuntimeError("HubSpot search: sin respuesta tras reintentos")


def tiene(propiedad):
    return total([{"propertyName": propiedad, "operator": "HAS_PROPERTY"}])


def mayor_cero(propiedad):
    return total(
        [{"propertyName": propiedad, "operator": "GT", "value": "0"}]
    )


def igual(propiedad, valor):
    return total(
        [{"propertyName": propiedad, "operator": "EQ", "value": valor}]
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--sin-validar",
        action="store_true",
        help="muestra conteos pero no falla por señales críticas vacías",
    )
    args = ap.parse_args()
    load_env()

    resultados = {}
    print("{:<38} {:>12} {:>12}".format("propiedad", "con valor", "> 0 / true"))
    print("-" * 64)
    for prop in NUMERICAS:
        con_valor, positivos = tiene(prop), mayor_cero(prop)
        resultados[prop] = con_valor
        resultados[prop + ":positivos"] = positivos
        print("{:<38} {:>12,} {:>12,}".format(prop, con_valor, positivos))
    for prop in FECHAS:
        con_valor = tiene(prop)
        resultados[prop] = con_valor
        print("{:<38} {:>12,} {:>12}".format(prop, con_valor, "-"))
    for prop in BOOLEANAS:
        con_valor, verdaderos = tiene(prop), igual(prop, "true")
        resultados[prop] = con_valor
        print("{:<38} {:>12,} {:>12,}".format(prop, con_valor, verdaderos))
    for prop in CIRCLE:
        con_valor = tiene(prop)
        resultados[prop] = con_valor
        print("{:<38} {:>12,} {:>12}".format(prop, con_valor, "-"))

    tipo = "brevo_desuscripcion_tipo"
    resultados[tipo] = tiene(tipo)
    print("{:<38} {:>12,} {:>12}".format(tipo, resultados[tipo], "-"))
    for valor in ("usuario", "rebote_duro", "admin"):
        print("  {:<36} {:>12,}".format(valor, igual(tipo, valor)))

    vacias = [p for p in CRITICAS_CON_DATOS if resultados.get(p, 0) == 0]
    if resultados.get("brevo_nl_aperturas_90d:positivos", 0) == 0:
        vacias.insert(0, "brevo_nl_aperturas_90d (> 0)")
    for prop, piso in PISOS_COBERTURA.items():
        cobertura = resultados.get(prop, 0)
        if cobertura < piso:
            vacias.append("{} ({:,} < piso {:,})".format(prop, cobertura, piso))
    if vacias and not args.sin_validar:
        print("\nFALLO: señales críticas sin datos: " + ", ".join(vacias))
        return 1
    if vacias:
        print("\nAVISO: señales críticas sin datos: " + ", ".join(vacias))
    else:
        print("\nOK: todas las señales críticas tienen datos en HubSpot.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
