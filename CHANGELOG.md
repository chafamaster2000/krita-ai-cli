# Changelog

Las versiones siguen [semver](https://semver.org/lang/es/). Cada una tiene su
tag anotado (`vX.Y.Z`) y coincide con `__version__` en `cli/kri`
(`kri --version`). Las extensiones piden una versión mínima con `min_kri` en
su `kri-extension.toml`.

## [0.5.1] - 2026-11-11

### Arreglado

- **`kri open` ya no se cuelga con un diálogo modal de Krita.** El plugin abre
  los documentos en batchmode: los avisos de carga (por ejemplo, un perfil de
  color que falta en una capa) quedan en el log de Krita y no se convierten en
  un `QMessageBox` que bloquea todo.
- **Vigía de diálogos modales.** Un timer propio del plugin detecta cuándo un
  comando dejó a Krita bloqueado por un diálogo. Le contesta al cliente en
  menos de un segundo con el título, el texto y el detalle del diálogo, y lo
  que llegue mientras tanto falla rápido con el mismo motivo (antes quedaba
  encolado hasta el timeout).
- Un timeout de lectura ya no se informa como "Cannot connect".

### Agregado

- `kri dialog [--dismiss]`: muestra el diálogo modal que bloquea a Krita y,
  con `--dismiss`, lo cierra. Anda aun con Krita bloqueado.
- Skill `kri`: qué hacer cuando Krita abre un diálogo modal.

## [0.5.0] - 2026-11-11

Primera versión numerada. Junta las ramas `layers` y `ext-system`.

### Agregado

- **Sistema de extensiones.** Paquetes pip en un ext-venv propio
  (`<data>/kri/ext-venv`), con entry point `kri.extensions` y manifiesto
  `kri-extension.toml`. Comandos `kri ext list|install|enable|disable|remove|mcp-tools|doctor`.
  Los nativos nunca importan nada de las extensiones; una extensión rota no
  los afecta.
- **Server MCP versionado** (`mcp/server.py`): el del tag `v-mcp-final`, más
  wrappers de capas, máscaras y SAM sobre `kri`, más las tools que declaran
  las extensiones (`kri ext mcp-tools`).
- **Instalador de componente** (`cli/install.py`): shim, plugin, `kritarc`,
  skills, hook, `settings.json`, ext-venv y server MCP. Idempotente, con
  respaldos y `--uninstall` a partir del manifiesto.
- **Capas y máscaras desde kri:** `kri layer list|new|import|export|set|move|group|fill…`,
  `kri select` (from-mask, from-layer, save, load, grow, shrink, feather…), `kri sam status` (estado del daemon
  de krita-autoselect) y `kri colorspace`.
- El plugin `kritamcp` corre en Krita 5 (PyQt5) y Krita 6 (PyQt6) con el mismo
  código.
- `ctx.autoselect_require("segment_many")` para extensiones: exige un daemon
  krita-autoselect con `api_version` 2 o más.

## Antes de 0.5.0 (sin número de versión)

- 2026-07: `kri select` (rect, elipse, polígono, modos) y `kri select sam`
  (selección por segmentación SAM 3, por texto o por click, con `--within`).
  Selecciones con el Select Opaque nativo de Krita (las hormigas se dibujan
  bien) y guarda de reentrada en `process_commands`.
- 2026-07: migración del server MCP al CLI `kri` (stdlib, un archivo, 29 veces
  menos latencia por llamada), skills `kri` y `kri-prompt-*`, y hook de
  recordatorio de formato de prompt. Resultados en `BASELINE.md`.
- 2026-06: tag `v-mcp-final`, último estado del bridge como server MCP (fork de
  nanayax3/krita-mcp).
