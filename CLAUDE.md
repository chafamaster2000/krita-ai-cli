# kri — CLI para controlar Krita

## Qué es esto

CLI `kri` (single-file, stdlib, `cli/kri`) que controla Krita vía el plugin
`krita-plugin/kritamcp/` (HTTP 127.0.0.1:5678). Skill de uso en
`claude/skills/kri/SKILL.md` — **usala siempre que haya que tocar Krita**.

La migración desde el MCP server está COMPLETA (2026-07-02); resultados en
`BASELINE.md` (paridad de turnos, 29x menos latencia por llamada, previews 4x
más livianos). El server MCP volvió a versionarse en `mcp/server.py` (tag
`v-mcp-final` + wrappers de capas/máscaras/SAM sobre `kri` + las tools que
declaran las extensiones vía `kri ext mcp-tools`).

kri tiene **extensiones** (paquetes pip en `<data>/kri/ext-venv`, entry point
`kri.extensions`, manifiesto `kri-extension.toml`). La primera es autopainter.
Contrato completo en README, sección "Extensiones".

## Reglas de trabajo en este repo

- El plugin corre DENTRO de Krita: cambios en `krita-plugin/` requieren
  redeploy a `pykrita` (`%APPDATA%\krita\pykrita\kritamcp\` en Windows)
  + reiniciar Krita. Verificar con `python3 -m py_compile`.
- Contrato CLI↔plugin: cada subcomando de `kri` mapea 1:1 a un método `cmd_*`
  del plugin; si agregás una action nueva, va en ambos lados + test en
  `tests/test_kri.py` (fake server, TDD).
- Los tests corren sin Krita: `python3 -m unittest discover -s tests -v`.
- Rendimiento: usar `127.0.0.1`, nunca `localhost` (fallback IPv6 de ~2s en
  Windows). El transporte es socket crudo — no reintroducir urllib/requests
  (+55ms de imports por invocación).
- Instalación a mano en una máquina nueva: shim `kri` en el PATH, skills de
  `claude/skills/` a `~/.claude/skills/`, hook de `claude/hooks/` a
  `~/.claude/hooks/` + permisos (`allow Bash(kri:*)`, `ask Bash(kri exec:*)`)
  y hook PreToolUse con `"if": "Bash(kri:*)"` en settings.json.
- **kri no cablea ninguna extensión.** Nada de `autopainter` (ni de otra) en
  `cli/`, `mcp/` ni el plugin: todo lo específico va en la extensión y en su
  `kri-extension.toml`. Los tests usan la extensión de juguete de
  `tests/test_ext.py`.
- La maquinaria de extensiones va en `cli/kri_extensions.py`, no en `cli/kri`:
  el script se recompila en cada invocación y cada línea suma al arranque. Los
  nativos no pueden importar ese módulo ni `importlib.metadata` (lo cuida
  `test_native_commands_never_load_extensions`). Medí antes de agrandar
  `cli/kri`: `kri health` no puede empeorar más de 5 ms.
- La regla de argv de las tools MCP está dos veces: `tool_argv` en
  `cli/kri_extensions.py` y `ext_tool_argv` en `mcp/server.py`. Si cambiás
  una, cambiá la otra.
- Los tests del server MCP necesitan un python con fastmcp (`KRI_MCP_PYTHON`,
  o el venv de `~/.local/share/krita-mcp` u `~/.local/opt/krita-mcp`); si no
  hay, se saltean.
- Instalar o desinstalar: `python3 cli/install.py [--plan-json] [--uninstall]`
  (contrato de componente del instalador del ecosistema). Para probar, usá un
  `HOME` temporal, nunca el real.
- Push directo a master está bloqueado por permisos; el usuario lo hace con
  `! git push` si hace falta.
