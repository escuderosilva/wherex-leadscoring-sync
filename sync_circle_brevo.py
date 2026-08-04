"""Sync semanal Circle + Brevo -> propiedades de Contacto en HubSpot.

Alimenta el LEAD SCORING NATIVO de HubSpot. Division de trabajo:
  - este script escribe FECHAS y NUMEROS crudos;
  - la ventana temporal ("en los ultimos 90 dias") la hace el scoring nativo.
La unica excepcion es `brevo_nl_aperturas_90d`: HubSpot no puede contar aperturas
por campana, asi que la ventana se calcula aca y se reescribe entera cada corrida.

Las metricas de email por contacto (aperturas, clics, desuscripcion y sus fechas)
salen de `GET /contacts/{email}?statistics=true`, UN llamado por contacto, con la
fecha EXACTA de cada evento (sin aproximar por ventana). Se pagina en paralelo:
ese endpoint tiene su propio bucket de rate limit, medido en **20 req/seg**
(cabecera `x-sib-ratelimit-*`, verificado 2026-07-30) -- muy distinto del bucket
de `POST /emailCampaigns/{id}/exportRecipients` (100/hora), que fue la razon por
la que una version anterior de este sync exportaba por campana y cacheaba en
disco (`brevo_campanas.py` + `cache_brevo/`, DEPRECADOS: ver HANDOFF.md, seccion
"Vuelta atras del diseno de Brevo"). Con este bucket, los ~22k contactos de Brevo
entran en el sync en unos 20-25 minutos, sin cache en disco y con precision total.

Reglas y puntajes del scoring: Scoring_MQL.md
Inventario de las propiedades: ../esquema/inv_contacto.py (bucket "integracion")

Por que existe: la carga de Circle/Brevo del 2026-07-21 fue un import unico
(verificado en el historial de propiedades) -> sin este sync el scoring queda
congelado en esa foto.

Uso:
  python sync_circle_brevo.py --dry-run        # calcula y muestra, no escribe
  python sync_circle_brevo.py --solo circle    # una fuente
  python sync_circle_brevo.py --solo brevo
  python sync_circle_brevo.py                  # las dos + escritura por lotes
  python sync_circle_brevo.py --limite 50      # smoke test acotado (tope de escritura)
  python sync_circle_brevo.py --generar-mapeo  # semilla de actividades_mkt.csv

Idempotente: recalcula desde la fuente y hace upsert por email. Correrlo dos
veces seguidas deja el mismo estado.
"""
import argparse
import concurrent.futures
import csv
import datetime as dt
import os
import re
import sys
import threading
import time
import urllib.parse
import warnings
from collections import defaultdict

warnings.filterwarnings("ignore")
import requests


ENV_REQUERIDAS = ("HUBSPOT_TOKEN", "BREVO_API_KEY", "CIRCLE_API_TOKEN")


def load_env():
    """Carga un .env en os.environ si existe (parser minimo KEY=VALUE).

    **Si no hay .env no falla**: en CI (GitHub Actions) los tokens llegan como
    variables de entorno reales desde los repository secrets, y no existe ningun
    archivo .env. La version anterior hacia `open()` sin proteccion y moria con
    FileNotFoundError antes de la primera linea util.

    Se usa `setdefault`, asi que una variable ya presente en el entorno **gana**
    sobre el archivo: en CI manda el secret, en local manda tu .env.
    Los secretos NO se imprimen nunca.
    """
    aqui = os.path.dirname(os.path.abspath(__file__))
    candidatos = [
        os.path.join(aqui, ".env"),        # junto al script (uso normal)
        os.path.join(aqui, "..", ".env"),  # legado: raiz de hubspot_admin
    ]
    for ruta in candidatos:
        if not os.path.isfile(ruta):
            continue
        with open(ruta) as fh:
            for linea in fh:
                linea = linea.strip()
                if not linea or linea.startswith("#") or "=" not in linea:
                    continue
                k, v = linea.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
        break


def exigir_env():
    """Falla temprano y claro si falta un token, en vez de morir con un KeyError
    opaco a los 20 minutos de sync. Nunca imprime valores."""
    faltan = [k for k in ENV_REQUERIDAS if not os.environ.get(k)]
    if faltan:
        raise SystemExit(
            "Faltan variables de entorno: " + ", ".join(faltan) +
            "\nEn local: agregalas al .env. En GitHub Actions: Settings -> "
            "Secrets and variables -> Actions."
        )


load_env()

HS = "https://api.hubapi.com"
BREVO = "https://api.brevo.com/v3"
CIRCLE = "https://app.circle.so/api/admin/v2"

