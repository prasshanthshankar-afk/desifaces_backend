#!/usr/bin/env bash
set -Eeuo pipefail

[[ "$(hostname -s)" == "desifaces-dev" ]] || {
  echo "FAIL: DEV host required"
  exit 1
}

WEB="df-web-dev"
NETWORK="df-v3-net"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="/tmp/next3-dns-contract-${STAMP}.txt"

{
  echo "============================================================"
  echo " NEXT3 — WEB SERVICE DISCOVERY CONTRACT"
  echo "============================================================"
  echo "timestamp=$STAMP"

  echo
  echo "===== WEB SERVICE HOSTS ====="
  timeout 10s docker exec -i "$WEB" node - <<'NODE'
const keys = [
  "CORE_BASE_URL",
  "DIRECTOR_BASE_URL",
  "FACE_BASE_URL",
  "AUDIO_BASE_URL",
  "FUSION_BASE_URL",
  "FUSION_EXTENSION_BASE_URL",
  "PRICING_BASE_URL",
  "COMMERCE_BASE_URL",
  "DASHBOARD_BASE_URL",
  "ASSISTANT_BASE_URL",
  "NOTIFICATION_BASE_URL",
];

for (const key of keys) {
  const raw = process.env[key] || "";
  if (!raw) {
    console.log(key + "=<missing>");
    continue;
  }
  try {
    const u = new URL(raw);
    console.log(key + "=" + u.hostname + ":" + (u.port || (u.protocol === "https:" ? "443" : "80")));
  } catch {
    console.log(key + "=<invalid-url>");
  }
}
NODE

  echo
  echo "===== RUNNING TARGET CONTAINERS ====="
  docker ps --format '{{.Names}}'     | grep -E '^df-(v3-)?svc-(core|director|face|audio|fusion|fusion-extension|pricing|commerce|dashboard)$'     | sort || true

  echo
  echo "===== NETWORK ENDPOINTS / ALIASES ====="
  timeout 10s docker network inspect "$NETWORK"     --format '{{json .Containers}}'     | python3 -c '
import json,sys
obj=json.load(sys.stdin)
wanted=("core","director","face","audio","fusion","pricing","commerce","dashboard","web")
for _,v in sorted(obj.items(), key=lambda kv: str(kv[1].get("Name",""))):
    name=str(v.get("Name",""))
    if any(x in name for x in wanted):
        aliases=v.get("Aliases") or []
        print("name={} ipv4={} aliases={}".format(name, v.get("IPv4Address",""), aliases))
' || true

  echo
  echo "===== WEB DNS LOOKUPS ====="
  timeout 15s docker exec -i "$WEB" node - <<'NODE'
const dns = require("node:dns").promises;
const keys = ["DIRECTOR_BASE_URL","CORE_BASE_URL","FACE_BASE_URL","AUDIO_BASE_URL","FUSION_BASE_URL","PRICING_BASE_URL","COMMERCE_BASE_URL","DASHBOARD_BASE_URL"];

(async () => {
  for (const key of keys) {
    const raw=process.env[key]||"";
    if (!raw) continue;
    let host;
    try { host=new URL(raw).hostname; } catch { continue; }
    const attempts=[];
    for (let i=0;i<3;i++) {
      try {
        const r=await Promise.race([
          dns.lookup(host),
          new Promise((_,rej)=>setTimeout(()=>rej(new Error("TIMEOUT")),1500)),
        ]);
        attempts.push("OK:"+r.address);
      } catch (e) {
        attempts.push("FAIL:"+(e.code||e.message));
      }
    }
    console.log(key+" host="+host+" "+attempts.join(" | "));
  }
})().catch(e=>{ console.error(e); process.exit(2); });
NODE

  echo
  echo "===== WEB HTTP HEALTH VIA CONFIGURED HOSTS ====="
  timeout 20s docker exec -i "$WEB" node - <<'NODE'
const keys = ["DIRECTOR_BASE_URL","CORE_BASE_URL","FACE_BASE_URL","AUDIO_BASE_URL","FUSION_BASE_URL","PRICING_BASE_URL","COMMERCE_BASE_URL","DASHBOARD_BASE_URL"];

(async () => {
  for (const key of keys) {
    const raw=(process.env[key]||"").replace(/\/+$/,"");
    if (!raw) continue;
    try {
      const r=await Promise.race([
        fetch(raw+"/api/health"),
        new Promise((_,rej)=>setTimeout(()=>rej(new Error("TIMEOUT")),2500)),
      ]);
      console.log(key+" HTTP_"+r.status);
    } catch(e) {
      console.log(key+" FAIL_"+(e.cause?.code||e.code||e.message));
    }
  }
})().catch(e=>{console.error(e);process.exit(2)});
NODE

  echo
  echo "============================================================"
  echo " NEXT3_DNS_CONTRACT_CAPTURE=PASS"
  echo "============================================================"
} > "$OUT" 2>&1

echo "TRACE_FILE=$OUT"
echo "----- RESULT -----"
tail -n 120 "$OUT"
