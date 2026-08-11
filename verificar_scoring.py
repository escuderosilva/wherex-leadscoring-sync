"""Read-back de las propiedades que alimentan el scoring de marketing.

Cuenta contactos directamente en HubSpot; no confía en el log del sync.

Uso:
  python3 verificar_scoring.py                  # las dos fuentes
  python3 verificar_scoring.py --solo circle    # sólo Circle (lo que corre a diario)
  python3 verificar_scoring.py --solo brevo     # sólo Brevo
  python3 verificar_scoring.py --sin-validar    # muestra conteos pero no falla
"""
import argparse
import datetime as dt
import os
import sys
import time

import requests

from sync_circle_brevo import load_env


BASE = "https://api.hubapi.com"

# Cada propiedad va etiquetada con QUIÉN LA ESCRIBE, no sólo con su tipo de dato.
# Sin esa separación el job diario de Circle fallaba por una señal de Brevo que esa
# corrida ni tocó — que es exactamente la alerta siempre-roja que nadie lee, el
# mismo error que tuvo este archivo hasta el 2026-08-04 al revés (gatillaba sólo
# sobre Brevo y dejaba pasar una caída total de Circle).
#
# Dos no están donde uno esperaría:
#   - `eventos_pl_asistencias_12m` la escribe CIRCLE desde el 2026-07-30
#     (`circle_eventos()`, RSVP real), no Brevo.
#   - `eventos_wherex_12m` sí es de Brevo: son los webinars propios que no pasan
#     por el Events de Circle.
#
# Tipos: num = con valor + > 0 · fecha/pres = con valor · bool = con valor + = true
PROPIEDADES = [
    ("brevo_enviados", "brevo", "num"),
    ("brevo_aperturas", "brevo", "num"),
    ("brevo_clics", "brevo", "num"),
    ("brevo_clics_comerciales", "brevo", "num"),
    ("brevo_nl_aperturas_90d", "brevo", "num"),
    ("academico_asistencias_pagas_12m", "brevo", "num"),
    ("academico_registros_12m", "brevo", "num"),
    ("academico_asistencias_12m", "brevo", "num"),
    ("academico_certificados", "brevo", "num"),
    ("eventos_wherex_12m", "brevo", "num"),
    ("brevo_ultima_apertura", "brevo", "fecha"),
    ("brevo_ultimo_clic", "brevo", "fecha"),
    ("brevo_fecha_desuscripcion", "brevo", "fecha"),
    ("brevo_desuscrito", "brevo", "bool"),
    ("eventos_pl_asistencias_12m", "circle", "num"),
    ("es_comunidad", "circle", "bool"),
    ("circle_publicaciones", "circle", "pres"),
    ("circle_comentarios", "circle", "pres"),
    ("circle_activity_score", "circle", "pres"),
    ("circle_nivel", "circle", "pres"),
    ("circle_ultima_visita", "circle", "pres"),
]

TIPO_DESUSCRIPCION = "brevo_desuscripcion_tipo"

# Señales que quedaron vacías por el incidente del 2026-07-29 y no deben volver a
# vaciarse. Todas de Brevo.
CRITICAS_CON_DATOS = [
    ("brevo_ultimo_clic", "brevo"),
    (TIPO_DESUSCRIPCION, "brevo"),
    ("brevo_fecha_desuscripcion", "brevo"),
]

# Un piso de cobertura, no un "> 0". Circle devuelve ~390 miembros y el objeto
# anidado `activity_score` viene en ~337 de ellos, así que una caída real se ve como
# un número que se desploma, no como un cero. Con "distinto de cero" un solo contacto
# sobreviviente haría pasar la prueba.
PISOS_COBERTURA = {
    "es_comunidad": ("circle", 300),
    "circle_publicaciones": ("circle", 300),
    "circle_activity_score": ("circle", 250),
}

