---
name: kri-prompt-unknown-entities
description: Usar al armar un prompt de imagen (Krita AI Diffusion, ComfyUI, etc.) que menciona un nombre propio que NO es una mega-celebridad/marca/landmark mundialmente conocido — personajes, criaturas, objetos, lugares, facciones o ítems de ficción, juegos, anime o nichos (ej. "Deku Tree", "Master Chief", "un Diglett"). Antes de escribir el prompt hay que googlear la entidad y describir cómo se ve de verdad, en vez de parafrasearla a ciegas e inventar algo genérico.
metadata:
  type: technique
  reusable: kri-prompt-format, iarte-prompt-*
---

# Resolver nombres propios desconocidos antes de promptear

## Principio

**No parafrasees a ciegas un nombre propio que no conocés con certeza.** Si el prompt nombra una entidad de ficción/nicho (personaje, criatura, objeto, lugar, facción, ítem) que **no** es una mega-celebridad / marca / landmark real que el modelo seguro conoce, **googlealo primero** para saber qué es y cómo se ve, y recién después describí sus rasgos visuales concretos. Si no, traducís "Deku Tree" a "un árbol con rostro ancestral" y perdés la referencia.

## Cuándo aplica

- El prompt tiene un sustantivo propio que no podés visualizar con seguridad.
- Personajes/criaturas/ítems de juegos, anime, cómics, películas de nicho, lore.
- Nombres en otro idioma, marcas chicas, lugares ficticios.

**Cuándo NO** (no hace falta googlear): mega-celebridades reales (Messi, Trump), marcas/landmarks universales (Coca-Cola, Torre Eiffel, Pikachu), o conceptos genéricos.

## Workflow

1. **Detectá** los nombres propios del pedido.
2. **Decidí por cada uno:** ¿el modelo seguro lo conoce? Mega-famoso real/landmark → seguí. Si dudás → googlealo igual (cuesta poco, evita el "teléfono descompuesto").
3. **Web search** → extraé rasgos visuales concretos: forma, colores, materiales, vestimenta, escala, contexto, origen (franquicia).
4. **Codificá según el modelo destino:**
   - **Modelos booru** (Pony, Illustrious, NoobAI, anime): muchas veces conocen al personaje como **tag exacto** (`hatsune miku, vocaloid`). Conservá el tag **y** sumá rasgos clave por si el checkpoint no lo clava. El search sirve para confirmar el tag.
   - **Modelos de lenguaje natural** (Flux, Z-Image, Qwen, SD3, SDXL realista): rara vez clavan el personaje por nombre → **describí la apariencia** (colores, forma, ropa, contexto), opcionalmente mencionando la franquicia.

## Ejemplo

```
Pedido: "haceme un Deku Tree"
❌ (adivinando) → "un árbol con rostro ancestral"   ← inventado, pierde la referencia

✅ web search "Deku Tree" → The Legend of Zelda: árbol colosal y anciano,
   espíritu guardián del bosque, con un rostro sabio formado en la corteza
   (ojos, cejas pobladas, bigote/boca de musgo), copa enorme, raíces nudosas.

   - Modelo natural: "a colossal ancient tree with a wise face formed in its
     bark, thick mossy eyebrows, gnarled roots, lush canopy, fantasy game art,
     The Legend of Zelda"
   - Modelo booru:   "tree, giant tree, ancient tree, face in tree, wise old
     face, thick eyebrows, moss, gnarled roots, no humans, fantasy,
     the legend of zelda, masterpiece, best quality"
```

## Quick reference

1. ¿Nombre propio no-megafamoso? → web search.
2. Extraé rasgos visuales concretos + franquicia.
3. Booru: tag exacto + rasgos. Natural: describí apariencia.

## Errores comunes

- **Parafrasear sin googlear** → resultado genérico que no se parece.
- **Tirar solo el nombre** en un modelo natural que no lo conoce → ignora la referencia.
- **Descartar el tag del personaje** en un modelo booru que sí lo conoce → perdés el mejor anclaje.
- **Googlear de más** una mega-celebridad obvia → innecesario.

> En Krita esto es el paso 3 del workflow de [[kri-prompt-format]]. También aplica a las skills `iarte-prompt-*` (ComfyUI).
