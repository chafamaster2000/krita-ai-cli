---
name: kri
description: Usar SIEMPRE que haya que controlar Krita desde Claude Code — pintar, crear canvas, mirar el resultado, o manejar AI Diffusion (prompt, estilo, regiones, controles, generación). El comando `kri` habla con el plugin kritamcp por HTTP local. Trigger en cualquier tarea de Krita, dibujo o generación de imágenes vía Krita.
---

# kri — Krita desde la terminal

`kri` habla con el plugin kritamcp dentro de Krita (`localhost:5678`).
Todo comando imprime JSON. Error → exit 1, así que las cadenas `&&` cortan solas.
`kri --help` y `kri <cmd> --help` listan todo.

## Reglas de oro (minimizar turnos)

1. **Primer comando SIEMPRE `kri status`** — documento + estado AI completo
   (workspace, estilo, `model.architecture`, prompt actual, colas, regiones,
   controles, estilos disponibles) en UNA invocación. No encadenes
   `health` + `ai status` + `ai region list` por separado.

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

3. **Para mirar el canvas**: `kri look` escribe la imagen (default
   `/tmp/kri-canvas.jpg`) e imprime el path → leelo con Read. `--full` (PNG a
   resolución completa) SOLO para la revisión final, nunca mientras iterás.

4. **Prompts**: `kri status` te da `ai.model.architecture` (familia del modelo).
   Antes de `kri ai set-prompt` aplicá la skill **krita-ai-prompt-format**
   (natural language vs tags danbooru según familia); nombres propios no
   mega-famosos → **image-prompt-unknown-entities**; antes de
   `kri ai generate` → **image-prompt-sanity-check**.

5. **Generar**: `kri ai generate --wait` bloquea hasta que la cola se vacía.
   NO hagas polling manual con `kri ai jobs`.

6. **`kri exec`** (Python arbitrario dentro de Krita) es la válvula de escape
   para lo que no tiene subcomando. Requiere que Krita haya arrancado con
   `KRITAMCP_ALLOW_EXEC=1`. Namespace disponible: `app`, `doc`, `view`,
   `layer`, `Krita`, `QColor`, `QImage`, `QPainter`, `QPen`, `QBrush`,
   `QPainterPath`, `QPointF`, `QRectF`, `Qt`. `print()` vuelve como `stdout`;
   asigná `result = <algo JSON-serializable>` para devolver datos.
   Scripts CORTOS: corre en el main thread de Krita — bloquea la UI y un
   script colgado NO se puede abortar.

## Flujos canónicos

Orientarse y dibujar:
```bash
kri status
kri batch --look fast <<'EOF'
[{"action": "new_canvas", "params": {"width": 1024, "height": 768, "background": "#ffffff"}},
 {"action": "set_color", "params": {"color": "#cc3333"}},
 {"action": "draw_shape", "params": {"shape": "rectangle", "x": 300, "y": 400, "width": 400, "height": 250}}]
EOF
```
→ Read del path que imprime, corregir con otro batch si hace falta.

Configurar AI y generar:
```bash
kri ai set-params --style "flux-dev" && kri ai status   # re-leer architecture
kri ai set-prompt -p "..." -n "..."                      # formateado a la familia
kri ai generate --wait && kri ai apply && kri look
```