HOY = dt.datetime.now(dt.timezone.utc)
VENTANA_ACTIVIDAD = 365  # dias, criterio academico/eventos
VENTANA_EMAIL = 365      # dias de los acumulados de email (enviados/aperturas/clics)
VENTANA_NL = 90          # dias del criterio "lector del newsletter"

# Espacios de Circle que cuentan como CREACION de contenido (definicion acordada
# con marketing 2026-07-29). Presentate y Preguntale quedan fuera: son participacion.
ESPACIOS_CONTENIDO = {"videos", "templates-y-recursos", "hacks", "todo-sobre-ia",
                      "contingencias-en-procure", "procure-weekly", "tutoriales-plataforma"}
ESPACIO_PRESENTACION = "presentate"

# Eventos del espacio "eventos" (space_type=event) que NO cuentan como asistencia
# de marketing: sólo pruebas/QA internas (ej. "Evento de Prueba"). Confirmado con
# Emilio 2026-07-30: el resto -- incluido Comité de Advisors -- sí cuenta como
# evento_pl. Ver circle_eventos().
EVENTO_EXCLUIR_PATRON = re.compile(r"prueba", re.IGNORECASE)


# --------------------------------------------------------------------------- utils
def _h_hs():
    return {"Authorization": f"Bearer {os.environ['HUBSPOT_TOKEN']}",
            "Content-Type": "application/json"}


def _h_brevo():
    return {"api-key": os.environ["BREVO_API_KEY"], "accept": "application/json",
            "content-type": "application/json"}


def _h_circle():
    return {"Authorization": f"Token {os.environ['CIRCLE_API_TOKEN']}",
            "accept": "application/json"}


def _get(url, headers, params=None, tries=5):
    """GET con backoff. 429 respeta Retry-After; 5xx reintenta; errores de red tambien."""
    espera = 1.0
    for intento in range(tries):
        try:
            r = requests.get(url, headers=headers, params=params, timeout=45)
        except requests.exceptions.RequestException as e:
            if intento < tries - 1:
                time.sleep(espera)
                espera = min(espera * 2, 30)
                continue
            raise RuntimeError(f"GET {url}: error de red tras {tries} intentos: {e}") from e
        if r.ok:
            return r
        if r.status_code in (429, 500, 502, 503, 504) and intento < tries - 1:
            time.sleep(float(r.headers.get("Retry-After", espera)))
            espera = min(espera * 2, 30)
            continue
        raise RuntimeError(f"GET {url} -> {r.status_code}: {r.text[:200]}")
    raise RuntimeError(f"GET {url}: agotados los reintentos")


def _fecha(iso):
    """ISO -> 'YYYY-MM-DD' (HubSpot date). None si no hay valor."""
    if not iso:
        return None
    try:
        return dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return None


def _dias(iso):
    if not iso:
        return None
    try:
        return (HOY - dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))).days
    except ValueError:
        return None


# -------------------------------------------------------------------------- circle
def circle_miembros():
    """{email: props} con gamificacion, actividad y fechas de cada miembro."""
    out, page = {}, 1
    while page <= 60:
        r = _get(f"{CIRCLE}/community_members", _h_circle(),
                 {"per_page": 100, "page": page})
        d = r.json()
        recs = d.get("records", [])
        if not recs:
            break
        for m in recs:
            email = (m.get("email") or "").strip().lower()
            if not email:
                continue
            g = m.get("gamification_stats") or {}
            a = m.get("activity_score") or {}
            out[email] = {
                "es_comunidad": "true",
                "circle_nivel": g.get("current_level"),
                "circle_puntos": g.get("total_points"),
                "circle_miembro_desde": _fecha(m.get("created_at")),
                "circle_ultima_visita": _fecha(m.get("last_seen_at")),
                "circle_perfil_confirmado": _fecha(m.get("profile_confirmed_at")),
                "circle_activity_score": a.get("activity_score"),
                "circle_presence": a.get("presence"),
                "circle_participacion": a.get("participation"),
                "circle_contribucion": a.get("contribution"),
                "circle_publicaciones": m.get("posts_count"),
                "circle_comentarios": m.get("comments_count"),
            }
        if not d.get("has_next_page"):
            break
        page += 1
    return out


