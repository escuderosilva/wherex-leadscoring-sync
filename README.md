# Sync Circle + Brevo → HubSpot (lead scoring de marketing)

Alimenta el lead scoring **nativo** de HubSpot con señales de la comunidad (Circle) y
del email marketing (Brevo).

Corre solo en **GitHub Actions**. No depende de que ninguna laptop esté prendida.

| | |
|---|---|
| **Qué escribe** | 25 propiedades de Contacto en HubSpot (`circle_*` y `brevo_*`), más `firstname`/`lastname`/`jobtitle`/`country` **sólo donde están vacías** |
| **Qué NO hace** | **crear contactos.** Actualiza los que existen, por `hs_object_id`. Ver §Este sync no crea contactos |
| **Circle** | **todos los días a las 07:07 de Chile** · `.github/workflows/sync-diario.yml` · ~40 s. Son **dos** entradas de cron (11:07 UTC de abr-ago, 10:07 UTC de sep-mar) porque GitHub sólo entiende UTC y Chile cambia de hora. |
| **Brevo** | **miércoles** 11:23 UTC (≈ 07:23 Chile) · `.github/workflows/sync-semanal.yml` · 20-25 min. El newsletter sale los **martes**: lo recoge ~24 h después del envío. |
| **Portal HubSpot** | 51404466 (producción) |

## Por qué Circle va a diario y Brevo no

La fase de Brevo son ~22.500 llamadas y 20-25 minutos, porque pide estadísticas
contacto por contacto. Circle son ~400 miembros y corre en menos de un minuto. Correr
Brevo a diario no aportaría nada — el newsletter es semanal — y correr Circle una vez
por semana deja la actividad de la comunidad hasta 7 días vieja para el scoring.

Un cron diario además es **más** confiable que uno semanal: si un día no dispara, al
día siguiente se repara solo; cuando se salta el semanal son 14 días de datos
congelados. El sync es idempotente, así que repetirlo no cuesta nada.

Los dos workflows comparten `concurrency: group: sync-circle-brevo`. Los grupos de
concurrency son del **repo**, no del workflow, así que eso serializa el diario contra
el semanal: nunca escriben el mismo contacto a la vez.

## Las tres guardas, y qué modo de falla cubre cada una

Este frente ya falló en silencio de tres formas distintas (launchd muriendo por TCC,
un read-back que nunca corría, 44 listas de Brevo contadas como actividad académica).
El patrón común no fue el error: fue que **nadie se enteró**. Cada guarda cubre un
modo distinto y ninguna reemplaza a otra.

| Guarda | Qué detecta | Latencia | Dónde |
|---|---|---|---|
| `MIN_MIEMBROS_CIRCLE = 300` | La API de Circle responde 200 con lista vacía (token revocado, cambio de contrato). Sin esto el sync escribe 0 contactos y sale **verde**. | Cero: corta antes de escribir | `sync_circle_brevo.py` |
| `FRESCURA_DIAS/MIN` | Datos **congelados**: los valores de ayer siguen en HubSpot, así que los pisos de cobertura pasan para siempre. Cuenta visitas a Circle en los últimos 7 días. | Días | `verificar_scoring.py` |
| Dead-man's switch | Que el cron **no corrió**. Ninguna alerta dentro de GitHub Actions puede avisar de esto. | Horas | `HEARTBEAT_*_URL` |

Los pisos de cobertura (`PISOS_COBERTURA`) prueban que una propiedad **tiene** datos,
no que sean de hoy. Ésa es la razón de existir del detector de frescura.

## El read-back va con el mismo alcance que el sync

`verificar_scoring.py --solo circle` existe para que el job diario **no pueda fallar
por una señal de Brevo que no tocó**. A 365 corridas por año, una alerta que se pone
roja por algo que el job no controla se aprende a ignorar en una semana — es el mismo
error que tuvo este repo hasta el 2026-08-04, cuando el job salía rojo todos los
miércoles por 22 emails con TLD malformado y eso mantenía el read-back desactivado.

