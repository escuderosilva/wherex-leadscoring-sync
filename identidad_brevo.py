#!/usr/bin/env python3
"""Parte el nombre que trae Brevo en `firstname` + `lastname`. Determinista.

**Copia deliberada.** El original vive en el repo `hubspot_admin`
(`calidad_datos/brevo_identidad/nombres_brevo.py`), que es la fuente de verdad del
modelo. Son dos repos y no se pueden importar entre sí, así que esta copia es
autocontenida: los helpers de `normalizar.py` y `plan_nombres.py` que necesita están
inlineados abajo en vez de arrastrar dos archivos de 23 KB de otro frente a un repo
que existe para ser mínimo. **Si cambias la lógica, cambiala en los dos.**
La autoprueba (`python3 identidad_brevo.py`) es la misma en ambos lados: si los dos
archivos pasan los mismos 18 casos, no divergieron en lo que importa.

La regla, en una línea: **el `LASTNAME` de Brevo marca dónde se separan los nombres
de pila de los apellidos dentro del `FIRSTNAME`.** Brevo guarda el nombre completo en
`FIRSTNAME` y, cuando hay `LASTNAME`, sólo el primer apellido:

    FIRSTNAME='Catalina Andrea Araya Tejada'  LASTNAME='Araya'
      -> firstname='Catalina Andrea'  lastname='Araya Tejada'

Lo que no se puede inferir queda en banda `revisar` y NO se escribe. Esa cautela es
la lección del frente de nombres: el daño de 32.295 contactos vino de escribir un
nombre completo en `firstname`, y se reparó restaurando del historial, no adivinando.
"""
from __future__ import annotations

import re
import unicodedata

# --------------------------------------------------- helpers inlineados
PARTICULAS = {"de", "del", "la", "las", "los", "el", "y", "da", "das", "do",
              "dos", "van", "von", "di", "le", "san", "santa"}
TRATAMIENTOS = {"sr", "sra", "srta", "don", "dona", "ing", "inge", "lic", "dr",
                "dra", "mr", "mrs", "ms", "arq", "cp", "cpa", "mba", "prof"}
PLACEHOLDERS = {"", "unknown", "unk", "na", "n/a", "-", ".", "..", "sin", "nombre",
                "sinnombre", "noname", "desconocido", "test", "prueba", "dummy",
                "null", "none", "vacio", "xx", "xxx", "cliente", "contacto"}
BASURA_RE = re.compile(
    r"\b(test|testing|prueba|pruebas|dummy|asdf|qwerty|xxx+|aaa+|nn|na|sin nombre|"
    r"no name|desconocido|borrar|delete|ejemplo|example|demo)\b", re.I)
# Tokens de sistema: si aparece UNO, el valor no es el nombre de una persona.
# El test de placeholder exige que TODOS los tokens lo sean, así que 'Zoom user' se
# le escapa y termina escrito como nombre de pila en producción.
BASURA_TOKENS = {"zoom", "user", "usuario", "invitado", "guest", "admin", "soporte",
                 "sistema", "notificaciones", "noreply", "webinar", "asistente"}
ROL_RAICES = ("compras", "ventas", "info", "contacto", "adquisicion", "abastecimiento",
              "administra", "gerencia", "cobranza", "facturacion", "logistica",
              "licitacion", "proveedor", "recepcion", "soporte", "rrhh", "pagos")
RE_JURIDICO = re.compile(
    r"\b(s\.?a\.?c?\.?|s\.?a\.?s\.?|spa|ltda?|e\.?i\.?r\.?l\.?|cia|s\.?r\.?l\.?|"
    r"inc|llc|corp|s\.? de r\.?l\.?|c\.?v\.?)\b\.?$")


def sin_acentos(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s)
                   if not unicodedata.combining(c))