def circle_posts(por_email):
    """Suma al dict: presentacion (espacio Presentate) y creacion de contenido.

    Los posts traen `user_email`, `space_slug` y `created_at`, asi que no hace
    falta cruzar por id de miembro.
    """
    presentacion, contenido_n, contenido_fecha = {}, defaultdict(int), {}
    page = 1
    while page <= 100:
        r = _get(f"{CIRCLE}/posts", _h_circle(), {"per_page": 100, "page": page, "sort": "latest"})
        d = r.json()
        recs = d.get("records", [])
        if not recs:
            break
        for p in recs:
            email = (p.get("user_email") or "").strip().lower()
            if not email:
                continue
            slug = (p.get("space_slug") or "").lower()
            cuando = p.get("published_at") or p.get("created_at")
            if slug == ESPACIO_PRESENTACION:
                # el primero cuenta: nos quedamos con el mas antiguo
                actual = presentacion.get(email)
                if not actual or (cuando or "") < actual:
                    presentacion[email] = cuando
            elif slug in ESPACIOS_CONTENIDO:
                contenido_n[email] += 1
                if not contenido_fecha.get(email) or (cuando or "") > contenido_fecha[email]:
                    contenido_fecha[email] = cuando
        if not d.get("has_next_page"):
            break
        page += 1

    for email, cuando in presentacion.items():
        por_email.setdefault(email, {})["circle_fecha_presentacion"] = _fecha(cuando)
    for email, n in contenido_n.items():
        por_email.setdefault(email, {})["circle_posts_contenido"] = n
        por_email[email]["circle_fecha_ultimo_contenido"] = _fecha(contenido_fecha.get(email))
    return por_email


def circle_eventos():
    """{email: {eventos_pl_asistencias_12m: n}} desde el RSVP real de Circle.

    `GET /events` + `GET /event_attendees?event_id=X` (espacio "eventos",
    space_type=event). Reemplaza la aproximacion anterior por listas de Brevo
    ("Inscritos - Eventos - <Pais>", fecha del createdAt de la LISTA, no del
    evento): acá la fecha es el `rsvp_date` real de cada persona. Los webinars
    propios de Wherex que no pasan por el Events de Circle (ej. "Registrados
    Webinar IA - Wherex") siguen viniendo de `brevo_actividades()`, categoria
    `evento_wherex` — no se tocan.
    """
    corte = HOY - dt.timedelta(days=VENTANA_ACTIVIDAD)
    out = defaultdict(lambda: defaultdict(int))
    eventos, page = [], 1
    while page <= 50:
        r = _get(f"{CIRCLE}/events", _h_circle(), {"per_page": 100, "page": page})
        d = r.json()
        recs = d.get("records", [])
        if not recs:
            break
        eventos += recs
        if not d.get("has_next_page"):
            break
        page += 1

    excluidos = []
    for ev in eventos:
        nombre = ev.get("name") or ""
        if EVENTO_EXCLUIR_PATRON.search(nombre):
            excluidos.append(nombre)
            continue
        p = 1
        while p <= 50:
            r = _get(f"{CIRCLE}/event_attendees", _h_circle(),
                     {"event_id": ev["id"], "per_page": 100, "page": p})
            d = r.json()
            for a in d.get("records", []):
                email = (a.get("member_email") or "").strip().lower()
                fecha = a.get("rsvp_date")
                if not email or not fecha:
                    continue
                if dt.datetime.fromisoformat(fecha.replace("Z", "+00:00")) < corte:
                    continue
                out[email]["eventos_pl_asistencias_12m"] += 1
            if not d.get("has_next_page"):
                break
            p += 1
    print(f"  {len(eventos) - len(excluidos)} eventos contados, "
          f"{len(excluidos)} excluidos (prueba/QA): {excluidos}", file=sys.stderr)
    return {email: dict(props) for email, props in out.items()}


def circle_todo():
    print("Circle: leyendo miembros...", file=sys.stderr)
    m = circle_miembros()
    print(f"  {len(m)} miembros", file=sys.stderr)
    print("Circle: leyendo posts...", file=sys.stderr)
    m = circle_posts(m)
    print("Circle: leyendo eventos (RSVP real)...", file=sys.stderr)
    for email, props in circle_eventos().items():
        m.setdefault(email, {}).update(props)
    return m


# --------------------------------------------------------------------------- brevo
def brevo_contactos():
    """{email: {enviados/aperturas/clics/desuscrito/...}} paginando /contacts.

    17 llamadas para ~16.600 contactos (limit=1000), en vez de una por contacto.
    """
    out, offset = {}, 0
    while offset < 200_000:
        r = _get(f"{BREVO}/contacts", _h_brevo(), {"limit": 1000, "offset": offset})
        cs = r.json().get("contacts", [])
        if not cs:
            break
        for c in cs:
            email = (c.get("email") or "").strip().lower()
            if not email:
                continue
            out[email] = {"brevo_desuscrito": "true" if c.get("emailBlacklisted") else "false"}
        offset += 1000
    return out