## Este sync no crea contactos

Antes sí, sin que nadie lo hubiera decidido. Escribía con `batch/upsert` +
`idProperty=email`, que es **create-or-update**: cada email de Brevo o Circle que no
existía en HubSpot entraba como **contacto nuevo** con nada más que propiedades de
marketing — sin nombre, sin empresa y sin `status_contacto`.

Lo que costó, medido el 2026-08-11: **9.593 contactos sin nombre** en el portal. 6.623 por
el import del 21-jul y **2.964 por las corridas del 29 y 30 de julio de este sync**. Esos
2.964 aparecen en HubSpot como `INTEGRACIÓN · Migración API`, no como Brevo, porque este
sync usa **la misma app privada que el ETL de Salesforce** (`hs_object_source_id
43401597`): el informe de calidad de datos los contó como migración durante dos semanas.

Cómo quedó:

1. `resolver()` hace `batch/read` por email antes de escribir (~226 llamadas para 22,5k
   emails, ~2 min). Sabe quién existe y quién no.
2. Los que existen se actualizan con **`batch/update` por `hs_object_id`**. Ese endpoint no
   puede crear: la clase de bug entera desaparece, no se mitiga.
3. Los que no existen van a **`contactos_sin_crear.csv`** y no entran al CRM. Brevo es el
   sistema de audiencia; HubSpot es el CRM. Un suscriptor del newsletter sin nombre y sin
   empresa no es un registro de CRM: es una fila de una lista. La decisión de qué hacer con
   ellos es de marketing, no de un cron.

**Dos fallos viejos se van de arrastre**, porque el update por id no valida el email: los
**22 contactos con TLD malformado** heredados de Salesforce (`.ocm`, `.con`, `.cpm`) por fin
reciben sus propiedades — era la única causa por la que el job salía rojo todas las semanas
— y desaparece el bug "un email inválido tumba el lote de 100".

### Y el nombre venía en la misma llamada

`brevo_contactos()` hacía `GET /contacts` y se quedaba **sólo con `emailBlacklisted`**. En
el `attributes` que descartaba venía FIRSTNAME (79,7% de la cuenta), COMPANY (75,3%),
COUNTRY (73,8%), LASTNAME (55,9%) y JOB_TITLE (46,5%).

Ahora se mapea, con dos reglas que no son negociables:

- **Sólo se escribe si el campo está vacío en HubSpot.** Nunca encima de un valor
  existente: las cargas CSV del 20/21/23-jul pisaron 32.295 nombres y repararlos costó un
  frente entero. Un cron desatendido no puede tener permiso de sobrescribir identidad.
- **El split resta, no copia.** Brevo guarda el nombre completo en `FIRSTNAME` y sólo el
  primer apellido en `LASTNAME` (`'Catalina Andrea Araya Tejada'` + `'Araya'`). Copiar
  `FIRSTNAME` tal cual reproduce exactamente el daño de los 32.295. La lógica está en
  `identidad_brevo.py`, con 18 casos de autoprueba (`python3 identidad_brevo.py`), y lo
  ambiguo **no se escribe**.

`COMPANY` se lee pero no se escribe: en ese portal la empresa del contacto vive en la
asociación a la Empresa, no en el campo de texto. Sale en el CSV para el frente que
corresponde.

> `identidad_brevo.py` es una **copia autocontenida** de
> `calidad_datos/brevo_identidad/nombres_brevo.py` del repo `hubspot_admin`, que es la
> fuente de verdad. Son dos repos y no se pueden importar entre sí. **Si cambias la lógica,
> cambiala en las dos**; las dos corren la misma autoprueba.

## División de trabajo (importante)

