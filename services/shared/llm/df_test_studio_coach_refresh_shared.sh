#!/usr/bin/env bash
set -euo pipefail

: "${DATABASE_URL:?DATABASE_URL is required}"

export DF_STUDIO_COACH_REFRESH_ENABLED="${DF_STUDIO_COACH_REFRESH_ENABLED:-true}"
export DF_STUDIO_COACH_AUTO_ACTIVATE="${DF_STUDIO_COACH_AUTO_ACTIVATE:-false}"
export DF_STUDIO_COACH_REFRESH_RUN_ONCE="true"
export DF_STUDIO_COACH_LLM_PROVIDER="${DF_STUDIO_COACH_LLM_PROVIDER:-openai}"
export DF_STUDIO_COACH_LLM_MODEL="${DF_STUDIO_COACH_LLM_MODEL:-gpt-4.1-mini}"
export DF_STUDIO_COACH_REFRESH_CONTEXTS_JSON="${DF_STUDIO_COACH_REFRESH_CONTEXTS_JSON:-[{\"studio\":\"face\",\"modes\":[\"text-to-image\",\"image-to-image\"],\"locales\":[\"en\"]},{\"studio\":\"audio\",\"modes\":[\"tts\"],\"locales\":[\"en\"]},{\"studio\":\"fusion\",\"modes\":[\"talking_video\",\"cinematic_video_direction\"],\"locales\":[\"en\"]}]}"

python -m desifaces_shared.llm.studio_coach_refresh_worker
