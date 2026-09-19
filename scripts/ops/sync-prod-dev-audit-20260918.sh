#!/usr/bin/env bash
set -Eeuo pipefail

PROD_HOST="${PROD_HOST:-desifaces-gpu}"
DEV_HOST="${DEV_HOST:-desifaces-dev}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="${OUT:-/tmp/desifaces-prod-dev-sync-${STAMP}}"
mkdir -p "$OUT"

fail(){ echo "BLOCKER: $*" >&2; exit 2; }
need(){ command -v "$1" >/dev/null 2>&1 || fail "required local command missing: $1"; }
need ssh
need python3

collect_remote() {
  local host="$1" role="$2" outfile="$3"
  echo "CAPTURE_${role}=START host=${host}"
  if ! ssh -o BatchMode=yes -o ConnectTimeout=12 "$host" "ROLE='$role' python3 -" >"$outfile" <<'PY'
import hashlib, json, os, re, shlex, socket, subprocess, sys

ROLE=os.environ.get("ROLE","UNKNOWN")

def run(cmd, timeout=30):
    try:
        p=subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        return {"rc":p.returncode,"out":p.stdout,"err":p.stderr}
    except Exception as e:
        return {"rc":999,"out":"","err":repr(e)}

def sha(s):
    return hashlib.sha256((s or "").encode("utf-8",errors="replace")).hexdigest()

def env_map(items):
    out={}
    for item in items or []:
        if "=" in item:
            k,v=item.split("=",1)
            out[k]={"sha256":sha(v),"present":True}
        else:
            out[item]={"sha256":sha(""),"present":True}
    return out

def source_hash(container):
    script=r'''set -eu
for d in /app/app /app/src /workspace/app /workspace/src; do
  if [ -d "$d" ]; then
    find "$d" -type f \( -name '*.py' -o -name '*.js' -o -name '*.jsx' -o -name '*.ts' -o -name '*.tsx' -o -name '*.json' \) \
      ! -path '*/node_modules/*' ! -path '*/.next/*' ! -path '*/__pycache__/*' -print0 2>/dev/null \
      | sort -z | xargs -0 -r sha256sum 2>/dev/null
  fi
done'''
    r=run(["docker","exec",container,"sh","-lc",script], timeout=90)
    if r["rc"]!=0:
        return {"available":False,"sha256":None,"file_count":0}
    lines=[x for x in r["out"].splitlines() if x.strip()]
    return {"available":bool(lines),"sha256":sha("\n".join(lines)),"file_count":len(lines)}

def git_repos():
    home=os.path.expanduser("~")
    candidates=[
      os.path.join(home,"workspace","desifaces-v3"),
      os.path.join(home,"workspace","desifaces_backend"),
      os.path.join(home,"workspace","desifaces_web"),
      os.path.join(home,"workspace","desifaces-web"),
      os.path.join(home,"workspace","web"),
    ]
    found=[]
    seen=set()
    for p in candidates:
        if p in seen or not os.path.isdir(os.path.join(p,".git")):
            continue
        seen.add(p)
        head=run(["git","-C",p,"rev-parse","HEAD"])
        branch=run(["git","-C",p,"branch","--show-current"])
        status=run(["git","-C",p,"status","--porcelain"])
        origin=run(["git","-C",p,"remote","get-url","origin"])
        if head["rc"]==0:
            remote=(origin["out"].strip() if origin["rc"]==0 else "")
            remote=re.sub(r'https://[^/@]+:[^/@]+@','https://***@',remote)
            found.append({
              "path":p,"head":head["out"].strip(),"branch":branch["out"].strip(),
              "dirty":bool(status["out"].strip()),"origin":remote
            })
    return found

def db_schema(containers):
    # Hash schema only; never emit DB credentials or row data.
    for c in containers:
        name=c["name"]
        marker=(name+" "+c.get("image","")).lower()
        if "postgres" not in marker and not re.search(r'(^|[-_])db($|[-_])', marker):
            continue
        try:
            insp=json.loads(run(["docker","inspect",name])["out"])[0]
        except Exception:
            continue
        vals={}
        for e in ((insp.get("Config") or {}).get("Env") or []):
            if "=" in e:
                k,v=e.split("=",1); vals[k]=v
        user=vals.get("POSTGRES_USER","postgres")
        db=vals.get("POSTGRES_DB",user)
        pw=vals.get("POSTGRES_PASSWORD","")
        cmd=["docker","exec"]
        if pw:
            cmd += ["-e",f"PGPASSWORD={pw}"]
        cmd += [name,"pg_dump","-U",user,"-d",db,"--schema-only","--no-owner","--no-privileges"]
        r=run(cmd, timeout=120)
        if r["rc"]==0 and r["out"].strip():
            # Remove comments/blank lines to reduce pg_dump timestamp/version noise.
            normalized="\n".join(line.rstrip() for line in r["out"].splitlines()
                                 if line.strip() and not line.startswith("--"))
            tables=sorted(set(re.findall(r'^CREATE TABLE\s+([^\s(]+)', normalized, flags=re.M)))
            return {"available":True,"container":name,"sha256":sha(normalized),"table_count":len(tables),"tables":tables}
        return {"available":False,"container":name,"error":"pg_dump schema capture failed"}
    return {"available":False,"container":None,"error":"postgres container not found"}

host=socket.gethostname()
di=run(["docker","info","--format","{{json .}}"], timeout=30)
if di["rc"]!=0:
    print(json.dumps({"role":ROLE,"host":host,"blocker":"docker info failed"}))
    sys.exit(0)

ps=run(["docker","ps","--format","{{.Names}}"], timeout=30)
names=[x.strip() for x in ps["out"].splitlines() if x.strip()]
containers=[]
for name in sorted(names):
    ir=run(["docker","inspect",name], timeout=30)
    if ir["rc"]!=0:
        continue
    try:
        i=json.loads(ir["out"])[0]
    except Exception:
        continue
    cfg=i.get("Config") or {}; st=i.get("State") or {}; hc=i.get("HostConfig") or {}
    img_id=i.get("Image") or ""
    image_name=cfg.get("Image") or ""
    img=run(["docker","image","inspect",img_id], timeout=30)
    labels={}
    repo_digests=[]
    if img["rc"]==0:
        try:
            ii=json.loads(img["out"])[0]
            labels=(ii.get("Config") or {}).get("Labels") or {}
            repo_digests=ii.get("RepoDigests") or []
        except Exception:
            pass
    health=((st.get("Health") or {}).get("Status") if st.get("Health") else "no-healthcheck")
    mounts=[]
    for m in i.get("Mounts") or []:
        mounts.append({"type":m.get("Type"),"destination":m.get("Destination"),"rw":m.get("RW")})
    nets=sorted(((i.get("NetworkSettings") or {}).get("Networks") or {}).keys())
    containers.append({
      "name":name,"image":image_name,"image_id":img_id,"repo_digests":sorted(repo_digests),
      "revision":labels.get("org.opencontainers.image.revision") or labels.get("org.opencontainers.image.source-revision") or "",
      "status":st.get("Status"),"health":health,"restart_count":i.get("RestartCount",0),
      "entrypoint":cfg.get("Entrypoint"),"cmd":cfg.get("Cmd"),"restart_policy":hc.get("RestartPolicy",{}).get("Name"),
      "networks":nets,"mounts":mounts,"env":env_map(cfg.get("Env") or []),
      "source":source_hash(name)
    })

result={
  "role":ROLE,"host":host,"captured_utc":__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(),
  "docker_root":json.loads(di["out"]).get("DockerRootDir") if di["out"].strip().startswith("{") else None,
  "git":git_repos(),"containers":containers,"db_schema":db_schema(containers)
}
print(json.dumps(result, sort_keys=True))
PY
  then
    fail "cannot capture ${role} via ssh host '${host}'. No changes were made."
  fi
  python3 - "$outfile" <<'PY'
import json,sys
p=sys.argv[1]
try:
    d=json.load(open(p))
except Exception as e:
    raise SystemExit(f"invalid capture {p}: {e}")
if d.get("blocker"):
    raise SystemExit(f"remote blocker: {d['blocker']}")
print(f"CAPTURE_{d.get('role')}=PASS host={d.get('host')} containers={len(d.get('containers',[]))} git_repos={len(d.get('git',[]))}")
PY
}

