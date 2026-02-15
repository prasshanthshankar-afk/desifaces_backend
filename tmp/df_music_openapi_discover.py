import json, re

p = "/tmp/svc-music-openapi.json"
doc = json.load(open(p, "r", encoding="utf-8"))
paths = doc.get("paths", {})

def score(path, method, op):
    s = 0
    if method.lower() == "post": s += 1
    if "project" in path: s += 2
    if "job" in path: s += 2
    if "status" in path: s += 3
    if "publish" in path: s += 2
    if op.get("requestBody"): s += 1
    return s

cands = []
for path, methods in paths.items():
    for method, op in methods.items():
        if method.lower() not in ("get","post","put","patch","delete"):
            continue
        cands.append((score(path, method, op), method.upper(), path, op.get("summary","")))

cands.sort(reverse=True)

print("\nTop endpoints (highest score first):")
for s, m, path, summary in cands[:40]:
    print(f"{s:02d}  {m:4s}  {path:60s}  {summary}")

print("\nLikely STATUS endpoints:")
for s, m, path, summary in cands:
    if m == "GET" and re.search(r"status", path, re.I):
        print(f"{m:4s}  {path:60s}  {summary}")

print("\nLikely CREATE PROJECT endpoints:")
for s, m, path, summary in cands:
    if m == "POST" and re.search(r"/projects\b", path):
        print(f"{m:4s}  {path:60s}  {summary}")

print("\nLikely CREATE JOB/START endpoints:")
for s, m, path, summary in cands:
    if m == "POST" and re.search(r"/jobs\b", path):
        print(f"{m:4s}  {path:60s}  {summary}")

print("\nLikely UPLOAD endpoints (audio/voice):")
for s, m, path, summary in cands:
    if m in ("POST","PUT","PATCH") and re.search(r"upload|voice|audio|reference", path, re.I):
        print(f"{m:4s}  {path:60s}  {summary}")