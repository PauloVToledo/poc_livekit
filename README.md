# Clara · LiveKit POC

Un agente Python local con `AgentServer` y una `AgentSession` por conversación:
Deepgram STT → OpenAI Responses → ElevenLabs TTS. LiveKit transporta el audio
WebRTC; Agent Console es el cliente web. No se despliega el agente ni se incluye
frontend propio. El dispatch se llama **`clara-livekit-poc`**.
En WebRTC puedes activar Runway para el video del avatar y su sincronización labial;
LiveKit conserva la sesión, el contexto y el control de turnos.

## Requisitos e instalación

Windows, PowerShell 7, Git, Python **3.13.2**, uv y LiveKit CLI **2.18.8 o posterior**.
Usa auriculares y un ambiente tranquilo para el primer ensayo.

```powershell
winget install --id astral-sh.uv -e
winget install --id LiveKit.LiveKitCLI -e
```

Abre una terminal nueva para actualizar el PATH. Si Python 3.13.2 no está instalado:

```powershell
uv python install 3.13.2
```

Comprueba `uv --version`, `lk --version` y `git --version`.
El SDK y los cuatro plugins están fijados en **1.8.5**; `uv.lock` fija las transitivas.
Ejecuta los comandos siguientes desde la raíz de este proyecto.

## Configuración

```powershell
if (!(Test-Path .env)) { Copy-Item .env.example .env }
notepad .env
```

Completa `DEEPGRAM_API_KEY`, `OPENAI_API_KEY`, `ELEVEN_API_KEY` y
`ELEVEN_VOICE_ID`. Elige una voz accesible en tu cuenta ElevenLabs y escucha una
muestra antes de probar. El ID de voz es obligatorio.

