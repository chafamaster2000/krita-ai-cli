---
name: kri
description: Usar SIEMPRE que haya que controlar Krita desde Claude Code — pintar, crear canvas, capas y máscaras, mirar el resultado, o manejar AI Diffusion (prompt, estilo, regiones, controles, generación). El comando `kri` habla con el plugin kritamcp por HTTP local. Trigger en cualquier tarea de Krita, dibujo o generación de imágenes vía Krita.
---

# kri — Krita desde la terminal

`kri` habla con el plugin kritamcp dentro de Krita (`localhost:5678`).
Todo comando imprime JSON. Error → exit 1, así que las cadenas `&&` cortan solas.
`kri --help` y `kri <cmd> --help` listan todo.

**Un turno = una invocación de Bash, no un comando kri.** Encadená con `&&`
todo lo que no dependa de leer un output intermedio: acción + verificación
viajan juntas (`kri batch <<EOF ... EOF && kri ai status`). Solo cortá el
turno cuando necesitás LEER el resultado para decidir el paso siguiente.

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
   `select_shape`, `select_all`, `select_none`, `select_invert`,
   `select_modify`, `select_info`, `select_from_mask`, `select_from_layer`,
   `select_save`, `select_load`, `layer_list`, `layer_new`, `layer_import`,
   `layer_export`, `layer_set`, `layer_move`, `layer_select`,
   `layer_duplicate`, `layer_delete`, `layer_group`, `layer_fill`,
   `doc_colorspace`,
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
   temporal del sistema e imprime el path → leelo con Read. El modo fast
   devuelve máx. 512px (~260 tokens de visión): alcanza para juzgar
   composición y ubicación mientras iterás. `--full` (PNG a resolución
   completa) SOLO para la revisión final; `--max-dim N` si necesitás un
   punto medio. Si el batch ya llevó `--look fast`, NO hagas un `kri look`
   suelto después: ya tenés el canvas de ese turno.

4. **Prompts**: `kri status` te da `ai.model.architecture` (familia del modelo).
   Antes de `kri ai set-prompt` aplicá la skill **kri-prompt-format**
   (natural language vs tags danbooru según familia); nombres propios no
   mega-famosos → **kri-prompt-unknown-entities**; antes de
   `kri ai generate` → **kri-prompt-sanity-check**.

5. **Generar**: `kri ai generate --wait` bloquea hasta que la cola se vacía.
   NO hagas polling manual con `kri ai jobs`.

6. **`kri exec`** (Python arbitrario dentro de Krita) es el ÚLTIMO recurso,
   solo para lo que de verdad no tiene subcomando (resize, filtros).
   **NUNCA para dibujar**: todo lo dibujable sale de
   `draw_shape`/`stroke`/`fill` en batch (ver regla 2). Suele estar
   deshabilitado — requiere que Krita haya arrancado con
   `KRITAMCP_ALLOW_EXEC=1`, así que intentarlo a ciegas quema un turno.
   Namespace disponible: `app`, `doc`, `view`,
   `layer`, `Krita`, `QColor`, `QImage`, `QPainter`, `QPen`, `QBrush`,
   `QPainterPath`, `QPointF`, `QRectF`, `Qt`. `print()` vuelve como `stdout`;
   asigná `result = <algo JSON-serializable>` para devolver datos.
   Scripts CORTOS: corre en el main thread de Krita — bloquea la UI y un
   script colgado NO se puede abortar.

## Selecciones (inpaint dirigido)

`kri select` maneja la selección del documento. AI Diffusion genera SOLO
dentro de la selección activa → es LA herramienta para regenerar una zona
sin tocar el resto (inpaint). Subcomandos: `rect X Y W H`,
`ellipse X Y W H`, `poly X,Y X,Y X,Y ...` (los tres aceptan
`--mode replace|add|subtract|intersect`), `all`, `none`, `invert`,
`feather R`, `grow R`, `shrink R`, `border R`, `info`.

- **`kri select sam`** = selección por segmentación (SAM 3, daemon
  krita-autoselect en `127.0.0.1:5679`). Es la herramienta de artista para
  "seleccioná ESE objeto" — como el pincel pero con prompt o click. Dos modos:
  - **Con texto** (`kri select sam "the red car"`): TODAS las instancias del
    concepto. `--point`/`--neg-point`/`--box` refinan el concepto;
    `--instance N` elige una (0 = mejor score); `--list` solo inspecciona
    (scores + bboxes) sin tocar la selección.
  - **Sin texto, solo `--point X,Y` y/o `--box X Y W H`**: el objeto EXACTO
    bajo el click / dentro de la caja (estilo SAM2). `--neg-point` = "esta
    parte no". Usalo cuando sabés DÓNDE está lo que querés.
  Preferila sobre rect/ellipse cuando el objetivo es un objeto de la imagen
  y no una zona geométrica. Primera llamada tarda ~10-30s (carga el modelo).
  Si el daemon está caído, kri le pide al plugin AI Select (SAM) de Krita
  que lo arranque y reintenta solo; si igual falla con "Cannot connect" (AI
  Select no instalado), decírselo al usuario, no reintentar.
  `kri sam status` / `kri sam start` / `kri sam stop` manejan ese daemon.
  **`--to-layer NOMBRE --color HEX`** además crea una capa rellena con ese
  color dentro de la selección (`--blend`, `--into GRUPO`/`--above REF`...,
  `--save-mask NOMBRE` guarda la máscara en esa capa): una parte = un comando.
- `kri select from-mask archivo.png` = selección desde cualquier máscara en
  escala de grises (blanco = seleccionado).
