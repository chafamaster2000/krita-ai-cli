---
name: kri-prompt-format
description: Usar SIEMPRE antes de inyectar o cambiar un prompt en AI Diffusion de Krita vía el CLI kri (kri ai set-prompt), al elegir las palabras del positivo o del negativo para una generación en Krita. Cubre clasificar la familia del modelo del estilo activo (Flux, Flux Kontext, Z-Image, Qwen, SD3, SDXL realista = lenguaje natural; Illustrious, NoobAI, Pony, Animagine, anime SDXL/SD1.5 = tags danbooru) y resolver nombres propios desconocidos (personajes, criaturas, lugares de ficción) por web antes de escribir el prompt.
metadata:
  type: technique
  target: kri CLI + AI Diffusion (Acly/krita-ai-diffusion)
---

# Formatear prompts para el modelo activo en Krita (AI Diffusion)

## Principio

**Nunca escribas un prompt a ciegas.** Cada familia de modelo quiere un *idioma* de prompt distinto: lenguaje natural (frases) vs. tags danbooru (palabras separadas por comas). Poner tags booru en un modelo de lenguaje natural —o viceversa— degrada el resultado. Antes de cada `kri ai set-prompt`, mirá qué estilo está activo, clasificá su familia, y formateá positivo + negativo para esa familia.

## El workflow de hierro (antes de CADA `kri ai set-prompt`)

1. **Leé el estado.** Llamá `kri ai status` (o `kri status`). Mirá `model.architecture` (**dato duro**, el resuelto del checkpoint, ej. `"sdxl"`, `"flux"`, `"zimage"`, `"illu"`, `"qwen"`, `"sd3"`), `model.checkpoint`, y como respaldo el `style` filename. Mirá también `prompt.positive` / `prompt.negative` actuales.
2. **Clasificá la familia** con la **Tabla A (por architecture)**. Si `model.architecture` es `sdxl`/`sd15`/`auto` o `model.available` es false (versión vieja del plugin), usá la **Tabla B (por nombre)** sobre el checkpoint y el style filename/name en minúsculas. Si nada matchea → **no adivines** (ver "Cuando la familia no es obvia").
3. **Resolvé nombres propios desconocidos.** Si el prompt menciona un personaje/criatura/lugar de ficción que no es una mega-celebridad real, googlealo primero → **REQUERIDO:** [[kri-prompt-unknown-entities]].
4. **Reescribí** positivo + negativo en la convención de esa familia.
5. **Inyectá** con `kri ai set-prompt`. (Si vas a cambiar el estilo, usá `kri ai set-params` y volvé al paso 1: el estilo nuevo puede cambiar la convención.)
6. **Antes de generar**, pasá el prompt final por [[kri-prompt-sanity-check]].

## Tabla A — por `model.architecture` (dato duro, preferí esta)

El valor de `model.architecture` viene del enum `Arch` de AI Diffusion (`.name`).

| `model.architecture` | Familia | Convención positivo | Negativo |
|---|---|---|---|
| `flux`, `flux_k`, `flux2_4b`, `flux2_9b`, `chroma` | Flux / Chroma | Lenguaje natural largo, 1-4 frases (`flux_k` = instrucción de edición) | Ignorado (no usar) |
| `zimage` | Z-Image / Turbo | Lenguaje natural, frases concisas | Ignorado (Turbo, CFG≈1) |
| `qwen`, `qwen_e`, `qwen_e_p`, `qwen_l` | Qwen-Image | Lenguaje natural (ES o EN); fuerte en texto en imagen | Opcional, poco peso |
| `sd3` | SD3 / 3.5 | Lenguaje natural; tags toleradas pero peor | Soportado, suave |
| `ernie` | ERNIE Image | Lenguaje natural | Opcional |
| `illu`, `illu_v`, `anima` | Illustrious / Anima (anime) | Tags danbooru | `worst quality, low quality, ...` |
| `sdxl`, `sd15` | **Ambiguo** → usá Tabla B sobre el checkpoint/nombre | (depende) | (depende) |
| `auto`, ausente, `model.available=false` | Desconocido → Tabla B; si tampoco, ver abajo | — | — |

## Tabla B — por nombre (fallback para sdxl/sd15 o sin architecture)

Matcheá keyword en el `model.checkpoint` y en el `style` filename/name (minúsculas). El primero que matchea gana.

| Keyword | Familia | Convención positivo | Negativo |
|---|---|---|---|
| `pony`, `ponydiffusion` | Pony Diffusion XL | Tags danbooru **+ score tags** (`score_9, score_8_up, score_7_up`), `source_anime`, `rating_safe` | `score_4, score_5, score_6` + tags de calidad |
| `noob`, `noobai`, `illustrious`, `ilxl` | NoobAI / Illustrious (SDXL anime) | Tags danbooru (NoobAI acepta e621), orden: calidad → sujeto → atributos → fondo | `worst quality, low quality, ...` |
| `animagine` | Animagine XL | Tags danbooru: `personaje, serie, atributos, masterpiece, best quality` | `lowres, bad anatomy, ...` |
| `anime`, `booru`, `hentai`, `waifu` | Anime SDXL/SD1.5 | Tags danbooru | Tags de calidad estándar |
| `cinematic`, `photo`, `realistic`, `realism`, `juggernaut`, `digital-artwork`, `concept` | SDXL realista | Lenguaje natural descriptivo (frases) | Tags de calidad estándar |