def _campanas_newsletter():
    """{campaign_id: es_newsletter} para TODAS las campanas alguna vez enviadas.

    No filtra por fecha: el filtro de ventana se aplica sobre el `eventTime` de
    cada evento del contacto (ver `brevo_estadisticas`), no sobre la fecha de
    envio de la campana. El newsletter se reconoce por nombre ("Newsletter #NN
    <Pais>"), igual que antes en brevo_campanas.py.
    """
    out, offset = {}, 0
    while offset < 5000:
        r = _get(f"{BREVO}/emailCampaigns", _h_brevo(),
                 {"limit": 100, "offset": offset, "sort": "desc"})
        cs = r.json().get("campaigns", [])
        if not cs:
            break
        for c in cs:
            if c.get("sentDate"):
                out[c["id"]] = "newsletter" in (c.get("name") or "").lower()
        offset += 100
    return out


def _get_brevo_contacto(email, tries=5):
    """GET /contacts/{email}?statistics=true con reintento propio.

    Bucket medido: 20 req/seg, ventana de 1s (`x-sib-ratelimit-reset`). Un 404
    significa que el contacto se borro de Brevo entre el listado y esta llamada
    -> se salta, no es un error del sync. Los errores de RED (DNS, timeout,
    conexion caida) no vienen con status_code -- son excepciones de `requests`,
    no HTTP -- y con miles de llamadas en paralelo alguna va a pasar; un blip
    de wifi no puede tirar abajo una corrida de 20 minutos.
    """
    url = f"{BREVO}/contacts/{urllib.parse.quote(email)}"
    espera = 0.5
    for intento in range(tries):
        try:
            r = requests.get(url, headers=_h_brevo(), params={"statistics": "true"}, timeout=30)
        except requests.exceptions.RequestException as e:
            if intento < tries - 1:
                time.sleep(espera)
                espera = min(espera * 2, 10)
                continue
            print(f"  {email}: error de red tras {tries} intentos: {e}", file=sys.stderr)
            return None
        if r.ok:
            return r.json()
        if r.status_code == 404:
            return None
        if r.status_code in (429, 500, 502, 503, 504) and intento < tries - 1:
            reset = r.headers.get("x-sib-ratelimit-reset")
            time.sleep(float(reset) if reset else espera)
            espera = min(espera * 2, 10)
            continue
        print(f"  {email}: {r.status_code} {r.text[:150]}", file=sys.stderr)
        return None
    return None


def brevo_estadisticas(emails, es_nl, max_workers=15):
    """{email: props} de email leyendo `statistics` por contacto, en paralelo.

    `statistics.messagesSent/opened` traen `campaignId` + `eventTime` PLANO por
    evento. `statistics.clicked` es distinto: el `eventTime` esta anidado por
    link, no al nivel del evento (`{"campaignId": N, "links": [{"eventTime":
    ..., "url": ...}, ...]}` -- un contacto puede clickear varios links de la
    misma campana). Verificado 2026-07-30 contra un clicker real; un primer
    intento leyo `e.eventTime` directo sobre `clicked` (inexistente ahi) y dejo
    `brevo_clics`/`brevo_clics_comerciales`/`brevo_ultimo_clic` en 0 para
    todos -- mismo patron de bug que A: un campo con "0 en todos" hay que
    sospecharlo, no asumir que la senal simplemente es baja.

    Los acumulados de 365d y la ventana de 90d del NL se calculan filtrando
    esos eventos, sin aproximar. `brevo_ultima_apertura` / `brevo_ultimo_clic`
    son el maximo HISTORICO (no se acotan a la ventana): una "ultima fecha" es
    un maximo, acotarla perderia informacion.
    """
    corte_365 = (HOY - dt.timedelta(days=VENTANA_EMAIL)).isoformat()
    corte_nl = (HOY - dt.timedelta(days=VENTANA_NL)).isoformat()
    out = {}
    lock = threading.Lock()
    procesados = [0]

    def uno(email):
        d = _get_brevo_contacto(email)
        s = (d or {}).get("statistics") or {}
        props = {}
        if s:
            abiertas_todas = s.get("opened", []) or []
            clics_todos = s.get("clicked", []) or []
            enviados = {e["campaignId"] for e in s.get("messagesSent", []) or []
                       if (e.get("eventTime") or "") >= corte_365}
            abiertas = {e["campaignId"] for e in abiertas_todas
                       if (e.get("eventTime") or "") >= corte_365}
            clics = {c["campaignId"] for c in clics_todos
                    if any((l.get("eventTime") or "") >= corte_365 for l in c.get("links") or [])}
            nl_abiertas = {e["campaignId"] for e in abiertas_todas
                          if (e.get("eventTime") or "") >= corte_nl
                          and es_nl.get(e.get("campaignId"))}
            clics_comerciales = {c for c in clics if not es_nl.get(c)}
            props = {
                "brevo_enviados": len(enviados),
                "brevo_aperturas": len(abiertas),
                "brevo_clics": len(clics),
                "brevo_clics_comerciales": len(clics_comerciales),
                "brevo_nl_aperturas_90d": len(nl_abiertas),
                "brevo_tasa_apertura": (round(100 * len(abiertas) / len(enviados), 1)
                                        if enviados else 0),
            }
            todas_ap = [e.get("eventTime") for e in abiertas_todas if e.get("eventTime")]
            todos_cl = [l.get("eventTime") for c in clics_todos for l in c.get("links") or []
                       if l.get("eventTime")]
            if todas_ap:
                props["brevo_ultima_apertura"] = _fecha(max(todas_ap))
            if todos_cl:
                props["brevo_ultimo_clic"] = _fecha(max(todos_cl))
            # Solo la baja de USUARIO resta puntaje (ver Scoring_MQL.md grupo 5); el
            # rebote duro y la limpieza administrativa (sin rastro aca) no.
            user_unsub = [e for e in (s.get("unsubscriptions") or {}).get("userUnsubscription") or []
                         if e.get("eventTime")]
            hb = [e for e in (s.get("hardBounces") or []) if e.get("eventTime")]
            if user_unsub:
                props["brevo_desuscripcion_tipo"] = "usuario"
                props["brevo_fecha_desuscripcion"] = _fecha(max(e["eventTime"] for e in user_unsub))
            elif hb:
                props["brevo_desuscripcion_tipo"] = "rebote_duro"
                props["brevo_fecha_desuscripcion"] = _fecha(max(e["eventTime"] for e in hb))
        with lock:
            out[email] = props
            procesados[0] += 1
            if procesados[0] % 2000 == 0:
                print(f"  {procesados[0]}/{len(emails)} contactos procesados", file=sys.stderr)

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
        list(ex.map(uno, emails))
    return out