# Detección de CONGELAMIENTO, no de ausencia (2026-08-11, al pasar Circle a diario).
#
# `PISOS_COBERTURA` prueba que la propiedad TIENE datos, no que los datos sean de
# hoy. Un token de Circle revocado deja los ~395 valores de ayer intactos en
# HubSpot: la cobertura sigue pasando para siempre y el job sale verde igual.
#
# `circle_ultima_visita` es la única señal que se mueve sola. Si el sync deja de
# refrescarla, la cuenta de "visitó en los últimos 7 días" se vacía por sí misma a
# medida que las fechas congeladas envejecen.
#
# Medido en el portal el 2026-08-11: 395 contactos con la propiedad, y por ventana
# 1d=17 · 2d=19 · 3d=20 · 7d=53 · 14d=77 · 30d=127 · 90d=373. Se usa la ventana de
# 7 días porque abarca una semana entera y cancela la estacionalidad de fin de
# semana (con la de 1 día, una corrida de domingo se caería sola). El piso de 15
# sobre 53 observados aguanta una baja real de actividad del ~70% sin falsa alarma.
#
# Es un detector LENTO: desde el congelamiento tarda unos días en cruzar el piso.
# La guarda de latencia cero es `MIN_MIEMBROS_CIRCLE` en el sync, que corta antes
# de escribir. Las dos son necesarias y miden cosas distintas.
FRESCURA_DIAS = 7
FRESCURA_MIN = 15


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


def visitas_recientes(dias=FRESCURA_DIAS):
    """Contactos con `circle_ultima_visita` dentro de los últimos `dias`.

    La propiedad es de tipo `date`; el search de HubSpot quiere el corte en
    milisegundos epoch a medianoche UTC.
    """
    corte = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=dias)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return total(
        [{
            "propertyName": "circle_ultima_visita",
            "operator": "GTE",
            "value": int(corte.timestamp() * 1000),
        }]
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--solo",
        choices=["circle", "brevo"],
        help="verificar una sola fuente; el job diario usa `circle`",
    )
    ap.add_argument(
        "--sin-validar",
        action="store_true",
        help="muestra conteos pero no falla por señales críticas vacías",
    )
    args = ap.parse_args()
    load_env()

    fuentes = {args.solo} if args.solo else {"circle", "brevo"}
    print("Verificando: {}".format(" + ".join(sorted(fuentes))))

    resultados = {}
    print("{:<38} {:>12} {:>12}".format("propiedad", "con valor", "> 0 / true"))
    print("-" * 64)
    for prop, fuente, tipo in PROPIEDADES:
        if fuente not in fuentes:
            continue
        con_valor = tiene(prop)
        resultados[prop] = con_valor
        if tipo == "num":
            positivos = mayor_cero(prop)
            resultados[prop + ":positivos"] = positivos
            print("{:<38} {:>12,} {:>12,}".format(prop, con_valor, positivos))
        elif tipo == "bool":
            verdaderos = igual(prop, "true")
            print("{:<38} {:>12,} {:>12,}".format(prop, con_valor, verdaderos))
        else:
            print("{:<38} {:>12,} {:>12}".format(prop, con_valor, "-"))

    if "brevo" in fuentes:
        resultados[TIPO_DESUSCRIPCION] = tiene(TIPO_DESUSCRIPCION)
        print("{:<38} {:>12,} {:>12}".format(
            TIPO_DESUSCRIPCION, resultados[TIPO_DESUSCRIPCION], "-"))
        for valor in ("usuario", "rebote_duro", "admin"):
            print("  {:<36} {:>12,}".format(valor, igual(TIPO_DESUSCRIPCION, valor)))

    vacias = []
    if "brevo" in fuentes and resultados.get("brevo_nl_aperturas_90d:positivos", 0) == 0:
        vacias.append("brevo_nl_aperturas_90d (> 0)")
    vacias += [p for p, f in CRITICAS_CON_DATOS
               if f in fuentes and resultados.get(p, 0) == 0]
    for prop, (fuente, piso) in PISOS_COBERTURA.items():
        if fuente not in fuentes:
            continue
        cobertura = resultados.get(prop, 0)
        if cobertura < piso:
            vacias.append("{} ({:,} < piso {:,})".format(prop, cobertura, piso))

    if "circle" in fuentes:
        recientes = visitas_recientes()
        print("\nFrescura: {:,} contactos visitaron Circle en los últimos {} días "
              "(piso {}).".format(recientes, FRESCURA_DIAS, FRESCURA_MIN))
        if recientes < FRESCURA_MIN:
            vacias.append(
                "circle_ultima_visita CONGELADA ({:,} visitas en {} días < piso {}): "
                "el sync probablemente no está refrescando Circle".format(
                    recientes, FRESCURA_DIAS, FRESCURA_MIN)
            )

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