> Por qué el split: `flux`/`zimage`/`qwen`/`sd3`/`illu` ya determinan la convención por sí solos. Pero `sdxl`/`sd15` corren tanto modelos booru (Pony, NoobAI, Animagine) como realistas de lenguaje natural — ahí el architecture no alcanza y mandan el checkpoint y el nombre.

### Cuando la familia no es obvia

No inventes la convención. En orden:
1. Mirá `model.checkpoint` (de `ai_status`) — suele tener el nombre real (`ponyRealism`, `noobaiXL`, etc.).
2. `kri ai styles` por si el name da una pista.
3. Si sigue ambiguo, **preguntale al usuario** qué checkpoint corre ese estilo, o web-searcheá el nombre del checkpoint.
4. Default conservador solo si todo falla: lenguaje natural **+** una línea breve de tags, y avisá al usuario que estás adivinando.

## Lenguaje natural vs. tags danbooru

**Lenguaje natural** (Flux, Z-Image, Qwen, SD3, SDXL realista): frases como se lo describirías a un fotógrafo/director.
```
✅ "A young woman with long red hair sitting by a window at golden hour,
    soft rim light, shallow depth of field, photorealistic, 35mm."
❌ "1girl, red hair, long hair, window, golden hour, depth of field"   ← tags en modelo natural
```

**Tags danbooru** (Illustrious, NoobAI, Pony, Animagine, anime SDXL): palabras/frases cortas separadas por comas, sin oraciones.
```
✅ "1girl, long hair, red hair, sitting, window, sunset, depth of field,
    masterpiece, best quality"
❌ "A young woman with long red hair sitting by a window."             ← frases en modelo booru
```

### Reglas de conversión
- **Tags → natural:** uní las tags en frases con sujeto y relaciones espaciales. `1girl` → "a woman/girl". Convertí tags de calidad (`masterpiece, best quality`) en adjetivos o descartalas (en Flux/Z-Image no aportan).
- **Natural → tags:** extraé sustantivos/atributos clave, separá por comas, agregá count tag (`1girl`/`1boy`/`2girls`), agregá tags de calidad de la familia. Convertí cámara/luz a tags (`depth of field`, `cinematic lighting`).
- **Conservá el peso semántico:** lo que el usuario enfatizó primero, va primero (en booru) o en la frase principal (en natural).

## Nombres propios desconocidos → googlear primero

Si el prompt nombra una entidad propia que no es una mega-celebridad/marca/landmark conocidísimo (Messi, Torre Eiffel), **NO la parafrasees a ciegas** ("Deku Tree" → "un árbol ancestral"): googleala y describí cómo se ve de verdad. El detalle completo (reglas booru vs natural, ejemplo) está en **[[kri-prompt-unknown-entities]]** — es el paso 3 del workflow.

## Negativos por familia

| Familia | Negativo |
|---|---|
| Flux / Flux Kontext / Z-Image Turbo / Schnell | **Dejalo vacío.** No usan CFG real; el negativo se ignora o casi. |
| Qwen / SD3 | Opcional, breve. |
| SDXL realista | Tags de calidad: `blurry, low quality, deformed, extra fingers, bad anatomy`. |
| Pony | `score_4, score_5, score_6, worst quality, low quality` + lo que no querés. |
| Illustrious / NoobAI / Animagine / anime | `worst quality, low quality, lowres, bad anatomy, bad hands, jpeg artifacts, watermark, signature`. |

Recordá: `kri ai set-prompt` escribe el positivo en la región activa (o root) y el negativo **siempre en el root**.

## Quick reference

1. `kri ai status` → mirá `model.architecture` (y `model.checkpoint`).
2. Clasificá: Tabla A; si es `sdxl`/`sd15`/`auto`, Tabla B. ¿Ambiguo? No adivines.
3. ¿Nombre propio raro? Web search.
4. Reescribí positivo + negativo en la convención.
5. `kri ai set-prompt`.

## Errores comunes

- **Inyectar sin chequear el estilo** → el error #1. Siempre `kri status` primero.
- **Tags booru en Flux/Z-Image** (o frases en Pony/Illustrious) → resultado pobre.
- **Mandar negativo a Flux/Z-Image** → esfuerzo perdido; dejalo vacío.
- **Parafrasear un nombre propio sin googlear** → "Deku Tree" → genérico.
- **Fiarse del nombre del estilo** cuando el checkpoint real es de otra familia.
- **Olvidar score tags en Pony** → calidad baja aunque el resto esté bien.

## Señal de architecture (estado)

El plugin expone `model` en `ai_status`/`ai_overview` con `architecture` (resuelto del checkpoint vía `resolve_arch`), `checkpoint` y `loras` — por eso Tabla A clasifica por dato duro. Si `model.available` es false (el plugin viejo todavía no fue redeployado, o falló la resolución), caé a Tabla B (por nombre). `architecture` puede ser `auto` si AI Diffusion no pudo determinarlo (estilo sin checkpoint asignado / desconectado) → tratalo como ambiguo.