def brevo_actividades(mapa_path="actividades_mkt.csv"):
    """Cuenta registros/asistencias/certificados por contacto desde las listas.

    El mapeo (que lista es que actividad, si es paga, su fecha) vive en un CSV
    curado que marketing valida; se genera con --generar-mapeo.
    """
    if not os.path.exists(mapa_path):
        print(f"  falta {mapa_path} (correr --generar-mapeo): se saltan academicas",
              file=sys.stderr)
        return {}
    corte = HOY - dt.timedelta(days=VENTANA_ACTIVIDAD)
    actividades = []
    with open(mapa_path) as fh:
        for row in csv.DictReader(fh):
            fecha = dt.datetime.fromisoformat(row["fecha"] + "T00:00:00+00:00")
            if fecha < corte:
                continue
            actividades.append({"list_id": int(row["list_id"]), "tipo": row["tipo"],
                                "paga": row["paga"].strip().lower() in ("si", "sí", "true", "1"),
                                "categoria": row["categoria"], "fecha": fecha.date().isoformat()})
    out = defaultdict(lambda: defaultdict(int))
    ultima = {}
    for act in actividades:
        if act["categoria"] == "evento_pl":
            # Desde 2026-07-30 esto sale de circle_eventos() (RSVP real, fecha
            # exacta). Estas listas de Brevo son audiencias de invitacion, no
            # asistencia por evento -> no sumar dos veces.
            continue
        offset = 0
        while offset < 100_000:
            r = _get(f"{BREVO}/contacts/lists/{act['list_id']}/contacts", _h_brevo(),
                     {"limit": 500, "offset": offset})
            cs = r.json().get("contacts", [])
            if not cs:
                break
            for c in cs:
                email = (c.get("email") or "").strip().lower()
                if not email:
                    continue
                if act["categoria"] == "evento_wherex":
                    out[email]["eventos_wherex_12m"] += 1
                elif act["tipo"] == "asistencia":
                    out[email]["academico_asistencias_12m"] += 1
                    if act["paga"]:
                        out[email]["academico_asistencias_pagas_12m"] += 1
                elif act["tipo"] == "certificado":
                    out[email]["academico_certificados"] += 1
                else:
                    out[email]["academico_registros_12m"] += 1
                if not ultima.get(email) or act["fecha"] > ultima[email]:
                    ultima[email] = act["fecha"]
            offset += 500
    res = {}
    for email, d in out.items():
        props = dict(d)
        props["academico_ultima_actividad"] = ultima.get(email)
        res[email] = props
    return res


ACUMULADOS_BREVO = ["brevo_enviados", "brevo_aperturas", "brevo_clics",
                    "brevo_clics_comerciales", "brevo_nl_aperturas_90d",
                    "brevo_tasa_apertura"]


