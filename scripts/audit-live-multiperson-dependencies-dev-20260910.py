#!/usr/bin/env python3
from __future__ import annotations

import socket
import subprocess
from collections import Counter

EXPECTED_HOST = "desifaces-dev"
DB = "desifaces-v3-db"


def run(args, *, check=True):
    return subprocess.run([str(x) for x in args], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=check)


def psql(sql: str) -> str:
    cp = run([
        "docker", "exec", "-e", f"DF_READONLY_SQL={sql}", DB,
        "sh", "-lc",
        'psql -X -A -t -F "|" -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "${POSTGRES_DB:-desifaces}" -c "$DF_READONLY_SQL"',
    ], check=False)
    if cp.returncode != 0:
        raise RuntimeError("read-only query failed: " + cp.stdout[-1200:])
    return cp.stdout.strip()


def rows(text: str):
    return [line.split("|") for line in text.splitlines() if line.strip()]


def main() -> int:
    host = socket.gethostname().split(".", 1)[0]
    if host != EXPECTED_HOST:
        raise SystemExit(f"FAIL: run only on {EXPECTED_HOST}; current={host}")

    print("============================================================")
    print(" desifaces DEV — LIVE MULTI-PERSON DEPENDENCY AUDIT")
    print("============================================================")
    print("environment=DEV_ONLY")
    print("database_writes=NONE")
    print("provider_calls=NONE")
    print("container_restarts=NONE")
    print("production_touch=NONE")

    latest = rows(psql("""
with candidate as (
  select w.workflow_id,s.stage_run_id,s.scene_id,s.state,w.current_stage,s.updated_at,
         case when s.state='generating' then 0 else 1 end as rank_state
  from public.v3_studio_stage_runs s
  join public.v3_studio_workflows w on w.workflow_id=s.workflow_id
  where s.stage_type='fusion' and s.scope_type='scene'
  order by rank_state,s.updated_at desc
  limit 1
)
select workflow_id::text,stage_run_id::text,scene_id::text,state::text,current_stage::text,
       extract(epoch from(now()-updated_at))::int::text
from candidate;
"""))
    if not latest:
        raise SystemExit("FAIL: no Fusion Scene stage found")
    workflow_id, stage_id, scene_id, scene_state, current_stage, age = latest[0]
    print("\n===== CURRENT/LATEST FUSION SCENE =====")
    print(f"workflow_id={workflow_id}")
    print(f"stage_run_id={stage_id}")
    print(f"scene_id={scene_id}")
    print(f"scene_state={scene_state}")
    print(f"workflow_current_stage={current_stage}")
    print(f"stage_age_seconds={age}")

    dependency_sql = f"""
select p.stage_type::text,p.stage_run_id::text,p.state::text,
       case when exists(
         select 1 from public.v3_studio_stage_outputs o
         join public.v3_studio_review_items r
           on r.stage_run_id=o.stage_run_id and r.media_id=o.media_id
         where o.stage_run_id=p.stage_run_id and o.is_active=true and r.decision='approved'
       ) then 'yes' else 'no' end,
       coalesce(p.participant_id::text,''),coalesce(p.dialogue_turn_id::text,'')
from public.v3_studio_stage_dependencies d
join public.v3_studio_stage_runs p on p.stage_run_id=d.parent_stage_run_id
where d.child_stage_run_id='{stage_id}'::uuid
order by p.stage_type,p.created_at,p.stage_run_id;
"""
    deps = rows(psql(dependency_sql))
    print("\n===== DECLARED FUSION DEPENDENCIES =====")
    print("columns=type|stage_run_id|state|approved_usable_output|participant_id|dialogue_turn_id")
    for row in deps:
        print("|".join(row))
    if not deps:
        print("NO_DEPENDENCIES")

    required_turns = {r[0] for r in rows(psql(f"""
select turn_id::text from public.v3_dialogue_turns
where scene_id='{scene_id}'::uuid and turn_kind='speech'
order by sequence_no,turn_id;
"""))}
    required_participants = {r[0] for r in rows(psql(f"""
select participant_id::text from public.v3_scene_participants where scene_id='{scene_id}'::uuid
union
select speaker_participant_id::text from public.v3_dialogue_turns
where scene_id='{scene_id}'::uuid and turn_kind='speech' and speaker_participant_id is not null;
"""))}

    audio_deps = [r for r in deps if r[0] == "audio"]
    face_deps = [r for r in deps if r[0] == "face"]
    declared_turns = {r[5] for r in audio_deps if len(r) > 5 and r[5]}
    declared_participants = {r[4] for r in face_deps if len(r) > 4 and r[4]}
    audio_usable = {r[5] for r in audio_deps if len(r) > 5 and r[2] == "approved" and r[3] == "yes" and r[5]}
    face_usable = {r[4] for r in face_deps if len(r) > 4 and r[2] == "approved" and r[3] == "yes" and r[4]}

    print("\n===== EXACT DEPENDENCY SET PROOF =====")
    print(f"required_scene_participants={len(required_participants)}")
    print(f"declared_face_dependencies={len(declared_participants)}")
    print(f"approved_usable_face_dependencies={len(face_usable)}")
    print(f"required_speech_turns={len(required_turns)}")
    print(f"declared_audio_dependencies={len(declared_turns)}")
    print(f"approved_usable_audio_dependencies={len(audio_usable)}")
    print(f"missing_face_dependency_ids={','.join(sorted(required_participants-declared_participants)) or 'NONE'}")
    print(f"unready_face_ids={','.join(sorted(required_participants-face_usable)) or 'NONE'}")
    print(f"missing_audio_dependency_turn_ids={','.join(sorted(required_turns-declared_turns)) or 'NONE'}")
    print(f"unready_audio_turn_ids={','.join(sorted(required_turns-audio_usable)) or 'NONE'}")

    exact_face = required_participants == declared_participants == face_usable
    exact_audio = required_turns == declared_turns == audio_usable and bool(required_turns)
    print(f"FACE_DEPENDENCY_EXACT={'PASS' if exact_face else 'FAIL'}")
    print(f"AUDIO_DEPENDENCY_EXACT={'PASS' if exact_audio else 'FAIL'}")

    child_sql = f"""
select j.status::text,count(*)::text
from public.studio_jobs j
where j.studio_type='fusion' and (
 j.payload_json #>> '{{provider_options,billing_context,billing_parent_job_id}}'='{stage_id}' or
 j.payload_json #>> '{{provider_options,billing_context,parent_longform_job_id}}'='{stage_id}' or
 j.payload_json #>> '{{provider_options,billing_context,parent_job_id}}'='{stage_id}' or
 j.payload_json #>> '{{tags,billing_context,billing_parent_job_id}}'='{stage_id}' or
 j.payload_json #>> '{{tags,billing_context,parent_longform_job_id}}'='{stage_id}' or
 j.payload_json #>> '{{tags,billing_context,parent_job_id}}'='{stage_id}'
)
group by j.status order by j.status;
"""
    child_rows = rows(psql(child_sql))
    counts = Counter({r[0]: int(r[1]) for r in child_rows})
    print("\n===== FUSION VIDEO CHILD JOBS =====")
    if child_rows:
        for state, count in child_rows:
            print(f"video_child_{state}={count}")
    else:
        print("video_child_jobs=0")
    total_children = sum(counts.values())
    active = sum(counts.get(s, 0) for s in ("queued", "processing", "running", "submitted", "pending"))
    print(f"video_child_total={total_children}")
    print(f"video_child_active={active}")
    print("JOB_SEMANTICS=FUSION_VIDEO_CLIPS_USING_APPROVED_FACE_AND_AUDIO_INPUTS")

    allowed = current_stage == "fusion" and exact_face and exact_audio
    print("\n===== RELEASE-GATE CLASSIFICATION =====")
    print(f"VIDEO_DEPENDENCIES_SATISFIED={'YES' if allowed else 'NO'}")
    if not allowed:
        print("CLASSIFICATION=DEPENDENCY_VIOLATION_RELEASE_BLOCKER")
    elif scene_state == "generating":
        print("CLASSIFICATION=VALID_FUSION_VIDEO_GENERATION")
    elif scene_state in {"awaiting_review", "approved"}:
        print("CLASSIFICATION=VALID_FUSION_VIDEO_COMPLETE")
    elif scene_state == "failed":
        print("CLASSIFICATION=VALID_DEPENDENCIES_SCENE_FAILED_FOR_OTHER_REASON")
    else:
        print(f"CLASSIFICATION=VALID_DEPENDENCIES_SCENE_{scene_state.upper()}")

    print("\n============================================================")
    print(" DEPENDENCY AUDIT COMPLETE")
    print("============================================================")
    return 0 if allowed else 3


if __name__ == "__main__":
    raise SystemExit(main())
