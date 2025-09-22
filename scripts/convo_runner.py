#!/usr/bin/env python3
import os, re, json, time, datetime, argparse, pathlib, sys
import requests
from typing import Dict, Any, List

DEFAULT_API = os.getenv("VAST_API_BASE", "http://localhost:5173")  # adjust if needed
PROCESS_PATH = os.getenv("VAST_API_PROCESS_PATH", "/api/conversations/process")
TIMEOUT = float(os.getenv("VAST_CONVO_TIMEOUT", "30"))

def load_yaml(path: str) -> Dict[str, Any]:
    try:
        import yaml  # PyYAML
    except ImportError:
        print("Please `pip install pyyaml` to use convo_runner.", file=sys.stderr)
        sys.exit(1)
    with open(path, "r") as f:
        return yaml.safe_load(f)

PAYLOAD_MODES = ("auto", "user_input", "prompt", "messages")


def _payload_shapes(prompt: str):
    return {
        "user_input": {"user_input": prompt},
        "prompt": {"prompt": prompt},
        "messages": {"messages": [{"role": "user", "content": prompt}]},
    }


def post_prompt(base: str, prompt: str, mode: str) -> Dict[str, Any]:
    url = base.rstrip("/") + PROCESS_PATH
    shapes = _payload_shapes(prompt)
    tried = []
    last_error = None

    def _send(payload_mode: str):
        payload = dict(shapes[payload_mode])
        payload.setdefault("conversation_id", "test")
        resp = requests.post(url, json=payload, timeout=TIMEOUT)
        body = None
        try:
            body = resp.json()
        except Exception:
            body = resp.text
        if resp.status_code >= 400:
            return {
                "error": True,
                "status": resp.status_code,
                "body": body,
                "payload_mode": payload_mode,
            }
        if isinstance(body, dict):
            body.setdefault("payload_mode", payload_mode)
            return body
        return {"content": body, "payload_mode": payload_mode}

    if mode == "auto":
        for name in ("user_input", "prompt", "messages"):
            tried.append(name)
            resp = _send(name)
            if not resp.get("error"):
                resp.setdefault("meta", {})
                resp["meta"]["payload_mode"] = name
                if len(tried) > 1:
                    resp["meta"]["auto_tried"] = tried
                print(f"[auto] Payload mode '{name}' succeeded")
                return resp
            last_error = resp
        # all failed, return last response details
        if last_error is None:
            last_error = {"error": True, "status": 0, "body": "no attempts", "payload_mode": None}
        last_error.setdefault("meta", {})
        last_error["meta"]["auto_tried"] = tried
        return last_error

    if mode not in shapes:
        raise ValueError(f"Unknown payload mode: {mode}")
    return _send(mode)

def extract_text(resp: Dict[str, Any]) -> str:
    # Try common shapes: {message: {content}}, {content}, raw_text, etc.
    if "content" in resp and isinstance(resp["content"], str):
        return resp["content"]
    msg = resp.get("message") or resp.get("assistant") or {}
    if isinstance(msg, dict) and isinstance(msg.get("content"), str):
        return msg["content"]
    if "raw_text" in resp:
        return resp["raw_text"]
    # last resort—stringify
    return json.dumps(resp, ensure_ascii=False)

def run_case(base: str, case: Dict[str, Any], default_mode: str) -> Dict[str, Any]:
    name = case["name"]
    prompt = case["prompt"]
    checks = case.get("checks", {})
    nots = case.get("must_not_contain", []) or []

    mode = case.get("payload_mode", default_mode)
    resp = post_prompt(base, prompt, mode)
    passed = True
    failures: List[str] = []

    if resp.get("error"):
        passed = False
        body = resp.get("body")
        if isinstance(body, dict):
            body_excerpt = json.dumps(body, ensure_ascii=False)[:300]
        else:
            body_excerpt = str(body)[:300]
        failures.append(f"HTTP {resp['status']}: {body_excerpt}")
        text = body_excerpt
    else:
        text = extract_text(resp)

    for s in checks.get("contains", []) or []:
        if s not in text:
            passed = False
            failures.append(f'missing substring: "{s}"')

    for pat in checks.get("regex", []) or []:
        if not re.search(pat, text, flags=re.IGNORECASE | re.MULTILINE):
            passed = False
            failures.append(f'regex not matched: /{pat}/')

    for s in nots:
        if s in text:
            passed = False
            failures.append(f'forbidden substring present: "{s}"')

    # Optional: peek into audit to ensure facts short-circuit (if API returns it)
    audit = resp.get("audit") or resp.get("last_actions") or []
    if "facts" in (case.get("labels") or []):
        if not any(isinstance(a, dict) and a.get("type") == "FACT" for a in audit):
            passed = False
            failures.append("expected FACT audit entry, none found")

    result = {
        "name": name,
        "prompt": prompt,
        "passed": passed,
        "failures": failures,
        "response_excerpt": text[:1200],
        "raw_response": resp,
    }
    return result

def write_report(results: List[Dict[str, Any]], outdir: str) -> str:
    pathlib.Path(outdir).mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    stem = f"convo_{ts.replace(':','').replace('-','')}"
    jpath = os.path.join(outdir, stem + ".json")
    mpath = os.path.join(outdir, stem + ".md")

    with open(jpath, "w") as jf:
        json.dump(results, jf, indent=2, ensure_ascii=False)

    passed = sum(1 for r in results if r["passed"])
    total = len(results)

    # Markdown summary
    lines = [
        f"# Conversation Suite — {ts}",
        "",
        f"**{passed}/{total} passed**",
        "",
        "| Case | Result | Notes |",
        "|------|--------|-------|",
    ]
    for r in results:
        note = "; ".join(r["failures"]) if r["failures"] else "—"
        lines.append(f"| {r['name']} | {'✅' if r['passed'] else '❌'} | {note} |")

    with open(mpath, "w") as mf:
        mf.write("\n".join(lines))

    return mpath

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--suite", default="convo_suite.yaml", help="Path to YAML suite")
    ap.add_argument("--base", default=DEFAULT_API, help="VAST API base URL")
    ap.add_argument("--outdir", default="reports/convo", help="Output dir for reports")
    ap.add_argument("--payload-mode", choices=PAYLOAD_MODES, default="auto", help="Payload shape to use")
    args = ap.parse_args()

    base_url = args.base.rstrip("/")
    full_url = base_url + PROCESS_PATH
    print(f"Using API endpoint: {full_url}")

    suite = load_yaml(args.suite)
    results = []
    for case in suite["cases"]:
        case.setdefault("payload_mode", args.payload_mode)
        try:
            results.append(run_case(args.base, case, args.payload_mode))
        except Exception as e:
            results.append({
                "name": case["name"],
                "prompt": case["prompt"],
                "passed": False,
                "failures": [f"exception: {e}"],
                "response_excerpt": "",
                "raw_response": {},
            })
    mpath = write_report(results, args.outdir)
    print(f"Report: {mpath}")
    # Non-zero exit on failure (so CI fails)
    if not all(r["passed"] for r in results):
        sys.exit(1)

if __name__ == "__main__":
    main()