def brevo_todo():
    """Todas las props de Brevo: estado del contacto + metricas de email + academia.

    Las metricas por contacto salen de `brevo_estadisticas` (un GET por contacto,
    en paralelo, ver docstring del modulo). Se ponen en 0 los acumulados para
    quien no tuvo actividad de email en la ventana: a diferencia del diseno
    anterior (cache de campanas, que podia estar incompleta), aca cada contacto
    se lee siempre completo, asi que el 0 nunca es una mentira por falta de cache.
    """
    print("Brevo: leyendo contactos...", file=sys.stderr)
    base = brevo_contactos()
    print(f"  {len(base)} contactos", file=sys.stderr)

    print("Brevo: clasificando campanas (newsletter o no)...", file=sys.stderr)
    es_nl = _campanas_newsletter()
    print(f"  {len(es_nl)} campanas", file=sys.stderr)

    print(f"Brevo: metricas de email por contacto ({len(base)} llamadas, en paralelo)...",
          file=sys.stderr)
    metricas = brevo_estadisticas(list(base.keys()), es_nl)
    con_envios = sum(1 for p in metricas.values() if p.get("brevo_enviados"))
    print(f"  {con_envios} contactos con envios en los ultimos {VENTANA_EMAIL} dias",
          file=sys.stderr)

    for email, props in metricas.items():
        base.setdefault(email, {}).update(props)
    for props in base.values():
        for p in ACUMULADOS_BREVO:
            props.setdefault(p, 0)
        # El tipo de desuscripcion que no deja rastro en ninguna campana es la
        # limpieza administrativa (import, borrado a mano). Solo aplica a quien
        # esta blacklisted.
        if props.get("brevo_desuscrito") == "true" and not props.get("brevo_desuscripcion_tipo"):
            props["brevo_desuscripcion_tipo"] = "admin"

    print("Brevo: actividades academicas y eventos...", file=sys.stderr)
    for email, props in brevo_actividades().items():
        base.setdefault(email, {}).update(props)
    return base


def generar_mapeo(destino="actividades_mkt.csv"):
    """Semilla del mapeo de actividades a partir de los nombres de lista de Brevo.

    Clasifica por convencion (Asistentes/Inscritos/Certificado) y usa el createdAt
    de la lista como fecha de la actividad. `paga` arranca en "revisar": marketing
    la corrige. Todo lo que no sea actividad queda con categoria "ignorar".
    """
    listas = []
    for off in range(0, 300, 50):
        r = _get(f"{BREVO}/contacts/lists", _h_brevo(), {"limit": 50, "offset": off})
        ls = r.json().get("lists", [])
        if not ls:
            break
        listas += ls
    filas = []
    for l in listas:
        d = _get(f"{BREVO}/contacts/lists/{l['id']}", _h_brevo()).json()
        nombre = l["name"]
        bajo = nombre.lower()
        # Orden importa: "Inscritos y Asistentes ..." debe caer en asistencia.
        # Las listas de audiencia para enviar ("Prospectos ...", "Clientes BP altos ...",
        # "MKT_..._no_abiertos") NO son actividades: quedan en ignorar.
        if bajo.startswith("certificado") or "certificados" in bajo or "diploma" in bajo:
            tipo, categoria = "certificado", "academica"
        elif "asistente" in bajo:
            tipo, categoria = "asistencia", "academica"
        elif bajo.startswith("inscritos - eventos") or bajo.startswith("registrados - eventos"):
            tipo, categoria = "asistencia", "evento_pl"
        elif "wherex" in bajo and ("webinar" in bajo or "evento" in bajo):
            tipo, categoria = "registro", "evento_wherex"
        elif "inscrit" in bajo or "registrados" in bajo or "webinar" in bajo or "tally" in bajo:
            tipo, categoria = "registro", "academica"
        else:
            tipo, categoria = "", "ignorar"
        filas.append({"list_id": l["id"], "nombre": nombre,
                      "fecha": (_fecha(d.get("createdAt")) or ""), "tipo": tipo,
                      "paga": "revisar" if categoria == "academica" else "no",
                      "categoria": categoria, "suscriptores": d.get("uniqueSubscribers", 0)})
    filas.sort(key=lambda f: f["fecha"], reverse=True)
    with open(destino, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["list_id", "nombre", "fecha", "tipo", "paga",
                                           "categoria", "suscriptores"])
        w.writeheader()
        w.writerows(filas)
    act = sum(1 for f in filas if f["categoria"] != "ignorar")
    print(f"{destino}: {len(filas)} listas, {act} clasificadas como actividad. "
          f"Marketing debe revisar la columna `paga`.")