Para WebRTC crea un proyecto de prueba en [LiveKit Cloud](https://cloud.livekit.io/)
y copia sus `LIVEKIT_URL`, `LIVEKIT_API_KEY` y `LIVEKIT_API_SECRET` a `.env`.
`lk cloud auth` permite autenticar el CLI con tu cuenta; no despliegues el agente.
La consola de terminal no requiere esas tres variables de LiveKit. Con ellas
disponibles, el SDK puede intentar interrupciones adaptativas también en terminal;
no se presupone que estén disponibles ni que equivalgan al recorrido web.

Se carga exclusivamente el `.env` de la raíz, con precedencia del entorno ya
existente. Evita conservar un `.env.local` activo. Nunca subas las claves a Git.
Los defaults editables son `DEEPGRAM_MODEL=nova-3`, `DEEPGRAM_LANGUAGE=multi`,
`OPENAI_MODEL=gpt-4.1-mini` y `ELEVEN_MODEL=eleven_flash_v2_5`.
Para comparar español exclusivo cambia únicamente `DEEPGRAM_LANGUAGE=es`.

## Quick Start

```powershell
uv sync --locked --python 3.13.2
lk agent console
```

## Terminal y dispositivos

```powershell
# Listar dispositivos (no requiere claves)
lk agent console --list-devices

# Seleccionar dispositivos por nombre
lk agent console --input-device "Nombre del micrófono" --output-device "Nombre de los auriculares"

# Conservar audio y reporte del CLI
lk agent console --record

# Revisar prompt y contexto; no evalúa audio
lk agent console --text
```

Clara saluda y pregunta por un proyecto reciente. `m` silencia el micrófono;
`Ctrl+T` cambia entre voz y texto; `?` muestra los atajos. Detén con `Ctrl+C`
(también `q` en modo voz). Por defecto la entrevista comienza automáticamente y
solicita el cierre a los 300 segundos desde el primer playback de Clara.

## Entrevista controlada para testing

```dotenv
INTERVIEW_AUTO_START=true
INTERVIEW_DURATION_SECONDS=300
INTERVIEW_AUTO_CLOSE=true
```

Los valores anteriores son también los defaults; no necesitas copiarlos al `.env`.
La introducción y despedida son textos fijos enviados con `session.say()`, sin
mensajes artificiales de usuario. La conversación conserva OpenAI Responses y
los proveedores actuales. La introducción se solicita una sola vez, después de
`session.start()` y, en WebRTC, `room_io.wait_for_ready()`; con Runway se espera
además el primer frame del avatar antes de arrancar la sesión.

El evento `playback_started` de la salida de audio inicia un reloj monotónico y
una tarea async independiente. Con Runway 1.8.5 este evento aproxima el inicio
con el primer frame de audio enviado al avatar; no demuestra el instante exacto
en que el navegador lo reproduce. En modo texto se usa el primer mensaje de
Clara confirmado en el historial y se registra esa diferencia.

Al vencer el plazo se bloquean respuestas normales nuevas mediante
`on_user_turn_completed`/`StopResponse` y `llm_node`, incluida generación
anticipada nueva. El transcript STT sigue en el log aunque `StopResponse` omite
ese último turno del historial conversacional nativo. El controlador espera
`wait_for_idle()` hasta 30 segundos para permitir terminar el turno del candidato.
Si supera esa espera, conserva el playback de Clara en curso antes de despedirse.
La despedida usa `allow_interruptions=False` únicamente para ese mensaje;
introducción y conversación conservan las interrupciones habituales.
Se espera `SpeechHandle.wait_for_playout()`, se comprueba su error/interrupción y
se llama `shutdown(drain=True)`. El evento `close` termina el job para generar
el reporte. No se elimina la sala. Las tareas se cancelan al desconectarse el
candidato, cerrarse la sesión o finalizar el job.

El tiempo total puede superar 300 segundos por el turno pendiente, audio en curso
y despedida. `INTERVIEW_AUTO_START=false` omite la introducción automática y
comienza el reloj con la primera respuesta de Clara; `INTERVIEW_AUTO_CLOSE=false`
registra el timeout y permite continuar la conversación. Para ensayos de 5–10
minutos desactiva el cierre o aumenta la duración.

Con avatar usa `RUNWAY_MAX_DURATION_SECONDS=900`: el límite del proveedor empieza
antes que la entrevista y debe dejar margen para preparar la sesión y despedirse.
Un `.env` existente con `300` puede desconectar el avatar prematuramente.
`RUNWAY_ENABLED=false` conserva el mismo controlador y la salida de audio LiveKit.

Los eventos `interview_started`, `interview_elapsed_seconds` (cada 30 segundos),
`interview_timeout_reached`, `interview_closing` e `interview_finished` quedan en
`outputs/agent.log` y `poc.interview_events` del reporte. `interview_finished.reason`
distingue playback final completado de desconexión/cierre anticipado.

Para verificar manualmente, guarda una sesión terminal con `--record` y otra
WebRTC con/sin avatar. Permanece en silencio al conectar, comprueba una sola
introducción, habla cerca del segundo 300, confirma ausencia de nuevas preguntas,
despedida completa y `session_end` con reporte. Repite abandonando antes del
timeout y comprueba que no se reproduce una despedida tardía. Para un ensayo
corto puedes establecer temporalmente `INTERVIEW_DURATION_SECONDS=15` en el
entorno; después elimina esa variable para recuperar el valor del `.env`/default.

## WebRTC con Agent Console

```powershell
lk agent dev

# Para una conversación larga sin recarga automática
lk agent dev --no-reload
```

Abre **Agent Console** en el dashboard del mismo proyecto LiveKit. Selecciona
`clara-livekit-poc`, pulsa **Start a session** y permite el micrófono.
Revisa Audio, Events y Metrics. Para detener, finaliza la sesión del navegador y
después pulsa `Ctrl+C` en PowerShell. El proceso Python sigue ejecutándose localmente.

## Turnos y resultados

`TurnHandlingOptions` fija únicamente `inference.TurnDetector(version="v1-mini")`.
El SDK suministra el VAD Silero y conserva los defaults: endpointing fijo
0,3–2,5 s; interrupciones automáticas habilitadas (0,5 s / 0 palabras), recuperación
de falsa interrupción a los 2 s y generación anticipada de LLM sin TTS especulativo.
No se imponen límites de palabras o duración al candidato. La regla de 10 s para
generación anticipada limita intentos especulativos, no las respuestas del candidato.

- `outputs/agent.log`: transcripts finales y parciales, historial con métricas de
  turno, métricas STT/LLM/TTS, interrupciones, avisos del SDK, errores e inicio/cierre.
  Cada evento de la POC incluye el identificador local de sesión.
- `outputs/<session-id>/report.json`: reporte nativo generado con
  `ctx.make_session_report().to_dict()` en `on_session_end`, más `poc` con modo,
  modelos, voz, opciones efectivas, tiempos, métricas de componentes y diagnósticos.
- `console-recordings/`: audio y reporte del CLI cuando usas `--record`.

El modo de interrupción solicitado permanece `auto`. `poc.interruption_mode`
registra evidencia de modo adaptativo o degradación a VAD a partir de eventos y
mensajes del SDK; queda **no observado** si no hubo evidencia. No equivale a una
garantía de disponibilidad. No se accede al estado privado del SDK.

Al cerrar normalmente, busca `event=session_end` en el log y comprueba el JSON
mencionado. Un cierre forzado puede impedir el reporte. Los transcripts y las
grabaciones contienen las intervenciones del candidato; los resultados quedan
ignorados por Git. En STT streaming `duration=0` no significa latencia cero: consulta
`transcription_delay` en las métricas del mensaje. Usa también `end_of_turn_delay`,
`llm_node_ttft` y `tts_node_ttfb`. No hay thresholds de aceptación definidos.

## Pruebas manuales (pendientes de credenciales)

Primero ejecuta terminal con `--record`; luego repite en `dev` + Agent Console.
Identifica cada resultado por modo. En cada prueba anota escenario, momento del
problema, ID de sesión, configuración y observación junto al reporte correspondiente.

| Escenario | Ejecución y observación |
| --- | --- |
| Conversación normal | Respuestas cortas; fluidez, seguimientos, duplicaciones y latencia. |
| Pausas de pensamiento | Pausas de 1, 2 y 3 s; cierre prematuro y recuperación. |
| Respuesta larga | Hablar 30–60 s; transcript, continuidad y seguimiento. |
| Barge-in | Corregir o preguntar mientras Clara habla; detención, captura y respuesta. |
| Ruido/falso inicio | Tos, ruido breve y “ajá”; corte, clasificación y recuperación. |
| Español completo | Acento habitual y vocabulario técnico; STT, pronunciación y coherencia. |
| Sesión continua | Hablar 5–10 min; contexto, repetición, errores y desconexiones. |

Incluye una intervención en inglés o un cambio de idioma. Revisa logs y reportes,
y cambia un solo componente o parámetro por comparación. Una pausa de 3 s puede
superar el máximo default de 2,5 s: es un caso prioritario. Terminal no incluye el
transporte LiveKit; sus resultados deben conservarse separados de WebRTC.

## Problemas frecuentes

- **Comando no encontrado:** abre otra terminal; comprueba las instalaciones de uv y lk.
- **Variables faltantes:** completa `.env`; el error indica nombres, sin claves.
  El CLI puede exigir credenciales LiveKit antes de importar el agente en `dev`.
- **401/403, voz o modelo inválido:** revisa la key del proveedor, permisos y
  disponibilidad de la voz; no se sustituyen componentes automáticamente.
- **No hay audio:** lista y selecciona dispositivos, permite el micrófono del
  navegador y confirma que no esté silenciado.
- **Interrupciones adaptativas no disponibles:** revisa los warnings y eventos;
  el SDK puede degradar a VAD por credenciales, conectividad o cuotas.
- **Primera ejecución lenta:** la inferencia nativa local inicializa sus modelos;
  espera a que termine antes de evaluar latencia.
- **No aparece el reporte:** verifica el log, el cierre normal y permisos de
  escritura en la raíz. No confíes en un proceso terminado por la fuerza.

## Estado de validación

Implementación preparada sin claves. La instalación bloqueada, imports, defaults,
dispositivos y verificaciones locales se realizan sin consumir APIs. La conversación
con proveedores, el cierre real mediante `Ctrl+C`/desconexión y los siete escenarios
en ambos modos requieren credenciales y quedan pendientes. Para reproducir una
instalación limpia en otro checkout: configura `.env` y ejecuta el Quick Start.

Verificado el 6 de octubre de 2026 en este equipo:

| Etapa | Evidencia local |
| --- | --- |
| Herramientas | PowerShell 7.6.6, Python 3.13.2, uv 0.12.23 y lk 2.18.8. |
| Dependencias | Instalación inicial y otra instalación en un entorno vacío desde `uv.lock`; SDK y plugins 1.8.5. |
| Configuración/pipeline | Validación de variables, precedencia del entorno, instancias reales de plugins y defaults; inicio/conexiones/bienvenida simulados para evitar llamadas. |
| Observabilidad | Eventos y métricas inyectados, reporte nativo del SDK escrito y leído, secretos redactados. No valida el cierre de una conversación real. |
| Inferencia/dispositivos | `v1-mini` ejecutado sobre silencio sintético; dispositivos enumerados por el CLI. No evalúa micrófono ni calidad de voz. |

## Runway + LiveKit: entrevista con avatar

La integración conserva **Deepgram → OpenAI Responses → ElevenLabs**.
Runway recibe el audio sintetizado por ElevenLabs mediante el transporte del plugin
y publica el avatar en la misma sala. La voz y el comportamiento configurados en
el personaje de Runway quedan sustituidos por ElevenLabs y `src/clara_prompt.txt`.
No necesitas Gemini ni `GOOGLE_API_KEY`.
[Arquitectura oficial de Runway](https://docs.dev.runwayml.com/characters/livekit/).

### Claves y personaje

Usa un proyecto LiveKit Cloud de prueba, accesible desde Runway, y un proyecto con
acceso y créditos en [Runway Developer Portal](https://dev.runway.com/).
Completa estas variables en el `.env` existente; no lo reemplaces si ya tienes claves:

| Variable | Qué colocar |
| --- | --- |
| `LIVEKIT_URL` | URL `wss://…` del proyecto que abres en Agent Console. |
| `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` | Credenciales de ese mismo proyecto. |
| `DEEPGRAM_API_KEY` | Clave de Deepgram para STT. |
| `OPENAI_API_KEY` | Clave de OpenAI para Responses. |
| `ELEVEN_API_KEY`, `ELEVEN_VOICE_ID` | Clave y voz accesible en ElevenLabs. |
| `RUNWAYML_API_SECRET` | Clave API de Runway Developer Portal. |
| `RUNWAY_ENABLED` | `true` activa el avatar en WebRTC; default `false`. |
| `RUNWAY_AVATAR_ID` | ID del personaje personalizado de Clara; preset vacío. |
| `RUNWAY_PRESET_ID` | ID de preset, por ejemplo `cat-character`; avatar vacío. |
| `RUNWAY_MAX_DURATION_SECONDS` | Entero positivo; default `900` (15 minutos). |

Para el primer ensayo con preset, añade este bloque y completa el secreto:

```dotenv
RUNWAY_ENABLED=true
RUNWAYML_API_SECRET=
RUNWAY_AVATAR_ID=
RUNWAY_PRESET_ID=cat-character
RUNWAY_MAX_DURATION_SECONDS=900
```

Para Clara personalizada, coloca su ID en `RUNWAY_AVATAR_ID` y deja
`RUNWAY_PRESET_ID=`. Debe haber **exactamente un selector**. El agente valida las
variables antes de iniciar el worker; los errores indican nombres sin secretos.
Las variables heredadas de PowerShell tienen precedencia sobre `.env`.
Los nombres `RUNWAY_ENABLED`, selectores y duración pertenecen a esta POC y se
convierten en argumentos del plugin; `RUNWAYML_API_SECRET` es el nombre oficial.
[Configuración del plugin](https://docs.livekit.io/agents/models/avatar/plugins/runway/).

### Llegar y probar

```powershell
uv sync --locked --python 3.13.2
notepad .env

# Regresión de voz en terminal; Runway permanece inactivo aunque ENABLED=true.
lk agent console --record

# Cierra la terminal de conversación y arranca el worker WebRTC.
lk agent dev --no-reload
```

Abre Agent Console en el dashboard del **mismo proyecto LiveKit**, selecciona
`clara-livekit-poc`, inicia una sesión y permite el micrófono. Usa auriculares.
El agente conecta la sala, solicita el avatar y espera su primer frame de video
antes de iniciar la conversación y saludar. El plazo de arranque del avatar es
**60 segundos**, incluyendo solicitud y espera de video. La entrada acepta al
candidato estándar o SIP y excluye participantes de tipo agente, incluido Runway.

Comprueba que ves el avatar, escuchas **una sola voz** y Clara responde al candidato.
`lk agent console`, incluso con `--text`, comprueba únicamente el pipeline de voz;
el avatar solo está activo en `dev`/`start` con WebRTC. Para comparar WebRTC sin
avatar, cambia `RUNWAY_ENABLED=false` y reinicia el worker.

### Fallos y cierre

Una clave/ID inválido, falta de créditos, timeout de arranque, desconexión del avatar
o pérdida de su pista de video termina explícitamente la conversación. No se cambia
automáticamente a voz sola. Al salir el candidato también se solicita terminar el job.
Finaliza la sesión del navegador y después detén el worker con `Ctrl+C`.
El cierre usa `AvatarSession.aclose()` con un plazo local de 5 segundos y conserva
el callback de limpieza del plugin. Una terminación forzada puede impedir limpieza
y reporte; `max_duration` limita la duración del worker Runway y su facturación.
Al alcanzar ese límite, la salida del avatar también termina la entrevista.

**Comprueba el estado final en Runway Developer Portal después de cada ensayo.**
El retorno de `aclose()` no demuestra que Runway haya confirmado el cierre remoto:
el plugin puede enviar `END_CALL` o intentar cancelar por API y registrar advertencias.
[Cierre y facturación de Runway](https://docs.dev.runwayml.com/characters/livekit/#end-sessions-promptly).

### Evidencia y aceptación

`outputs/agent.log` y `poc.avatar_events` en el reporte incluyen:

| Evento | Qué acredita |
| --- | --- |
| `avatar_start_requested` | Solicitud de inicio local. |
| `avatar_start_accepted` | Retorno de `avatar.start()`; todavía no acredita video. |
| `avatar_video_available` | Primer frame recibido en el worker y `startup_seconds`; comprobar también su visualización en el navegador. |
| `avatar_failure` / `session_start_failed` | Pérdida del avatar o fallo de arranque; incluye tipo de error cuando aplica. |
| `avatar_close_requested` | Solicitud de cierre local. |
| `avatar_close_method_returned` / `avatar_close_failed` | Retorno o fallo/timeout del método; estado final del proveedor `UNKNOWN`. |

`poc.config` registra activación solicitada/efectiva, selector, duración y timeout,
sin la key de Runway. `RUNWAYML_API_SECRET` se redacta en logs y JSON. Si falla antes
de `session.start()`, se escribe un reporte de arranque con `startup_failed=true`
y los eventos locales; no se inventa un reporte nativo de conversación.

Repite los siete escenarios de pruebas manuales anteriores en WebRTC con avatar.
Añade estas comprobaciones, conservando modo, ID, configuración, observaciones y
`outputs/<session-id>/report.json` para cada resultado:

| Prueba | Criterio de aceptación |
| --- | --- |
| Inicio | Avatar visible antes del saludo; tiempo de arranque registrado. |
| Audio/video | Voz ElevenLabs única, sin eco, labios sincronizados; evaluar en el navegador. |
| Interrupción | Al interrumpir, Clara deja de hablar y el avatar acompaña la detención. |
| Continuidad | 5–10 minutos con contexto, voz y video continuos. |
| Clave/ID inválidos | Error y cierre explícitos; sin saludo ni degradación silenciosa. |
| Avatar ausente | Timeout acotado y reporte de fallo. |
| Cierre | Salida del navegador, `Ctrl+C` y límite de duración; reporte y estado remoto final comprobados. |

Compara `startup_seconds` y las métricas de respuesta con WebRTC sin avatar.
Todavía no hay un umbral de latencia aprobado.

### Validación local de la integración

Verificado el **7 de octubre de 2026**, sin iniciar sesiones de proveedores:
instalación con `uv sync --locked --python 3.13.2`, imports y SDK/cuatro plugins
en 1.8.5; validación de activación, selectores, claves y duración; verificaciones
con 12 pruebas locales para configuración, primer frame, orden de inicio/saludo, timeout,
desconexión, pérdida de video, cierre y reportes con secreto redactado.
Se comprobó también, con la solicitud HTTP simulada, que el plugin real sustituye
la salida de audio por `DataStreamAudioOutput`, y que terminal no crea un avatar.
Las pruebas locales no acreditan calidad audiovisual ni cierre remoto.
Quedan pendientes con tus claves la regresión de voz grabada, la entrevista real
con avatar, los siete escenarios y el estado final en Runway.

## Referencias verificadas

- [Starter oficial](https://github.com/livekit-examples/agent-starter-python)
- [Comandos del CLI](https://docs.livekit.io/reference/developer-tools/livekit-cli/agent/)
- [Turn detector](https://docs.livekit.io/agents/logic/turns/turn-detector/)
- [Eventos, métricas y reportes](https://docs.livekit.io/testing/observability/data/)
- [OpenAI directo en LiveKit](https://docs.livekit.io/agents/models/llm/openai/)
- [GPT-4.1 mini](https://developers.openai.com/api/docs/models/gpt-4.1-mini)
