# kri — CLI para controlar Krita

## Qué es esto

CLI `kri` (single-file, stdlib, `cli/kri`) que controla Krita vía el plugin
`krita-plugin/kritamcp/` (HTTP 127.0.0.1:5678). Skill de uso en
`claude/skills/kri/SKILL.md` — **usala siempre que haya que tocar Krita**.

La migración desde el MCP server está COMPLETA (2026-07-02). `server.py` fue
retirado; queda recuperable en el tag `v-mcp-final`. Resultados de la
migración en `BASELINE.md` (paridad de turnos, 29x menos latencia por
llamada, previews 4x más livianos).

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
- Instalación en una máquina nueva: shim `kri` en el PATH, skills de
  `claude/skills/` a `~/.claude/skills/`, hook de `claude/hooks/` a
  `~/.claude/hooks/` + permisos (`allow Bash(kri:*)`, `ask Bash(kri exec:*)`)
  y hook PreToolUse con `"if": "Bash(kri:*)"` en settings.json.
- Push directo a master está bloqueado por permisos; el usuario lo hace con
  `! git push` si hace falta.
