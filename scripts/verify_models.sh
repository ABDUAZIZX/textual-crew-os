#!/usr/bin/env bash
# Verify that the Ollama models required by Textual Crew OS are present.
#
# Exit codes:
#   0 - all required models present
#   1 - one or more required models missing, or Ollama not installed
#
# Listed models cover Stage 2-4 agents. Optional offensive models
# (WhiteRabbitNeo / Lily) are checked separately because LAB_MODE is opt-in.

set -euo pipefail

REQUIRED_MODELS=(
    "qwen2.5:3b"
    "qwen2.5-coder:7b-instruct-q4_K_M"
    "llama3.1:8b-instruct-q4_K_M"
    "nomic-embed-text:latest"
)

OPTIONAL_MODELS=(
    "hf.co/fdtn-ai/Foundation-Sec-8B-Reasoning-Q4_K_M-GGUF:latest"
    "hf.co/mradermacher/WhiteRabbitNeo-2.5-Qwen-2.5-Coder-7B-OBLITERATED-i1-GGUF:Q4_K_M"
    "hf.co/QuantFactory/Lily-Cybersecurity-7B-v0.2-GGUF:Q4_K_M"
)

if ! command -v ollama >/dev/null 2>&1; then
    printf 'ollama: not installed or not on PATH\n' >&2
    exit 1
fi

installed="$(ollama list 2>/dev/null | awk 'NR>1 {print $1}')"

print_status() {
    local label="$1" model="$2"
    if grep -qFx "$model" <<<"$installed"; then
        printf '  [ OK ] %-8s %s\n' "$label" "$model"
        return 0
    fi
    printf '  [MISS] %-8s %s\n' "$label" "$model"
    return 1
}

printf 'Required models:\n'
missing_required=0
for m in "${REQUIRED_MODELS[@]}"; do
    print_status "required" "$m" || missing_required=$((missing_required + 1))
done

printf '\nOptional models (LAB_MODE / defensive variants):\n'
for m in "${OPTIONAL_MODELS[@]}"; do
    print_status "optional" "$m" || true
done

if (( missing_required > 0 )); then
    printf '\n%d required model(s) missing. Install with:\n' "$missing_required" >&2
    for m in "${REQUIRED_MODELS[@]}"; do
        if ! grep -qFx "$m" <<<"$installed"; then
            printf '  ollama pull %s\n' "$m" >&2
        fi
    done
    exit 1
fi

printf '\nAll required models present.\n'
exit 0