collect_remote "$PROD_HOST" PROD "$OUT/prod.json"
collect_remote "$DEV_HOST" DEV "$OUT/dev.json"

python3 - "$OUT/prod.json" "$OUT/dev.json" "$OUT/report.txt" <<'PY'
import json, os, re, sys
prod=json.load(open(sys.argv[1])); dev=json.load(open(sys.argv[2])); report_path=sys.argv[3]
rows=[]

def add(area,item,status,detail): rows.append((area,item,status,detail))

def repo_key(r):
    o=(r.get('origin') or '').rstrip('/').split('/')[-1]
    return o[:-4] if o.endswith('.git') else o or os.path.basename(r.get('path',''))

def norm(n):
    s=n.lower().strip('/')
    for p in ('df-v3-','df-','desifaces-v3-','desifaces-'):
        if s.startswith(p): s=s[len(p):]
    s=re.sub(r'-(prod|production|dev|development)$','',s)
    s=s.replace('_','-')
    return s

# Git provenance
pg={repo_key(r):r for r in prod.get('git',[])}; dg={repo_key(r):r for r in dev.get('git',[])}
for k in sorted(set(pg)|set(dg)):
    a,b=pg.get(k),dg.get(k)
    if not a or not b:
        add('GIT',k,'BLOCKER','repository not visible on both hosts')
    elif a.get('dirty'):
        add('GIT',k,'BLOCKER','PROD source working tree is dirty; runtime/source provenance needs explicit reconstruction')
    elif a.get('head')==b.get('head') and not b.get('dirty'):
        add('GIT',k,'MATCH',a.get('head','')[:12])
    elif b.get('dirty'):
        add('GIT',k,'BLOCKER',f"DEV working tree dirty; prod={a.get('head','')[:12]} dev={b.get('head','')[:12]}")
    else:
        add('GIT',k,'DRIFT REQUIRING CORRECTION',f"prod={a.get('head','')[:12]} dev={b.get('head','')[:12]}")

