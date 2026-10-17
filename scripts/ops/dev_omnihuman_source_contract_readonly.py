#!/usr/bin/env python3
"""Read-only DEV OmniHuman source contract inspector: no credentials or URLs output."""
import asyncio,json,os
import asyncpg
ATTEMPT="992b2904-8ddf-4788-8e1e-1aa0d076fb9a"
def safe_meta(v):
 if not isinstance(v,dict):return {}
 allow=("audio","duration","fps","frame","start","offset","padding","trim","provider","status","model","sync","mode")
 return {k:("present" if isinstance(x,(dict,list)) else x)
         for k,x in v.items() if any(w in k.lower() for w in allow)
         and isinstance(x,(type(None),bool,int,float,str,dict,list))
         and not any(w in k.lower() for w in ("url","uri","token","secret","key","path","script","text"))}
async def main():
 if os.environ.get("QC_DEV_HOST_VERIFIED")!="1":raise RuntimeError("DEV_ONLY")
 c=await asyncpg.connect(os.environ["DATABASE_URL"],timeout=12,
   server_settings={"default_transaction_read_only":"on"})
 try:
  raw=await c.fetchval("SELECT metadata_json FROM v3_studio_stage_attempts WHERE attempt_id=$1::uuid",ATTEMPT)
  meta=json.loads(raw) if isinstance(raw,str) else raw
  if len(meta["children"])!=6:raise RuntimeError("EXPECTED_SIX_TURNS")
  jobs=[x.get("fusion_job_id") for x in meta["children"]]
  print("PARENT_ATTEMPT_FOUND=YES")
  print("CHILDREN=6")
  for i,(child,job) in enumerate(zip(meta["children"],jobs),1):
   print(json.dumps({"turn":i,"job_id":job,"participant_id":child.get("participant_id"),
     "audio_media_id":child.get("audio_media_id"),
     "provider_status":child.get("provider_status"),
     "child_metadata":safe_meta(child)},default=str))
  # Check only exact child job records without guessing digital_performances.id.
  rows=await c.fetch("""SELECT column_name FROM information_schema.columns
      WHERE table_schema='public' AND table_name='v3_generation_jobs'
      ORDER BY ordinal_position""")
  print("GENERATION_JOB_COLUMNS=",[r["column_name"] for r in rows])
  rows=await c.fetch("""SELECT column_name FROM information_schema.columns
      WHERE table_schema='public' AND table_name='digital_performances'
      ORDER BY ordinal_position""")
  print("DIGITAL_PERFORMANCES_COLUMNS=",[r["column_name"] for r in rows])
  print("NEXT=VERIFY_JOB_TO_PROVIDER_REQUEST_METADATA_WITH_EXACT_COLUMNS")
 finally:await c.close()
asyncio.run(main())