Este script escribe **fechas y números crudos**. La ventana temporal ("en los últimos 90
días") la calcula el **scoring nativo de HubSpot**. La única excepción es
`brevo_nl_aperturas_90d`: HubSpot no puede contar aperturas por campaña, así que esa
ventana se calcula acá y se reescribe entera en cada corrida.

Las **reglas y puntajes** del scoring no viven acá: viven en `leadscoring/Scoring_MQL.md`
del repo `hubspot_admin`. El scoring nativo **no tiene API**, así que si alguien cambia las
reglas en la UI y no las refleja en ese documento, se pierden.

## Relación con el repo `hubspot_admin`

`hubspot_admin` es la **fuente de verdad del modelo** (qué significa cada propiedad, el
inventario en `esquema/inv_contacto.py`, las reglas de scoring). Este repo es el
**runtime**: el código que corre y su programación.

Se separó por dos razones concretas:

1. **`hubspot_admin` no puede salir del disco.** Tiene 69.303 contactos con nombre, email y
   teléfono, más credenciales de Salesforce, HubSpot, Brevo, Circle, Supabase y GA4. Subirlo
   a GitHub —incluso privado— es exponer datos personales que el cron no necesita.
2. **Antes había copias duplicadas.** El script vivía en `circle-brevo/` y en `leadscoring/`,
   y los jobs de `launchd` corrían una tercera copia en `~/Downloads/`. Editar una no
   afectaba a las otras. Acá hay **una sola**.

## Uso local

```bash
pip install -r requirements.txt

# los tokens salen de un .env junto al script, o del entorno
python3 sync_circle_brevo.py --dry-run              # calcula y muestra, no escribe
python3 sync_circle_brevo.py --solo circle          # una sola fuente (~10 s)
python3 sync_circle_brevo.py --limite 50            # smoke test acotado
python3 sync_circle_brevo.py                        # completo, escribe
python3 verificar_scoring.py                        # read-back de las dos fuentes
python3 verificar_scoring.py --solo circle          # read-back de Circle (8 searches)
```

En la nube, a demanda, sin clonar nada:

```bash
gh workflow run sync-diario.yml                     # Circle ahora
gh workflow run sync-semanal.yml                    # Brevo ahora
gh workflow run sync-semanal.yml -f solo=ambas      # las dos fuentes en una pasada
gh workflow run sync-diario.yml -f dry_run=true     # calcula sin escribir
```

Es **idempotente**: recalcula desde la fuente y hace upsert por email. Correrlo dos veces
seguidas deja el mismo estado.

## Secretos

Ninguno vive en el repo:

| Variable | Obligatorio | Dónde |
|---|---|---|
| `HUBSPOT_TOKEN` | sí | GitHub → Settings → Secrets and variables → Actions |
| `BREVO_API_KEY` | sí | ídem |
| `CIRCLE_API_TOKEN` | sí | ídem |
| `HEARTBEAT_DIARIO_URL` | recomendado | URL de ping de Healthchecks.io (`period 1 day`, `grace 6 hours`) |
| `HEARTBEAT_SEMANAL_URL` | recomendado | ídem (`period 7 days`, `grace 12 hours`) |
| `SLACK_WEBHOOK_URL` | recomendado | Webhook entrante de Slack para el aviso de falla |

Los tres opcionales **no rompen nada si faltan**: el paso avisa con un `::warning::` y
sigue. Pero sin `HEARTBEAT_DIARIO_URL` este sync no tiene dead-man's switch, y ése es
el único mecanismo capaz de detectar que el cron dejó de disparar.

⚠️ Al rotar un token, actualizar el secret **en la misma tanda**. Orden correcto:
rotar → `gh secret set` → `gh workflow run sync-diario.yml` para verificar en vivo →
recién ahí revocar el viejo. Si se rota sin actualizar el secret, el job muere mañana a
las 07:07 y el aviso va a un mail que nadie mira.

En local salen de un `.env` junto al script (está en `.gitignore`). `load_env()` usa
`setdefault`, así que **una variable ya presente en el entorno gana sobre el archivo**: en
CI manda el secret, en local manda tu `.env`. Si falta alguno, el script falla en la primera
línea con un mensaje claro en vez de morir con un `KeyError` a los 20 minutos.

## `actividades_mkt.csv`: la columna `categoria` es un contrato, no una anotación

El CSV que cura marketing dice, por cada lista de Brevo, **qué es**. `brevo_actividades()`
la consume así:

| `categoria` | Qué hace el sync |
|---|---|
| `academica` | cuenta según `tipo` (registro / asistencia / certificado) y, si `paga`, suma a `academico_asistencias_pagas_12m` |
| `evento_wherex` | suma `eventos_wherex_12m` |
| `evento_pl` | **se saltea**: desde 2026-07-30 sale del RSVP real de Circle, no de la audiencia de invitación |
| `ignorar` | **se saltea**: son segmentos de prospección y audiencias de campaña, no actividades |

⚠️ **Si agregas una categoría al generador, tienes que consumirla acá.** Hasta el
2026-08-10 `ignorar` se escribía y no se leía: las 44 listas marcadas así caían al `else`
final y cada una sumaba `academico_registros_12m`. Medido ese día contra Brevo: las listas
académicas de verdad son **6.758** emails distintos y las de `ignorar` **18.540** (15.602
jamás estuvieron en una académica), con la propiedad poblada en **16.664** contactos de
HubSpot. Estar en "Prospectos Fit - México" contaba como haberse registrado a una actividad
académica, y el scoring nativo puntuaba sobre eso.

**El arreglo no limpia lo ya escrito** — ver "Un `None` se omite" acá abajo: este sync no
vacía propiedades. Los ~10.000 contactos que quedaron con un valor inflado necesitan un
backfill de una vez, que vive en `hubspot_admin/leadscoring/`.

## Dos decisiones de diseño que conviene no revertir sin pensarlo

### Un `None` se omite, no se manda como `""`

En HubSpot un string vacío **borra** la propiedad. La versión anterior mandaba
`"" if v is None else str(v)`, así que cualquier métrica que la fuente no devolviera se
limpiaba. En un job desatendido eso convierte una falla transitoria de la API en
pérdida permanente de datos —y a diario, la convierte todos los días—: las 4 métricas
`circle_activity_score`, `_presence`,
`_participacion` y `_contribucion` salen de un objeto anidado (`activity_score`) que Circle
**no incluye para miembros sin actividad**; si algún día dejara de incluirlo para todos, la
corrida siguiente habría vaciado los 337 contactos que hoy lo tienen.

La contracara: este sync **no puede vaciar** una propiedad a propósito. Es el intercambio
correcto — "no lo sé" y "está vacío" son cosas distintas.

### Un `400` de HubSpot no invalida el lote entero

Un solo email con forma inválida hace fallar el batch de 100 completo (así se perdieron
1.700 contactos el 2026-07-29). `_postear_lote()` extrae del mensaje de error los emails
culpables, los saca y reintenta; si el `400` es por otra cosa, parte el lote en dos para que
un registro malo no arrastre a los otros 99.

## El read-back no es opcional

El sync puede terminar "verde" con lotes parcialmente rechazados. `verificar_scoring.py`
cuenta los contactos **directamente en HubSpot** en vez de confiar en el log, y el workflow
lo corre después de cada sync. Si falla, la corrida falla.

### Una propiedad que el sync ya no puede corregir

`eventos_pl_asistencias_12m` está poblada en **1.892** contactos, de los cuales **1.831
no son miembros de Circle** (medido 2026-08-11). Vienen del diseño anterior, que la
aproximaba con listas de Brevo (`Inscritos - Eventos - <País>`); desde el 2026-07-30 sale
del RSVP real de Circle, que hoy cubre 6 eventos y 61 contactos.

Como el sync **omite `None`** (ver abajo, y es la decisión correcta), esos 1.831 valores
no se pisan ni se bajan nunca: `circle_eventos()` sólo devuelve a quien tiene un RSVP
dentro de la ventana de 365 días, y a quien se le vence simplemente desaparece del dict.
O sea que es un contador de ventana móvil **que sólo puede subir**, y el scoring nativo
está puntuando sobre eso.

Correr a diario no lo arregla y lo hace parecer más fresco de lo que está. Es la misma
clase de falla que el bug de la categoría `ignorar`, y necesita el mismo tratamiento: un
backfill deliberado de una vez, con su guarda — no un cambio a la regla de omitir `None`.
Anotado en `hubspot_admin/ESTADO.md`.

## Historia: qué reemplaza esto

Dos jobs de `launchd` en el Mac de Emilio, que fallaban en silencio:

| Job | Programado | Qué pasaba |
|---|---|---|
| `com.procure.circle-hubspot-sync` | lunes 07:00 | Falló desde el 2026-07-27: `Operation not permitted`. macOS (TCC) no deja que `launchd` lea `~/Downloads` sin *Full Disk Access*. Última corrida exitosa: 2026-07-21, **a mano**. |
| `com.procure.brevo-hubspot-sync` | viernes 07:00 | Nunca alcanzó a correr programado. |

Además ambos ejecutaban los scripts **viejos** (13 propiedades entre los dos); el sync
unificado que alimenta el scoring —este— no estaba programado en ninguna parte.

✅ **Los tres jobs de `launchd` se eliminaron el 2026-08-04** (`bootout` + `remove` + `rm`
de los plists, verificado con `launchctl list` vacío). Hasta ese día seguían cargados y
disparando aunque el tracker los daba por desinstalados. No fallaban por diseño sino por
TCC, y ése era el único motivo por el que no colisionaban: el viejo de Circle estaba
programado el **mismo instante** que GitHub Actions sobre las mismas propiedades. Conceder
*Full Disk Access* —la reacción natural frente a `Operation not permitted`— habría
arrancado la colisión. El relato está en `hubspot_admin/_historico/`.

### Una propiedad que se perdió en el cambio, a propósito

`circle_likes_dados` (75 contactos con dato, congelada) la escribía el sync **viejo** de
Circle y este **no**. **No la portes.** En el código viejo la línea es
`likes[em] = p["member_likes_count"]` **dentro del loop de posts**: es last-write-wins del
último post de ese autor, no una suma ni un total del miembro (el campo a nivel miembro no
existe en `community_members`). Portarlo sería portar un agregado basura. Decisión
pendiente de marketing: archivar la propiedad, o definir bien qué medir y calcularlo.

## Dos gotchas de GitHub Actions que ya nos costaron

**1. GitHub desactiva los workflows programados** de un repo sin actividad por 60 días, y
la actividad que cuenta es un **commit** — no las corridas del propio cron. Un repo que
sólo tiene un sync andando solo, funcionando perfecto, se apaga a los 60 días por hacer
exactamente lo que se le pidió. Lo previene `keep-alive.yml` (commitea `.latido` el día 1
de cada mes, dos veces por dentro de la ventana); si igual pasa, lo detecta el dead-man's
switch. `keep-alive.yml` también es un cron y también puede quedar desactivado: por eso
hacen falta las dos cosas.

**2. El cron no dispara a la hora que dice.** Medido en este repo:

| Cron nominal | Disparó | Atraso |
|---|---|---|
| `0 11` — lun 2026-08-03 | 17:28 UTC | **6 h 28 min** |
| `0 11` — lun 2026-08-10 | 14:40 UTC | **3 h 40 min** |
| `17 10` — mié 2026-08-12 | 11:09 UTC | **52 min** |
| `23 11` — mié 2026-08-12 | 12:03 UTC | **40 min** |

El minuto 0 de una hora en punto es el slot más congestionado del scheduler compartido, y
la tercera y cuarta fila son la evidencia de que moverse de ahí sirve: de horas a menos de
una. Los workflows de acá usan minutos impares (07, 23, 41) por eso.

Pero **no lo elimina**, y el atraso siempre empuja hacia adelante: "07:07 de Chile"
significa en la práctica *temprano en la mañana*, no las 07:07. No construyas nada que
dependa de la hora exacta, y dale al dead-man's switch una holgura de horas, no de
minutos. El 2026-08-10 alguien revisó a las 14:05, no vio la corrida y la dio por no
ejecutada; había disparado 35 minutos después.