pc={norm(c['name']):c for c in prod.get('containers',[])}; dc={norm(c['name']):c for c in dev.get('containers',[])}
intentional_env=re.compile(r'(SECRET|TOKEN|PASSWORD|PASS$|KEY$|API_KEY|DATABASE|DB_|DSN|REDIS|HOST|URL|URI|DOMAIN|PORT|CALLBACK|OAUTH|STRIPE|APPLE|GOOGLE|AZURE|BLOB|STORAGE|SAS|CONNECTION|ENVIRONMENT|ENV$)',re.I)
critical_env=re.compile(r'(STITCH|SCENE|COORDINATOR|WORKER|QUEUE|MODEL|PROVIDER|GENERATION|FUSION|FACE|AUDIO|PRICING|CREDIT|CURRENCY|FEATURE|ENABLED|CONCURRENCY|BATCH|POLL|REVISION)',re.I)
for k in sorted(set(pc)|set(dc)):
    a,b=pc.get(k),dc.get(k)
    if not a or not b:
        add('RUNTIME',k,'BLOCKER',f"container only on {'PROD' if a else 'DEV'}")
        continue
    if a.get('status')!='running': add('RUNTIME',k,'BLOCKER',f"PROD status={a.get('status')}")
    elif b.get('status')!='running': add('RUNTIME',k,'DRIFT REQUIRING CORRECTION',f"DEV status={b.get('status')}")
    elif b.get('health') not in ('healthy','no-healthcheck'): add('RUNTIME',k,'DRIFT REQUIRING CORRECTION',f"DEV health={b.get('health')}")
    else: add('RUNTIME',k,'MATCH',f"running prod_health={a.get('health')} dev_health={b.get('health')}")

    sa,sb=(a.get('source') or {}),(b.get('source') or {})
    if sa.get('available') and sb.get('available'):
        if sa.get('sha256')==sb.get('sha256'):
            add('CODE',k,'MATCH',f"runtime source hash {sa.get('sha256','')[:12]}")
        else:
            add('CODE',k,'DRIFT REQUIRING CORRECTION',f"runtime source differs prod_files={sa.get('file_count')} dev_files={sb.get('file_count')}")
    elif sa.get('available') != sb.get('available'):
        add('CODE',k,'BLOCKER','runtime source fingerprint available on only one host')

    # Image provenance can differ only if runtime source and revision prove equivalence.
    same_image=(a.get('image_id')==b.get('image_id')) or bool(set(a.get('repo_digests',[])) & set(b.get('repo_digests',[])))
    same_rev=bool(a.get('revision')) and a.get('revision')==b.get('revision')
    same_src=sa.get('available') and sb.get('available') and sa.get('sha256')==sb.get('sha256')
    if same_image:
        add('IMAGE',k,'MATCH',(a.get('image_id') or '')[:19])
    elif same_rev and same_src:
        add('IMAGE',k,'INTENTIONAL DIFFERENCE',f"different image IDs but same revision={a.get('revision')[:12]} and runtime source")
    else:
        add('IMAGE',k,'DRIFT REQUIRING CORRECTION',f"prod_image={a.get('image')} dev_image={b.get('image')}")

    ea,eb=a.get('env',{}),b.get('env',{})
    for ekey in sorted(set(ea)|set(eb)):
        if ekey not in ea or ekey not in eb:
            st='INTENTIONAL DIFFERENCE' if intentional_env.search(ekey) else ('DRIFT REQUIRING CORRECTION' if critical_env.search(ekey) else 'INTENTIONAL DIFFERENCE')
            add('ENV',f"{k}:{ekey}",st,f"present only on {'PROD' if ekey in ea else 'DEV'}")
        elif ea[ekey].get('sha256')!=eb[ekey].get('sha256'):
            if intentional_env.search(ekey) and not critical_env.search(ekey): st='INTENTIONAL DIFFERENCE'
            elif critical_env.search(ekey): st='DRIFT REQUIRING CORRECTION'
            else: st='INTENTIONAL DIFFERENCE'
            add('ENV',f"{k}:{ekey}",st,'value hashes differ; values redacted')