def norm_texto(s: object) -> str:
    if not s:
        return ""
    t = sin_acentos(str(s)).lower()
    t = re.sub(r"[^a-z0-9ñ ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def local(email: str) -> str:
    return (email or "").split("@")[0]


def es_buzon_rol(email: str) -> bool:
    """Buzón funcional (compras@, info@): su local part no dice nada del nombre."""
    lp = re.sub(r"[^a-z0-9]", "", norm_texto(local(email)).replace(" ", ""))
    return bool(lp) and any(r in lp for r in ROL_RAICES)


def es_nombre_basura(fn: object, ln: object) -> bool:
    toks = [t for t in (norm_texto(fn) + " " + norm_texto(ln)).split()
            if t not in TRATAMIENTOS and len(t) > 1]
    n = " ".join(toks)
    if not n or BASURA_RE.search(n):
        return True
    plano = n.replace(" ", "")
    return bool(re.fullmatch(r"(.)\1*", plano)) or plano.isdigit()


def es_placeholder(s: object) -> bool:
    t = norm_texto(s).split()
    return not t or all(x in PLACEHOLDERS for x in t)


def capitalizar(nombre: str) -> str:
    """'GIANCARLO' -> 'Giancarlo'. Sólo actúa si viene TODO mayúsculas o TODO minúsculas."""
    limpio = (nombre or "").strip()
    if not limpio or not (limpio.isupper() or limpio.islower()):
        return limpio
    out = []
    for i, p in enumerate(limpio.split()):
        base = sin_acentos(p).lower()
        out.append(p.lower() if i > 0 and base in PARTICULAS
                   else p[:1].upper() + p[1:].lower())
    return " ".join(out)


def parece_razon_social(s: str) -> bool:
    bruto = (s or "").strip()
    alfa = re.sub(r"[^a-z]", "", sin_acentos(bruto).lower())
    return bruto.isupper() and len(alfa) >= 14 and len(bruto.split()) == 1


# ------------------------------------------------------------- la lógica
def _limpio(s: object) -> str:
    return re.sub(r"\s+", " ", str(s or "").strip())


def _toks(s: str) -> list[str]:
    return [norm_texto(t) for t in s.split() if norm_texto(t)]


def _significativos(partes: list[str]) -> int:
    """'Maria del Carmen' son 2 significativos, no 3: es compuesto, no completo."""
    return sum(1 for p in partes if norm_texto(p) not in PARTICULAS)


def _armar(partes: list[str], corte: int, forma: str) -> dict:
    """Corta en `corte` y decide la banda.

    Un lado con 3+ tokens significativos es ambiguo: puede ser un segundo nombre
    que quedó del lado del apellido ('Laura Alejandra Nuñez Ruiz' + 'Laura') o un
    apellido compuesto legítimo ('Suazo Luna Victoria'). No hay señal, no se elige.
    """
    izq, der = partes[:corte], partes[corte:]
    if not izq or not der:
        return {"firstname": "", "lastname": "", "banda": "revisar", "forma": f"{forma}_vacio"}
    if _significativos(der) >= 3 or _significativos(izq) >= 3:
        return {"firstname": "", "lastname": "", "banda": "revisar", "forma": f"{forma}_ambiguo"}
    return {"firstname": " ".join(izq), "lastname": " ".join(der),
            "banda": "auto", "forma": forma}


def _sub_indice(grande: list[str], chico: list[str]) -> int:
    if not chico or len(chico) > len(grande):
        return -1
    for i in range(len(grande) - len(chico) + 1):
        if grande[i:i + len(chico)] == chico:
            return i
    return -1


def _corregir_particula(partes: list[str], corte: int) -> int:
    while corte > 0 and norm_texto(partes[corte - 1]) in PARTICULAS:
        corte -= 1
    return corte


def _por_email(partes: list[str], email: str) -> int:
    """Corte deducido del local part del buzón; 0 si no se puede.

        jruiz@      -> 'j' + 'ruiz'     -> Jesús | Ruiz Esparza
        jcurihual@  -> 'j' + 'curihual' -> Juan Carlos | Curihual
    """
    if not email or es_buzon_rol(email):
        return 0
    lp = norm_texto(local(email)).replace(" ", "").replace(".", "")
    if not lp:
        return 0
    t = [norm_texto(p) for p in partes]
    for i in range(1, len(t)):
        if len(t[i]) < 4:
            continue
        iniciales = "".join(x[0] for x in t[:i])
        nombres = "".join(t[:i])
        # `t[0][0] + t[i]` cubre el buzón que usa sólo la inicial del PRIMER nombre y
        # saltea el segundo: 'Juan Carlos Curihual' -> jcurihual (no jccurihual).
        if lp in (iniciales + t[i], nombres + t[i], t[0][0] + t[i], t[0] + t[i],
                  t[i] + iniciales, t[i] + nombres):
            return i
    return 0


def partir(firstname: object, lastname: object, email: str = "") -> dict:
    """Devuelve {firstname, lastname, banda, forma}.

    banda: auto (escribible) · parcial (sólo nombre de pila) · revisar (no se
    escribe, va a cola) · descartar (no es una persona).
    """
    fn, ln = _limpio(firstname), _limpio(lastname)
    if not fn and not ln:
        return {"firstname": "", "lastname": "", "banda": "descartar", "forma": "sin_datos"}
    if not fn:
        fn, ln = ln, ""

    if es_placeholder(fn) or es_nombre_basura(fn, ln):
        return {"firstname": "", "lastname": "", "banda": "descartar", "forma": "placeholder"}
    if set(_toks(fn)) & BASURA_TOKENS:
        return {"firstname": "", "lastname": "", "banda": "descartar", "forma": "token_de_sistema"}
    if RE_JURIDICO.search(sin_acentos(fn).lower()) or parece_razon_social(fn):
        return {"firstname": "", "lastname": "", "banda": "descartar", "forma": "razon_social"}

    fn, ln = capitalizar(fn), capitalizar(ln)
    # `partes` y `tf` tienen que quedar ALINEADOS uno a uno: el corte se calcula sobre
    # los normalizados y se aplica sobre los originales. Un token que normaliza a
    # vacío ('-', '.') desalinea los dos y el corte cae una posición corrida.
    partes = [p for p in fn.split() if norm_texto(p)]
    if not partes:
        return {"firstname": "", "lastname": "", "banda": "descartar", "forma": "sin_tokens"}
    fn, tf = " ".join(partes), [norm_texto(p) for p in partes]
    ta = _toks(ln)

    if ta:
        if tf == ta:
            return _sin_apellido(fn, partes, email, "identicos")
        i = _sub_indice(tf, ta)
        if i > 0:
            # El LASTNAME va DESPUÉS de los nombres de pila: corta donde empieza.
            corte = _corregir_particula(partes, i)
            if corte > 0:
                return _armar(partes, corte, "corte_por_apellido")
        if i == 0:
            # El LASTNAME es el PREFIJO del FIRSTNAME. Medido sobre 6.791 casos
            # reales, eso NO es "apellido al frente": es el campo LASTNAME relleno
            # con el NOMBRE DE PILA, y el apellido es el resto del FIRSTNAME.
            #   'Carlos Durán' + 'Carlos' -> Carlos | Durán   (no 'Durán' | 'Carlos')
            if len(ta) < len(partes):
                return _armar(partes, len(ta), "nombre_en_lastname")
            return {"firstname": "", "lastname": "", "banda": "revisar",
                    "forma": "solo_apellido"}
        if i < 0:
            # Son dos campos de verdad... salvo que el FIRSTNAME ya traiga el nombre
            # completo y el LASTNAME sea otro apellido ('GABRIELA RAMIREZ PINZON' +
            # 'Morales Castillo'): escribir eso reproduce el daño de los 32.295.
            if _significativos(partes) >= 3:
                return {"firstname": "", "lastname": "", "banda": "revisar",
                        "forma": "campos_separados_ambiguo"}
            return {"firstname": fn, "lastname": ln,
                    "banda": "auto", "forma": "campos_separados"}

    return _sin_apellido(fn, partes, email, "un_solo_campo")


def _sin_apellido(fn, partes, email, forma_base) -> dict:
    n = len(partes)
    if n == 1:
        return {"firstname": fn, "lastname": "", "banda": "parcial",
                "forma": f"{forma_base}_1tok"}
    if n == 2:
        return _armar(partes, 1, f"{forma_base}_2tok")
    corte = _por_email(partes, email)
    if corte:
        corte = _corregir_particula(partes, corte)
    if not corte and n == 4 and not any(norm_texto(p) in PARTICULAS for p in partes):
        corte = 2                      # convención hispana: 2 nombres + 2 apellidos
    if corte:
        return _armar(partes, corte, f"{forma_base}_{n}tok_corte{corte}")
    return {"firstname": "", "lastname": "", "banda": "revisar",
            "forma": f"{forma_base}_{n}tok_ambiguo"}


# --------------------------------------------------------------- autoprueba
CASOS = [
    ("Catalina Andrea Araya Tejada", "Araya", "caraya@x.cl", "Catalina Andrea", "Araya Tejada", "auto"),
    ("Felipe Lira", "Lira", "flira@x.cl", "Felipe", "Lira", "auto"),
    ("Kathya", "Ahumada", "kahumada@x.cl", "Kathya", "Ahumada", "auto"),
    ("Gaston Cortez", "", "gcortez@x.cl", "Gaston", "Cortez", "auto"),
    ("Melissa", "", "melissa@x.cl", "Melissa", "", "parcial"),
    ("Juan Carlos Curihual", "", "jcurihual@x.cl", "Juan Carlos", "Curihual", "auto"),
    ("Jesús Ruiz Esparza", "", "jruiz@x.mx", "Jesús", "Ruiz Esparza", "auto"),
    ("Mervyn Ayres Cortés", "", "nada@x.cl", "", "", "revisar"),
    ("Carlos Durán", "Carlos", "cduran@x.cl", "Carlos", "Durán", "auto"),
    ("CARLA PEREZ GONZALES", "Carla", "x@x.pe", "Carla", "Perez Gonzales", "auto"),
    ("GABRIELA RAMIREZ PINZON", "Morales Castillo", "x@x.co", "", "", "revisar"),
    ("Maria del Carmen", "Navarro Gallo", "x@x.pe", "Maria del Carmen", "Navarro Gallo", "auto"),
    ("Laura Alejandra Nuñez Ruiz", "Laura", "x@x.cl", "", "", "revisar"),
    ("Georgeny Julio Nossa Morales", "", "x@x.co", "Georgeny Julio", "Nossa Morales", "auto"),
    ("MARIA DEL CARMEN VIRGINIA VELAZQUEZ MARTINEZ", "", "x@x.mx", "", "", "revisar"),
    ("TRANSPORTES MENAR Y CIA LTDA.", "", "x@x.cl", "", "", "descartar"),
    ("VERONICA DEL CARMEN ALVAREZ OLIVARES", "Alvarez", "x@x.cl",
     "Veronica del Carmen", "Alvarez Olivares", "auto"),
    ("", "", "x@x.cl", "", "", "descartar"),
]

if __name__ == "__main__":
    fallos = 0
    for fn, ln, em, efn, eln, eb in CASOS:
        r = partir(fn, ln, em)
        ok = (r["firstname"], r["lastname"], r["banda"]) == (efn, eln, eb)
        fallos += not ok
        print(f"{'ok ' if ok else 'FALLA'}  {fn!r:48.48} + {ln!r:10.10} -> "
              f"{r['firstname']!r} | {r['lastname']!r}  [{r['banda']}/{r['forma']}]"
              + ("" if ok else f"   ESPERADO {efn!r} | {eln!r} [{eb}]"))
    print(f"\n{len(CASOS) - fallos}/{len(CASOS)} casos ok")
    raise SystemExit(1 if fallos else 0)
