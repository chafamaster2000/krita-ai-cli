#!/usr/bin/env python3
"""PreToolUse hook for the krita-mcp prompt workflow.

Fires before krita_ai_set_prompt and krita_ai_generate. It calls the Krita bridge
itself (localhost:5678 ai_status), reads the active style's resolved architecture,
and injects a reminder naming the model family + prompt convention so the active
model is enforced even if the skills aren't loaded in context.

Reminder-only: never blocks. Any failure degrades to a generic reminder. The
output is PreToolUse `additionalContext` (added to the model's context, not shown
to the user as a block)."""
import sys
import os
import json
import urllib.request

NAT = "lenguaje natural (frases descriptivas)"
TAG = "tags danbooru (palabras separadas por comas)"

# Arch.name -> (familia, convención del positivo, negativo)
ARCH = {
    "flux": ("Flux", NAT, "vacío (se ignora)"),
    "flux_k": ("Flux Kontext", NAT, "vacío (se ignora)"),
    "flux2_4b": ("Flux.2", NAT, "vacío (se ignora)"),
    "flux2_9b": ("Flux.2", NAT, "vacío (se ignora)"),
    "chroma": ("Chroma", NAT, "vacío (se ignora)"),
    "zimage": ("Z-Image", NAT, "vacío (Turbo, CFG≈1)"),
    "qwen": ("Qwen-Image", NAT, "opcional, poco peso"),
    "qwen_e": ("Qwen Edit", NAT, "opcional"),
    "qwen_e_p": ("Qwen Edit Plus", NAT, "opcional"),
    "qwen_l": ("Qwen Layered", NAT, "opcional"),
    "sd3": ("SD3 / 3.5", NAT, "soportado, suave"),
    "ernie": ("ERNIE Image", NAT, "opcional"),
    "illu": ("Illustrious", TAG, "worst quality, low quality, ..."),
    "illu_v": ("Illustrious (v-pred)", TAG, "worst quality, low quality, ..."),
    "anima": ("Anima (anime)", TAG, "worst quality, low quality, ..."),
}


def fetch_status():
    token = os.environ.get("KRITAMCP_TOKEN", "")
    url = os.environ.get("KRITA_URL", "http://localhost:5678")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Kritamcp-Token"] = token
    body = json.dumps({"action": "ai_status", "params": {}}).encode()
    req = urllib.request.Request(url, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=3) as r:
        return json.loads(r.read().decode())


def model_line():
    """Best-effort one-liner about the active model. Empty string on any failure."""
    try:
        st = fetch_status()
    except Exception:
        return ""
    m = st.get("model") or {}
    arch = m.get("architecture")
    ck = m.get("checkpoint")
    info = ARCH.get(arch)
    if info:
        fam, conv, neg = info
        return f"Modelo activo: architecture='{arch}' ({fam}). Convención del positivo: {conv}. Negativo: {neg}. "
    if arch in ("sdxl", "sd15"):
        return (
            f"Modelo activo: architecture='{arch}' (AMBIGUO: SDXL/SD1.5 corre tanto "
            f"booru —Pony/NoobAI/Animagine— como realista de lenguaje natural). "
            f"Mirá checkpoint='{ck}' y el nombre del estilo para decidir la convención. "
        )
    if arch:
        return f"Modelo activo: architecture='{arch}'. "
    return ""


def main():
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except Exception:
        data = {}
    tool = data.get("tool_name", "")

    if "generate" in tool:
        base = (
            "Antes de generar: corré image-prompt-sanity-check sobre el prompt final "
            "(coherencia con el modelo activo y con lo que pidió el usuario; restos de "
            "ediciones, contradicciones, score tags si la familia los pide, negativo, "
            "nombres propios resueltos)."
        )
    else:  # set_prompt
        base = (
            "Antes de escribir este prompt usá: krita-ai-prompt-format (formatear según "
            "el modelo activo), e image-prompt-unknown-entities (googlear nombres propios "
            "no famosos antes de describirlos)."
        )

    line = model_line()
    msg = (line + base) if line else (base + " (No pude leer ai_status; verificá el modelo activo vos.)")

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": msg,
        }
    }))


if __name__ == "__main__":
    main()
