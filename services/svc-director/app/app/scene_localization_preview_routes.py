"""DEV-only intent-first scene preview for the existing Director HITL.
No state mutation, TTS, provider auto-approval or independent QA gate.
"""
from __future__ import annotations
import json
import os
from typing import Any
import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from .security import DirectorAuthContext, get_director_auth
from .localization_source_contract import validate_english_source

router = APIRouter()

class SceneTurn(BaseModel):
    turn_id: str = Field(min_length=1,max_length=100)
    speaker: str = Field(min_length=1,max_length=160)
    text: str = Field(min_length=1,max_length=4000)

class LocalizeSceneIn(BaseModel):
    source_locale: str = "en-US"
    target_locale: str = "hi-IN"
    scene_context: str = Field(default="",max_length=4000)
    feedback: str = Field(default="",max_length=1000)
    turns: list[SceneTurn] = Field(min_length=1,max_length=50)

@router.post("/api/director/dialogue/localize-preview")
async def localize_preview(body: LocalizeSceneIn,
                           auth: DirectorAuthContext=Depends(get_director_auth)):
    if os.getenv("DF_RUNTIME_ENV","").lower() not in {"dev","development"}:
        raise HTTPException(404,detail="dev_only")
    if body.target_locale != "hi-IN" or body.source_locale != "en-US":
        raise HTTPException(422,detail="unsupported_preview_language_pair")
    if len({t.turn_id for t in body.turns}) != len(body.turns):
        raise HTTPException(422,detail="duplicate_turn_ids")
    try:
        for turn in body.turns:
            validate_english_source(turn.text)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    key=os.getenv("OPENAI_API_KEY","").strip()
    if not key:
        raise HTTPException(503,detail="localization_provider_not_configured")
    instructions=(
       "You are a native Hindi screenplay localizer. Interpret the COMPLETE "
       "conversation before rewriting. Retain who acts, who receives each action, "
       "the relationships, promise, timing and speaker emotional intent. "
       "'I cannot wait to serve customers' means eager anticipation by the café "
       "owner, not waiting for YOU to serve. Keep facts, names, order, all turn IDs "
       "and speaker identifiers. Natural spoken Hindi, not word-for-word calques. "
       "Return ONLY JSON object {turns:[{turn_id,speaker,text},...]}. "
       "This is a draft for the EXISTING human dialogue review; no AI approval."
    )
    payload={"model":os.getenv("DF_SCENE_INTENT_LOCALIZATION_MODEL","gpt-4.1-mini"),
       "instructions":instructions,
       "input":json.dumps(body.model_dump(),ensure_ascii=False),
       "max_output_tokens":5000}
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response=await client.post(
              os.getenv("OPENAI_BASE_URL","https://api.openai.com/v1").rstrip("/")+"/responses",
              headers={"Authorization":"Bearer "+key},json=payload)
            response.raise_for_status()
            data=response.json()
        raw=data.get("output_text") or "\n".join(
          c.get("text","") for o in data.get("output",[]) if isinstance(o,dict)
          for c in o.get("content",[]) if isinstance(c,dict))
        raw=raw.strip()
        if raw.startswith("```"):
            raw=raw.split("\n",1)[-1].rsplit("```",1)[0].strip()
        turns=json.loads(raw)["turns"]
        if not isinstance(turns,list) or len(turns)!=len(body.turns):
            raise ValueError("count_mismatch")
        for source,translated in zip(body.turns,turns):
            if not isinstance(translated,dict) or translated.get("turn_id")!=source.turn_id or translated.get("speaker")!=source.speaker or not isinstance(translated.get("text"),str) or not translated["text"].strip():
                raise ValueError("turn_identity_mismatch")
    except (httpx.HTTPError,ValueError,KeyError,TypeError) as exc:
        raise HTTPException(502,detail="localization_preview_unavailable") from exc
    return {"source_locale":"en-US","target_locale":"hi-IN",
            "status":"pending_hitl","turns":turns,"approved":False}