- Feather antes de generar (10-20px) para que el parche se funda con el
  entorno; al terminar, `kri select none` para no dejar una selección
  colgada que recorte la próxima generación.
- OJO: la selección NO recorta `draw_shape`/`stroke`/`fill` (escriben pixel
  data directo). Afecta a AI generate y a las operaciones nativas de Krita.

Inpaint canónico (tras configurar el prompt para la zona):
```bash
kri select ellipse 300 200 220 180 && kri select feather 15 \
  && kri ai generate --wait && kri ai apply && kri select none && kri look
```

## Capas y máscaras

`kri layer list` da el árbol (arriba→abajo) con path, id, blend, opacidad,
visible, locks, activa y bounds, más el espacio de color del documento.
`REF` = nombre único, path `Grupo/Sub/Capa` desde arriba (`/Capa` fija una
de primer nivel) o el id. Un nombre repetido falla con la lista de
candidatos: usá el path o el id que trae. Ubicación (una sola): `--above REF`,
`--below REF`, `--into GRUPO`, `--top`, `--bottom`; sin ninguna, va arriba de
la capa activa. Opacidad en %. Blends: ids de Krita o alias (`multiply`,
`screen`, `overlay`, `soft-light`...); `kri layer blends` los lista.

- **Line art en grises**: un JPG gris abre como GRAYA y toda capa de color
  sale gris (la respuesta trae `warning`). `kri colorspace RGBA U8` primero.
- `kri layer import ARCHIVO` lo lee Krita del disco (path absoluto en batch);
  ~0.5 s por PNG de 3000 px. Armá la estructura ANTES (grupo vacío) e
  importá `--into GRUPO`: mover o agrupar capas grandes cuesta ~0.5 s c/u.
- Máscaras de partes: `kri select save NOMBRE --on CAPA` guarda la selección
  como selection mask; `kri select load NOMBRE` la recupera;
  `kri select from-layer CAPA` selecciona por el alfa de una capa.
- `kri layer fill REF HEX [--selection]` respeta alpha lock (recolorea un
  flat sin cambiar su forma); falla sobre capas bloqueadas.
- Sombras recortadas a un flat: capa nueva `--blend multiply` +
  `kri layer set X --inherit-alpha`.

Flats de un line art (estructura en un turno, después una parte por SAM):
```bash
kri colorspace RGBA U8 && kri batch <<'EOF'
[{"action": "layer_set", "params": {"ref": "Fondo", "name": "Line art", "blend": "multiply", "locked": true}},
 {"action": "layer_new", "params": {"name": "Flats", "type": "group", "below": "Line art"}},
 {"action": "layer_import", "params": {"path": "/abs/abrigo.png", "name": "Abrigo", "into": "Flats"}}]
EOF
kri select sam "the beard" --to-layer Barba --color "#eeeeee" --into Flats \
  --save-mask "Barba mask" && kri select none && kri look
```

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

Configurar AI (estilo + prompt + verificación = UN turno):
```bash
kri batch <<'EOF' && kri ai status
[{"action": "ai_set_params", "params": {"style": "flux-dev"}},
 {"action": "ai_set_prompt", "params": {"positive": "...", "negative": "..."}}]
EOF
```
La convención de prompt de la familia NUEVA sale del nombre/filename del
estilo (Tabla B de kri-prompt-format) — no hace falta un turno aparte
para re-leer `architecture` antes de escribir el prompt.

Generar (después de configurar):
```bash
kri ai generate --wait && kri ai apply && kri look
```

## Extensiones (`kri <ext> …`)

Además de los nativos, kri puede tener extensiones instaladas: subcomandos que
agregan otros paquetes (la primera es `kri autopainter`, flats automáticos de
un lineart). `kri --help` las lista al final, y `kri <ext> --help` muestra
sus comandos. Hablan el mismo idioma: imprimen JSON y un error sale con exit 1.

- `kri ext list`: qué hay instalado y si carga (`ok`, `reason`).
- `kri ext doctor`: qué servicios necesita cada una (daemon SAM con
  `segment_many`, ComfyUI, CLI `claude`) y si responden.
- Un error `extensión X rota: …` no afecta a los nativos: seguí con kri y
  avisale al usuario (`kri ext list` dice por qué no carga).
- Un error `no tiene segment_many` quiere decir que el daemon de
  krita-autoselect es viejo. `kri sam status` lo muestra como `warning`.
- `--human` devuelve texto legible en vez de JSON (para mostrarle al usuario).
  Para trabajar, usá el JSON.
- Instalar, deshabilitar o sacar extensiones (`kri ext install|disable|remove`)
  cambia el entorno del usuario: hacelo sólo si te lo pide.

## Delegar a subagentes (tareas de dibujo largas)

Si el dibujo pide varias rondas de iteración, no lo hagas en la sesión
principal: el contexto acumulado (imágenes incluidas) hace cada turno más
lento y más caro. Patrón:

- **La sesión principal** decide QUÉ dibujar: composición, paleta, coordenadas
  aproximadas — y lo escribe como spec concreto en el prompt del subagente.
- **Un subagente de contexto limpio ejecuta** el loop dibujar→mirar→corregir
  con esta skill. Para loops mecánicos con spec claro alcanza un modelo chico
  (`model: haiku`); reservá el modelo grande para decisiones de composición.
- **NUNCA dos subagentes en paralelo contra el mismo Krita**: el plugin opera
  sobre el documento activo y se pisan. Serial siempre.
- El subagente devuelve el path del último look como evidencia; la sesión
  principal lo lee UNA vez en vez de acumular cada iteración intermedia.