# ------------------------------------------------------------------------ escritura
# Prefiltro estructural de email. No valida el TLD (HubSpot lo hace contra su
# propia lista y rechaza cosas como .con/.ocm/.cpm, que este patron acepta): para
# eso esta el reintento sin los rechazados en _postear_lote.
RE_EMAIL = re.compile(r"^[^@\s,;]+@[^@\s,;.]+(\.[^@\s,;.]+)+$")
# HubSpot devuelve los emails invalidos dentro del mensaje de error del lote.
RE_INVALIDO = re.compile(r"Email address (\S+) is invalid")


def _postear_lote(lote, nivel=0, intento=0):
    """Escribe un lote y devuelve (escritos, fallidos, [emails descartados]).

    Un solo email invalido hace fallar el lote ENTERO con 400 (asi se perdieron
    1.700 contactos en la corrida del 2026-07-29). La respuesta nombra a los
    culpables, asi que se los saca y se reintenta; si el 400 es por otra cosa, se
    parte el lote en dos para que un registro malo no arrastre a los otros 99.
    """
    if not lote:
        return 0, 0, []
    # Un `None` se OMITE, no se manda como "".
    #
    # En HubSpot un string vacio BORRA la propiedad. La version anterior mandaba
    # `"" if v is None else str(v)`, asi que cualquier metrica que la fuente no
    # devolviera se limpiaba en HubSpot. Con un job semanal desatendido eso convierte
    # una falla transitoria de la API en perdida permanente de datos: las 4 metricas
    # `circle_activity_score/_presence/_participacion/_contribucion` salen de un objeto
    # anidado (`activity_score`) que Circle no incluye para miembros sin actividad; si
    # algun dia deja de incluirlo para todos, la corrida siguiente vaciaba los 337
    # contactos que hoy lo tienen, sin que nadie se enterara.
    #
    # Omitir significa que este sync no puede vaciar una propiedad a proposito. Es el
    # intercambio correcto: "no lo se" y "esta vacio" son cosas distintas, y para
    # limpiar un campo a mano estan la UI de HubSpot o un script puntual.
    body = {"inputs": [{"idProperty": "email", "id": e,
                        "properties": {k: str(v) for k, v in p.items() if v is not None}}
                       for e, p in lote]}
    try:
        r = requests.post(f"{HS}/crm/v3/objects/contacts/batch/upsert",
                          headers=_h_hs(), json=body, timeout=60)
    except requests.exceptions.RequestException as e:
        if intento < 5:
            time.sleep(min(2 ** intento, 30))
            return _postear_lote(lote, nivel, intento + 1)
        print(f"  lote de {len(lote)}: error de red tras reintentos: {e}", file=sys.stderr)
        return 0, len(lote), []
    if r.ok:
        return len(lote), 0, []

    if r.status_code in (429, 500, 502, 503, 504) and intento < 5:
        espera = float(r.headers.get("Retry-After", min(2 ** intento, 30)))
        time.sleep(espera)
        return _postear_lote(lote, nivel, intento + 1)

    if r.status_code == 400:
        malos = {m.lower() for m in RE_INVALIDO.findall(r.text)}
        quedan = [(e, p) for e, p in lote if e.lower() not in malos] if malos else lote
        # Solo recursamos si el reintento va a ser distinto del intento actual. Si
        # HubSpot nombra un email que no coincide exacto con ninguna clave del lote
        # (comillas, puntuacion, otra normalizacion), `quedan == lote` y esta rama se
        # llamaria a si misma con el mismo lote hasta reventar por RecursionError.
        if malos and len(quedan) < len(lote) and nivel < 8:
            esc, fal, desc = _postear_lote(quedan, nivel + 1, 0)
            return esc, fal + len(lote) - len(quedan), desc + sorted(malos)
        if not malos and len(lote) > 1 and nivel < 8:
            mitad = len(lote) // 2
            a = _postear_lote(lote[:mitad], nivel + 1, 0)
            b = _postear_lote(lote[mitad:], nivel + 1, 0)
            return a[0] + b[0], a[1] + b[1], a[2] + b[2]

    print(f"  lote de {len(lote)}: {r.status_code} {r.text[:200]}", file=sys.stderr)
    return 0, len(lote), []


