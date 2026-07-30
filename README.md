# Sync Circle + Brevo → HubSpot (lead scoring de marketing)

Sync **semanal** que alimenta el lead scoring **nativo** de HubSpot con señales de la
comunidad (Circle) y del email marketing (Brevo).

Corre solo en **GitHub Actions**, los lunes. No depende de que ninguna laptop esté prendida.

| | |
|---|---|
| **Qué escribe** | 25 propiedades de Contacto en HubSpot (`circle_*` y `brevo_*`) |
| **Cuándo** | lunes 11:00 UTC (≈ 07:00 Chile) · `.github/workflows/sync-semanal.yml` |
| **Cuánto tarda** | 20-25 min (~22.500 llamadas a Brevo en paralelo) |
| **Portal HubSpot** | 51404466 (producción) |

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
python3 sync_circle_brevo.py --solo circle          # una sola fuente (rápido)
python3 sync_circle_brevo.py --limite 50            # smoke test acotado
python3 sync_circle_brevo.py                        # completo, escribe
python3 verificar_scoring.py                        # read-back contra HubSpot
```

Es **idempotente**: recalcula desde la fuente y hace upsert por email. Correrlo dos veces
seguidas deja el mismo estado.

## Secretos

Tres, y ninguno vive en el repo:

| Variable | Dónde |
|---|---|
| `HUBSPOT_TOKEN` | GitHub → Settings → Secrets and variables → Actions |
| `BREVO_API_KEY` | ídem |
| `CIRCLE_API_TOKEN` | ídem |

En local salen de un `.env` junto al script (está en `.gitignore`). `load_env()` usa
`setdefault`, así que **una variable ya presente en el entorno gana sobre el archivo**: en
CI manda el secret, en local manda tu `.env`. Si falta alguno, el script falla en la primera
línea con un mensaje claro en vez de morir con un `KeyError` a los 20 minutos.

## Dos decisiones de diseño que conviene no revertir sin pensarlo

### Un `None` se omite, no se manda como `""`

En HubSpot un string vacío **borra** la propiedad. La versión anterior mandaba
`"" if v is None else str(v)`, así que cualquier métrica que la fuente no devolviera se
limpiaba. En un job semanal desatendido eso convierte una falla transitoria de la API en
pérdida permanente de datos: las 4 métricas `circle_activity_score`, `_presence`,
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

## Historia: qué reemplaza esto

Dos jobs de `launchd` en el Mac de Emilio, que fallaban en silencio:

| Job | Programado | Qué pasaba |
|---|---|---|
| `com.procure.circle-hubspot-sync` | lunes 07:00 | Falló desde el 2026-07-27: `Operation not permitted`. macOS (TCC) no deja que `launchd` lea `~/Downloads` sin *Full Disk Access*. Última corrida exitosa: 2026-07-21, **a mano**. |
| `com.procure.brevo-hubspot-sync` | viernes 07:00 | Nunca alcanzó a correr programado. |

Además ambos ejecutaban los scripts **viejos** (13 propiedades entre los dos); el sync
unificado que alimenta el scoring —este— no estaba programado en ninguna parte.

⚠️ **Al activar este repo hay que desinstalar los dos jobs de `launchd`**, o van a competir
escribiendo las mismas propiedades:

```bash
launchctl unload ~/Library/LaunchAgents/com.procure.circle-hubspot-sync.plist
launchctl unload ~/Library/LaunchAgents/com.procure.brevo-hubspot-sync.plist
rm ~/Library/LaunchAgents/com.procure.{circle,brevo}-hubspot-sync.plist
```

### Una propiedad que se pierde en el cambio

`circle_likes_dados` (75 contactos con dato) la escribía el sync **viejo** de Circle y este
**no**. Si marketing la usa en el scoring, hay que portarla antes de desinstalar el job
viejo. Si no la usa, se archiva la propiedad y listo.

## Gotcha de GitHub Actions

GitHub **desactiva los workflows programados** de un repo sin actividad por 60 días. Si el
repo queda quieto, revisar en la pestaña *Actions* que el schedule siga habilitado. Un
commit cualquiera reinicia el contador.
