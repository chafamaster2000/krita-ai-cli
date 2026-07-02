---
name: kri-prompt-sanity-check
description: Usar como última compuerta ANTES de lanzar una generación en Krita AI Diffusion (kri ai generate), y después de cualquier cambio de prompt o de estilo/modelo — para revisar que el prompt final siga siendo coherente con el modelo activo y con lo que pidió el usuario antes de gastar la generación. Atrapa restos de ediciones, contradicciones, convención equivocada tras cambiar de estilo, nombres propios sin resolver y negativos que pelean con el positivo.
metadata:
  type: technique
  target: kri CLI + AI Diffusion
---

# Sanity check del prompt antes de lanzar

## Principio

**Cambiar algo y lanzar sin releer es donde se cuela el error.** Cambiaste el estilo (puede dar vuelta la convención), editaste medio prompt (quedaron restos), reusaste un prompt de otro modelo... Antes de `kri ai generate`, revisá que el prompt final sea **coherente con el modelo activo** y **fiel a la intención del usuario**. Es una compuerta de lectura, no de reescritura: o pasa, o devolvés una lista corta de fixes.

## Cuándo correrlo

- **Siempre antes de `kri ai generate`.**
- Después de `kri ai set-params(style=...)` — cambiar de estilo puede cambiar la familia (natural ↔ tags).
- Después de editar parcialmente el positivo/negativo.
- Al reusar un prompt escrito para otro modelo.

## Checklist (releé `kri ai status` primero)

1. **Convención correcta para el modelo activo.** Mirá `model.architecture`. ¿El positivo está en la convención de esa familia (lenguaje natural vs tags danbooru)? Si cambiaste de estilo, ¿el prompt sigue en el formato viejo? → ver [[kri-prompt-format]].
2. **Intención del usuario preservada.** Sujeto, acción, estilo, mood, encuadre que pidió: ¿están todos? ¿Se coló algo que NO pidió? ¿Se perdió algo en la reescritura?
3. **Sin restos de edición.** Tags/frases duplicadas, fragmentos colgados, comas dobles, mezcla de oraciones + tags en el mismo prompt.
4. **Sin contradicciones.** "día" y "noche", "primer plano" y "plano general", colores que pelean, atributos incompatibles.
5. **Count tags coherentes** (booru): `1girl` pero el texto describe dos personas; `solo` con varios sujetos.
6. **Score/quality tags presentes** donde la familia los exige (Pony: `score_9, score_8_up, score_7_up`).
7. **Nombres propios resueltos.** ¿Quedó un nombre propio raro sin describir? → [[kri-prompt-unknown-entities]].
8. **Negativo apropiado a la familia.** Flux / Z-Image Turbo → negativo vacío (se ignora). Booru/SDXL → tags de calidad. ¿El negativo contradice o borra algo del positivo?
9. **Coherencia técnica.** ¿`strength` acorde (img2img refine vs txt2img)? ¿El positivo está en la región correcta (región vs root)?

## Salida

- **PASS** → seguí con `kri ai generate`.
- **Si falla algo** → listá los fixes concretos, corregí con `kri ai set-prompt`, y recién ahí generá. No lances con issues conocidos "a ver qué sale".

## Errores comunes

- **Generar justo después de cambiar el estilo** sin reformatear el prompt a la nueva familia.
- **Lanzar con restos** de una edición previa (tags duplicadas, contradicciones).
- **Olvidar las score tags de Pony** tras editar.
- **Dejar un negativo viejo** que pelea con el positivo nuevo.
- **Saltear el check "para ahorrar tiempo"** y quemar la generación en un prompt incoherente.

> Esta skill cierra el ciclo: [[kri-prompt-format]] (formato por modelo) + [[kri-prompt-unknown-entities]] (nombres propios) → sanity check → generar.