def escribir(por_email, dry_run=False, limite=None):
    """Upsert por email en lotes de 100 (batch/upsert con idProperty=email).

    OJO (aprendizajes §13): la respuesta del batch/upsert NO viene en el orden de
    entrada. Aca no se mapea la respuesta, solo se cuenta; si alguna vez hay que
    leerla, mapear por properties[idProperty] y nunca por posicion.
    """
    # `if p` solo probaba que el dict no estuviera vacio. Un contacto con todas sus
    # propiedades en None (p. ej. solo alcanzado por circle_posts() con una fecha que
    # no parsea) pasaba el filtro y, como el write es batch/upsert por email, CREABA
    # un contacto pelado en HubSpot con nada mas que el email.
    items = [(e, p) for e, p in por_email.items()
             if any(v is not None for v in p.values())]
    descartados_regex = [e for e, _ in items if not RE_EMAIL.match(e)]
    if descartados_regex:
        items = [(e, p) for e, p in items if RE_EMAIL.match(e)]
        print(f"  {len(descartados_regex)} emails con forma invalida, se saltan: "
              f"{descartados_regex[:5]}{' ...' if len(descartados_regex) > 5 else ''}",
              file=sys.stderr)
    if limite:
        items = items[:limite]
    print(f"\nA escribir: {len(items)} contactos", file=sys.stderr)
    if dry_run:
        for email, props in items[:10]:
            print(f"  [dry-run] {email}: {props}")
        print(f"  [dry-run] ... y {max(0, len(items)-10)} mas")
        return {"escritos": 0, "dry_run": len(items)}

    escritos = fallidos = 0
    rechazados = []
    for i in range(0, len(items), 100):
        e, f, desc = _postear_lote(items[i:i+100])
        escritos += e
        fallidos += f
        rechazados += desc
        time.sleep(0.2)
    print(f"  escritos={escritos} fallidos={fallidos}", file=sys.stderr)
    if rechazados:
        print(f"  emails rechazados por HubSpot ({len(rechazados)}): "
              f"{rechazados[:10]}{' ...' if len(rechazados) > 10 else ''}", file=sys.stderr)
    # `rechazados` = emails que HubSpot nombro como invalidos en el cuerpo del 400.
    # `inexplicados` = fallidos que nadie explico: lote caido por token vencido, rate
    # limit agotado, 500. Esa es la unica cifra que debe tumbar la corrida (ver main).
    inexplicados = max(0, fallidos - len(rechazados))
    if inexplicados:
        print(f"  ⚠ {inexplicados} fallidos SIN explicacion de HubSpot", file=sys.stderr)
    return {"escritos": escritos, "fallidos": fallidos,
            "inexplicados": inexplicados,
            "rechazados_hubspot": rechazados,
            "rechazados": rechazados + descartados_regex}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--solo", choices=["circle", "brevo"])
    ap.add_argument("--limite", type=int, help="tope de contactos a escribir")
    ap.add_argument("--generar-mapeo", action="store_true")
    a = ap.parse_args()

    # Antes de gastar 20 minutos de llamadas: si falta un token, fallar acá.
    exigir_env()

    if a.generar_mapeo:
        generar_mapeo()
        return

    datos = {}
    if a.solo in (None, "circle"):
        for email, props in circle_todo().items():
            datos.setdefault(email, {}).update(props)
    if a.solo in (None, "brevo"):
        for email, props in brevo_todo().items():
            datos.setdefault(email, {}).update(props)
    resultado = escribir(datos, dry_run=a.dry_run, limite=a.limite)

    # Fallar SOLO por fallos que HubSpot no explico.
    #
    # Antes esto era `if resultado.get("fallidos")`, un test de verdad sobre un entero:
    # 22.541 escritos de 22.563 salia rojo igual que 0 escritos. Y los 22 fallidos son
    # emails con TLD malformado heredados de Salesforce (.ocm/.con/.c/.cpm/.ccom) que
    # HubSpot rechaza contra su propia lista: son los MISMOS 22 corrida tras corrida y
    # no hay reintento que los vuelva validos. O sea, el job salia rojo todas las
    # semanas por una razon que no cambia nunca.
    #
    # El costo real de eso no era el color: el paso de read-back del workflow lleva un
    # `success()` implicito, asi que verificar_scoring.py NUNCA corria. La unica guarda
    # del frente estaba desactivada por un fallo benigno y permanente.
    #
    # No se usa una allowlist de los 22 porque seria exacta hoy y quedaria vieja sin
    # que nadie la actualice. Esto usa datos que escribir() ya devuelve.
    if resultado.get("inexplicados"):
        raise SystemExit(1)
    if resultado.get("rechazados_hubspot"):
        print(f"\nOK con reparos: {len(resultado['rechazados_hubspot'])} contactos no se "
              f"pudieron escribir por email invalido en el origen (Brevo/Circle). "
              f"Se corrigen alla o se aceptan; no son un fallo de este sync.",
              file=sys.stderr)


if __name__ == "__main__":
    main()
