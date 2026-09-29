# ADAM

Asistente de voz local. **No es un chat**: todo entra y sale por voz.

**ADAM** = Automated Developer and Asset Manager  
(solo lo explica si se lo preguntan).

## APIs (`.env`)

| Variable | Para |
|----------|------|
| `MOONSHOT_API_KEY` | Kimi (cerebro) |
| `ELEVENLABS_API_KEY` / `ELEVENLABS_VOICE_ID` | voz + STT |
| `CURSOR_API_KEY` | enviar prompts al agente de Cursor ([crear clave](https://cursor.com/dashboard/api)) |
| `HOME_ASSISTANT_URL` / `HOME_ASSISTANT_TOKEN` | Home Assistant (opcional) |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | Gmail/Calendar API (opcional; también login manual en el perfil Playwright) |
| `SPOTIFY_CLIENT_ID` | Spotify Web API (opcional; fallback a escritorio) |
| `TELEGRAM_BOT_TOKEN` | envío real de Telegram (confirma antes) |
| `WHATSAPP_CLOUD_TOKEN` / `WHATSAPP_PHONE_NUMBER_ID` | WhatsApp Cloud Business (no WhatsApp personal) |

## Arranque

```powershell
cd aether
.\.venv\Scripts\Activate.ps1
py -3 -m aether_core.main
```

HUD: `cd apps\hud && npm start`

Di por ejemplo: “Adam, ábreme este proyecto en Cursor y dile al agente que revise el README”.

## Acciones en servicios y aplicaciones

- “Pon *[canción]* en YouTube”: busca, abre el resultado y verifica reproducción.
- “Sube *[ruta de carpeta]* a mi Drive”: usa el perfil persistente de ADAM y verifica que Drive reciba la carpeta.
- “Abre WhatsApp Web, la app de escritorio”: abre y verifica la aplicación de Windows, no el navegador.
- “Pon *[canción]* en Spotify de escritorio”: controla Spotify y solo confirma si puede verificar reproducción.

El navegador automatizado conserva su sesión en `data/browser_profile`. La primera
vez, inicia sesión manualmente en Google dentro de esa ventana; después ADAM
reutiliza ese perfil. ADAM no evade contraseñas ni autenticación de dos factores.

## Superagente

ADAM ahora también:

- Delega misiones a Cursor **en segundo plano** y avisa al terminar.
- Controla UI nativa (`vision.act_and_verify`) y verifica el efecto.
- Recuerda hechos/entidades, indexa archivos, hace backups verificados y rutinas.
- Pide confirmación oral para enviar, publicar, compartir, borrar, comprar o restaurar.
- Muestra trabajos activos en el HUD (cancelar con el botón).

Onboarding: di “Adam, estado de integraciones”. Si un servicio está en `stub` o `needs_oauth`, no fingirá que ya actuó.

Las rutinas `health_hourly`, `morning_health`, `files_index_daily` y `memory_daily_consolidate` existen desactivadas. Actívalas con `routine.enable` cuando quieras.
