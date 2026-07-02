---
name: kri
description: Usar SIEMPRE que haya que controlar Krita desde Claude Code — pintar, crear canvas, mirar el resultado, o manejar AI Diffusion (prompt, estilo, regiones, controles, generación). El comando `kri` habla con el plugin kritamcp por HTTP local. Trigger en cualquier tarea de Krita, dibujo o generación de imágenes vía Krita.
---

# kri — Krita desde la terminal

`kri` habla con el plugin kritamcp dentro de Krita (`localhost:5678`).
Todo comando imprime JSON. Error → exit 1, así que las cadenas `&&` cortan solas.
`kri --help` y `kri <cmd> --help` listan todo.

## Reglas de oro (minimizar turnos)

1. **`kri status` solo cuando el estado existente importa** — trabajar sobre un
   documento ya abierto, o cualquier tarea de AI (te da workspace, estilo,
   `model.architecture`, prompt actual, colas, regiones, controles y estilos en
   UNA invocación; no encadenes `health` + `ai status` + `ai region list`).
   **Si la tarea arranca con un canvas nuevo y no toca AI, salteá el status**:
   andá directo a un `kri batch` con `new_canvas` como primera action.

2. **Agrupá todo lo agrupable en `kri batch`** — una invocación = un turno:

   ```bash
   kri batch --look fast <<'EOF'
   [{"action": "set_color", "params": {"color": "#ffffff"}},
    {"action": "fill", "params": {"x": 100, "y": 100, "radius": 80}},
    {"action": "ai_set_prompt", "params": {"positive": "..."}}]
   EOF
   ```

   Las actions son los nombres internos del plugin: `new_canvas`, `set_color`,
   `set_brush`, `stroke`, `fill`, `draw_shape`, `clear`, `undo`, `redo`,
   `ai_set_prompt`, `ai_set_params`, `ai_set_workspace`, `ai_generate`,
   `ai_create_region`, `ai_select_region`, `ai_add_control`.
   El batch PARA en el primer error (exit 1 con `stopped_at`).
   `--look fast` te devuelve el canvas final en el mismo turno.

   `draw_shape` solo conoce `rectangle`, `ellipse` y `line` (`fill` pinta un
   círculo relleno). Triángulos y polígonos se COMPONEN dentro del mismo batch:
   p.ej. un techo triangular = tandas de `line` horizontales cada vez más
   cortas, o rectángulos apilados, y las aristas prolijas con dos `line`
   diagonales. No hay shape que falte que justifique otro camino.

3. **Para mirar el canvas**: `kri look` escribe la imagen en el directorio
   temporal del sistema e imprime el path → leelo con Read. `--full` (PNG a
   resolución completa) SOLO para la revisión final, nunca mientras iterás.
   Si el batch ya llevó `--look fast`, NO hagas un `kri look` suelto después:
   ya tenés el canvas de ese turno.

4. **Prompts**: `kri status` te da `ai.model.architecture` (familia del modelo).
   Antes de `kri ai set-prompt` aplicá la skill **krita-ai-prompt-format**
   (natural language vs tags danbooru según familia); nombres propios no
   mega-famosos → **image-prompt-unknown-entities**; antes de
   `kri ai generate` → **image-prompt-sanity-check**.

5. **Generar**: `kri ai generate --wait` bloquea hasta que la cola se vacía.
   NO hagas polling manual con `kri ai jobs`.

6. **`kri exec`** (Python arbitrario dentro de Krita) es el ÚLTIMO recurso,
   solo para lo que de verdad no tiene subcomando (metadata de capas, resize,
   filtros). **NUNCA para dibujar**: todo lo dibujable sale de
   `draw_shape`/`stroke`/`fill` en batch (ver regla 2). Suele estar
   deshabilitado — requiere que Krita haya arrancado con
   `KRITAMCP_ALLOW_EXEC=1`, así que intentarlo a ciegas quema un turno.
   Namespace disponible: `app`, `doc`, `view`,
   `layer`, `Krita`, `QColor`, `QImage`, `QPainter`, `QPen`, `QBrush`,
   `QPainterPath`, `QPointF`, `QRectF`, `Qt`. `print()` vuelve como `stdout`;
   asigná `result = <algo JSON-serializable>` para devolver datos.
   Scripts CORTOS: corre en el main thread de Krita — bloquea la UI y un
   script colgado NO se puede abortar.

## Flujos canónicos

Dibujar desde cero (SIN status previo — el canvas lo define la tarea):
```bash
kri batch --look fast <<'EOF'
[{"action": "new_canvas", "params": {"width": 1024, "height": 768, "background": "#ffffff"}},
 {"action": "set_color", "params": {"color": "#cc3333"}},
 {"action": "draw_shape", "params": {"shape": "rectangle", "x": 300, "y": 400, "width": 400, "height": 250}}]
EOF
```
→ Read del path que imprime, corregir con otro batch `--look fast` si hace
falta. Piso ideal: 1 batch + 1 batch de corrección = 2 turnos.

Trabajar sobre un documento existente o con AI: `kri status` primero, después
el batch.

Configurar AI y generar:
```bash
kri ai set-params --style "flux-dev" && kri ai status   # re-leer architecture
kri ai set-prompt -p "..." -n "..."                      # formateado a la familia
kri ai generate --wait && kri ai apply && kri look
```