# DB schema
pa,da=prod.get('db_schema',{}),dev.get('db_schema',{})
if pa.get('available') and da.get('available'):
    if pa.get('sha256')==da.get('sha256'):
        add('DB','schema','MATCH',f"hash={pa.get('sha256','')[:12]} tables={pa.get('table_count')}")
    else:
        add('DB','schema','DRIFT REQUIRING CORRECTION',f"prod_tables={pa.get('table_count')} dev_tables={da.get('table_count')}")
else:
    add('DB','schema','BLOCKER',f"prod={pa.get('error','unavailable')} dev={da.get('error','unavaile')}")

# Dedicated coordinator/stitch gate.
for key in sorted(set(pc)&set(dc)):
    if 'stitch' in key or 'fusion-extension' in key or 'director' in key or 'coordinator' in key:
        a,b=pc[key],dc[key]
        sa,sb=a.get('source',{}),b.get('source',{})
        if not (sa.get('available') and sb.get('available') and sa.get('sha256')==sb.get('sha256')):
            add('GENERATION-GATE',key,'BLOCKER','V3 coordinator/stitch/director runtime source not proven identical')
        else:
            add('GENERATION-GATE',key,'MATCH','runtime source fingerprint identical')

order={'BLOCKER':0,'DRIFT REQUIRING CORRECTION':1,'INTENTIONAL DIFFERENCE':2,'MATCH':3}
rows.sort(key=lambda r:(order.get(r[2],9),r[0],r[1]))
counts={s:sum(1 for r in rows if r[2]==s) for s in ['MATCH','INTENTIONAL DIFFERENCE','DRIFT REQUIRING CORRECTION','BLOCKER']}
lines=[]
lines.append('============================================================')
lines.append(' DESIFACES #next2 — PROD / DEV SYNCHRONIZATION AUDIT')
lines.append('============================================================')
lines.append(f"PROD_HOST={prod.get('host')}  DEV_HOST={dev.get('host')}")
for s in ['MATCH','INTENTIONAL DIFFERENCE','DRIFT REQUIRING CORRECTION','BLOCKER']:
    lines.append(f"{s}={counts[s]}")
lines.append('')
for area,item,status,detail in rows:
    if status!='MATCH':
        lines.append(f"[{status}] {area} :: {item} :: {detail}")
lines.append('')
if counts['BLOCKER']:
    verdict='BLOCKED — no synchronization mutation is safe yet'
elif counts['DRIFT REQUIRING CORRECTION']:
    verdict='READY_FOR_RECONCILIATION — drift identified; apply phase required'
else:
    verdict='CERTIFIED — no corrective drift found'
lines.append(f"VERDICT={verdict}")
lines.append('PROD_TOUCH=NONE')
lines.append('DEV_TOUCH=NONE')
text='\n'.join(lines)+'\n'
open(report_path,'w').write(text)
print(text,end='')
PY

tar -C "$OUT" -czf "$OUT/evidence.tgz" prod.json dev.json report.txt
sha256sum "$OUT/evidence.tgz" > "$OUT/evidence.tgz.sha256"
echo "EVIDENCE_DIR=$OUT"
echo "EVIDENCE_BUNDLE=$OUT/evidence.tgz"
echo "EVIDENCE_SHA256=$(awk '{print $1}' "$OUT/evidence.tgz.sha256")"
echo "NEXT2_AUDIT=COMPLETE"
