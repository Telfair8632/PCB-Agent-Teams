#!/usr/bin/env python3
"""Bulk-fetch datasheet PDFs for a list of MPNs in a single process.

Three input modes (mutually exclusive):
  --shortlist-dir DIR : read every <role>_shortlist.json under DIR, pull the
                        top-ranked candidate's MPN + pre-fetched datasheet_urls.
                        **Fastest path** — no DigiKey keyword search needed.
  --mpns MPN [MPN ...] : list MPNs as positional args. Falls back to DK keyword
                        search to discover datasheet URL.
  --mpns-file FILE    : JSON file with list of MPN strings or {mpns: [...]}.

Faster than calling fetch_datasheet_digikey.py once per MPN, because it:
  1. Reuses DigiKey OAuth token across all MPNs (one-time auth cost)
  2. Uses concurrent.futures for parallel HTTP fetches (workers=4)
  3. Honors DigiKey API throttle via the shared _dk_throttle module
  4. Validates Content-Type to detect HTML-redirect "PDFs" (Phoenix etc.)
  5. Skips files that already exist
  6. With --shortlist-dir, completely skips the DK keyword search step
     (component-selecting already saved datasheet_urls in the shortlist).

Usage:
  # Recommended: read shortlist results from component-selecting
  python3 bulk_fetch_datasheets.py \\
      --output-dir Projects/<name>/datasheets \\
      --shortlist-dir Projects/<name>/_artifacts/component_selecting

  # Fallback: direct MPN list (still uses DK API to discover datasheet URL)
  python3 bulk_fetch_datasheets.py \\
      --output-dir Projects/<name>/datasheets \\
      --mpns AMC1311BDWVR TMA-0505S 1715721

Environment: DIGIKEY_CLIENT_ID, DIGIKEY_CLIENT_SECRET
  (only required when discovering URLs via --mpns / --mpns-file)

Notes:
  - MPN with '/' or whitespace is sanitized to '_' for filename
  - URL fallback chain per MPN: shortlist datasheet_urls.digikey →
    .mouser → DK keyword search (only if the shortlist didn't carry URLs)
  - Content-Type validation: if response body doesn't have '%PDF' magic
    in the first 4KB, treats it as failure and tries next URL candidate
"""
from __future__ import annotations


# -- utf-8 guard: cp936 consoles/pipes cannot encode status glyphs --
import sys as _sys
for _s in (_sys.stdout, _sys.stderr):
    if _s is not None and hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass
import argparse
import concurrent.futures
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

try:
    import requests
except ImportError:
    requests = None  # type: ignore

# Reuse the cross-process DK throttle module from component-selecting-JP.
_THIS = Path(__file__).resolve()
_CS_SCRIPTS = _THIS.parents[2] / "component-selecting-JP" / "scripts"
sys.path.insert(0, str(_CS_SCRIPTS))
try:
    from _dk_throttle import throttled_urlopen as _dk_urlopen  # type: ignore
except ImportError:
    _dk_urlopen = urllib.request.urlopen  # graceful fallback

DK_TOKEN_URL = "https://api.digikey.com/v1/oauth2/token"
DK_KEYWORD_URL = "https://api.digikey.com/products/v4/search/keyword"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) Gecko/20100101 Firefox/128.0"
HTTP_TIMEOUT = 30
PDF_HEAD_BYTES = 4096


def _sanitize_filename(mpn: str) -> str:
    s = re.sub(r'[/\\:*?"<>|,;\s]', "_", mpn)
    return re.sub(r"_+", "_", s).strip("_")


def _read_shortlist_dir(shortlist_dir: Path) -> list[dict]:
    """Read every <role>_shortlist.json under shortlist_dir, extract the
    top-ranked candidate (verdict in {pass, warn_single_source}) per file,
    return [{mpn, datasheet_url_candidates: [...]}] preserving file order."""
    out: list[dict] = []
    seen_mpns: set[str] = set()
    for path in sorted(shortlist_dir.glob("*_shortlist.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"warn: cannot parse {path}: {exc}", file=sys.stderr)
            continue
        results = data.get("results") or []
        # Pick first pass/warn candidate
        chosen = next(
            (r for r in results if r.get("verdict") in ("pass", "warn_single_source")),
            None,
        )
        if chosen is None:
            continue
        mpn = chosen.get("mpn")
        if not mpn or mpn in seen_mpns:
            continue
        seen_mpns.add(mpn)
        ds_urls = chosen.get("datasheet_urls") or {}
        # Order: digikey first (typical PrimaryDatasheet from manufacturer),
        # then mouser as fallback. Drop empties.
        candidates = []
        for vendor in ("digikey_jp", "digikey_us", "digikey_de", "mouser_jp", "mouser_us", "mouser_de"):
            url = ds_urls.get(vendor)
            if url and url not in candidates:
                candidates.append(url)
        # Also accept any other vendor key (future-proofing)
        for vendor, url in ds_urls.items():
            if url and url not in candidates:
                candidates.append(url)
        out.append({
            "mpn": mpn,
            "datasheet_url_candidates": candidates,
            "source_file": path.name,
        })
    return out


def _get_dk_token() -> tuple[str | None, str | None]:
    cid = os.environ.get("DIGIKEY_CLIENT_ID", "").strip()
    csec = os.environ.get("DIGIKEY_CLIENT_SECRET", "").strip()
    if not (cid and csec):
        return None, "missing DIGIKEY_CLIENT_ID / DIGIKEY_CLIENT_SECRET"
    body = urllib.parse.urlencode({
        "client_id": cid,
        "client_secret": csec,
        "grant_type": "client_credentials",
    }).encode()
    req = urllib.request.Request(
        DK_TOKEN_URL, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read())
    except Exception as exc:
        return None, f"token fetch failed: {exc}"
    token = payload.get("access_token")
    if not token:
        return None, "no access_token in response"
    return token, None


def _dk_search_one(mpn: str, token: str, locale_jp: bool) -> dict:
    """Return {'datasheet_url': ..., 'product_url': ..., 'mpn_dk': ...} or {'error': ...}."""
    headers = {
        "Authorization": f"Bearer {token}",
        "X-DIGIKEY-Client-Id": os.environ["DIGIKEY_CLIENT_ID"],
        "Content-Type": "application/json",
    }
    if locale_jp:
        headers.update({
            "X-DIGIKEY-Locale-Site": "JP",
            "X-DIGIKEY-Locale-Language": "ja",
            "X-DIGIKEY-Locale-Currency": "JPY",
        })
    body = json.dumps({"Keywords": mpn, "Limit": 5, "Offset": 0}).encode()
    req = urllib.request.Request(DK_KEYWORD_URL, data=body, headers=headers, method="POST")
    try:
        with _dk_urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            data = json.loads(resp.read())
    except Exception as exc:
        return {"error": f"DK API failed: {exc}"}
    products = data.get("Products") or []
    if not products:
        return {"error": "no_products"}
    target = re.sub(r"[\s\-_]", "", mpn).upper()
    for p in products:
        mfg = re.sub(r"[\s\-_]", "", (p.get("ManufacturerProductNumber") or "")).upper()
        if mfg == target or target in mfg or mfg in target:
            return {
                "datasheet_url": p.get("DatasheetUrl"),
                "product_url": p.get("ProductUrl"),
                "mpn_dk": p.get("ManufacturerProductNumber"),
                "mfg": (p.get("Manufacturer") or {}).get("Name"),
            }
    # Fall back to first result if no exact match
    p = products[0]
    return {
        "datasheet_url": p.get("DatasheetUrl"),
        "product_url": p.get("ProductUrl"),
        "mpn_dk": p.get("ManufacturerProductNumber"),
        "mfg": (p.get("Manufacturer") or {}).get("Name"),
        "note": "fallback_first_result",
    }


def _is_pdf_content(content: bytes) -> bool:
    head = content[:PDF_HEAD_BYTES]
    if head.startswith(b"%PDF"):
        return True
    if b"%PDF" in head:  # some servers prepend BOM/whitespace
        return True
    if head.lstrip().startswith(b"<"):
        return False  # HTML
    return False


def _resolve_redirect_url(url: str) -> str:
    """Pre-process known JS-redirect URL shapes.

    TI distributors return URLs like
      https://www.ti.com/general/docs/suppproductinfo.tsp?distId=10&gotoUrl=<encoded>
    The HTML body just JS-navigates to gotoUrl; follow it directly.
    """
    parsed = urllib.parse.urlparse(url)
    if "ti.com" in parsed.netloc and "suppproductinfo" in parsed.path:
        qs = urllib.parse.parse_qs(parsed.query)
        goto = qs.get("gotoUrl") or qs.get("gotourl")
        if goto:
            return urllib.parse.unquote(goto[0])
    return url


def _download_pdf(url: str, output: Path) -> dict:
    """Download URL to output. Returns {'ok': True, 'bytes': N} or {'error': ...}."""
    if not url:
        return {"error": "empty_url"}
    # Normalize protocol-relative URL
    if url.startswith("//"):
        url = "https:" + url
    url = _resolve_redirect_url(url)
    headers = {"User-Agent": USER_AGENT, "Accept": "application/pdf,*/*;q=0.8"}
    try:
        if requests is not None:
            r = requests.get(url, headers=headers, timeout=HTTP_TIMEOUT, allow_redirects=True)
            r.raise_for_status()
            content = r.content
        else:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                content = resp.read()
    except Exception as exc:
        return {"error": f"http_fetch_failed: {exc}"}
    if not _is_pdf_content(content):
        return {"error": f"not_a_pdf (got {len(content)} bytes, head={content[:64]!r})"}
    output.write_bytes(content)
    return {"ok": True, "bytes": len(content)}


def _fetch_one(
    mpn: str,
    output_dir: Path,
    token: str | None,
    locale_jp: bool,
    force: bool,
    pre_urls: list[str] | None = None,
) -> dict:
    """Try every URL in pre_urls (in order). Only falls back to DK keyword
    search when pre_urls is empty AND a token is available."""
    safe = _sanitize_filename(mpn)
    output = output_dir / f"{safe}.pdf"
    if output.exists() and not force:
        return {"mpn": mpn, "status": "skip_exists", "path": str(output)}

    candidates = list(pre_urls or [])
    info: dict = {}
    if not candidates:
        if token is None:
            return {"mpn": mpn, "status": "search_failed", "reason": "no_url_and_no_dk_token"}
        info = _dk_search_one(mpn, token, locale_jp)
        if "error" in info:
            return {"mpn": mpn, "status": "search_failed", "reason": info["error"]}
        if info.get("datasheet_url"):
            candidates.append(info["datasheet_url"])

    last_err = None
    for url in candidates:
        result = _download_pdf(url, output)
        if result.get("ok"):
            return {
                "mpn": mpn,
                "mpn_dk": info.get("mpn_dk"),
                "mfg": info.get("mfg"),
                "status": "ok",
                "path": str(output),
                "bytes": result["bytes"],
                "datasheet_url": url,
                "candidates_tried": len(candidates),
                "product_url": info.get("product_url"),
                "source": "shortlist" if pre_urls else "dk_search",
            }
        last_err = result.get("error")

    return {
        "mpn": mpn,
        "mpn_dk": info.get("mpn_dk"),
        "status": "download_failed",
        "reason": last_err or "no_url_candidates",
        "candidates_tried": len(candidates),
        "datasheet_url_first_tried": candidates[0] if candidates else None,
        "product_url": info.get("product_url"),
        "source": "shortlist" if pre_urls else "dk_search",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Bulk-fetch datasheet PDFs for MPN list")
    parser.add_argument("--output-dir", required=True, type=Path)
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--shortlist-dir", type=Path, help="Read <role>_shortlist.json files; use pre-fetched datasheet_urls (no DK keyword search needed)")
    src.add_argument("--mpns", nargs="+", help="MPN list as positional args (falls back to DK keyword search)")
    src.add_argument("--mpns-file", type=Path, help="JSON file: list of MPN strings or {mpns: [...]}")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--locale", default="jp", choices=["jp", "us"], help="DK locale headers (only used when --mpns / --mpns-file)")
    parser.add_argument("--force", action="store_true", help="Re-download even if file exists")
    parser.add_argument("--json-output", type=Path, help="Write summary JSON to this path")
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)

    # Build the work list: each entry has {mpn, pre_urls}
    work: list[dict] = []
    if args.shortlist_dir:
        if not args.shortlist_dir.exists():
            print(f"error: shortlist-dir not found: {args.shortlist_dir}", file=sys.stderr)
            return 2
        shortlist_entries = _read_shortlist_dir(args.shortlist_dir)
        if not shortlist_entries:
            print(f"error: no shortlist JSONs with passing candidates under {args.shortlist_dir}", file=sys.stderr)
            return 2
        work = [{"mpn": e["mpn"], "pre_urls": e["datasheet_url_candidates"]} for e in shortlist_entries]
    elif args.mpns_file:
        data = json.loads(args.mpns_file.read_text(encoding="utf-8"))
        mpns = data.get("mpns") if isinstance(data, dict) else data
        work = [{"mpn": m, "pre_urls": []} for m in (mpns or [])]
    else:
        work = [{"mpn": m, "pre_urls": []} for m in args.mpns]

    if not work:
        print("error: no MPNs provided", file=sys.stderr)
        return 2

    # Token is only needed when we'll fall back to DK keyword search
    needs_token = any(not w["pre_urls"] for w in work)
    token: str | None = None
    if needs_token:
        token, err = _get_dk_token()
        if err:
            print(f"error: {err}", file=sys.stderr)
            return 2

    locale_jp = args.locale == "jp"
    mpns_order = [w["mpn"] for w in work]
    t0 = time.time()
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(args.workers, 8))) as ex:
        futures = {
            ex.submit(_fetch_one, w["mpn"], args.output_dir, token, locale_jp, args.force, w["pre_urls"]): w["mpn"]
            for w in work
        }
        for fut in concurrent.futures.as_completed(futures):
            mpn = futures[fut]
            try:
                results.append(fut.result())
            except Exception as exc:
                results.append({"mpn": mpn, "status": "exception", "reason": str(exc)})

    # Sort results by input MPN order for stable output
    results.sort(key=lambda r: mpns_order.index(r["mpn"]) if r["mpn"] in mpns_order else 999)
    mpns = mpns_order  # for summary line below

    elapsed = time.time() - t0
    counts = {"ok": 0, "skip_exists": 0, "search_failed": 0, "download_failed": 0, "exception": 0}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1

    # Human-readable summary
    print(f"\n=== bulk_fetch_datasheets — {len(mpns)} MPN in {elapsed:.1f}s ===")
    for r in results:
        sym = {"ok": "✓", "skip_exists": "·", "search_failed": "?", "download_failed": "✗", "exception": "!"}.get(r["status"], "?")
        msg = r.get("path") or r.get("reason") or ""
        print(f"  {sym} {r['mpn']:<28} {r['status']:<16} {msg}")
    print(f"summary: {counts}")

    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(
            json.dumps({"results": results, "counts": counts, "elapsed_sec": elapsed}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    return 0 if counts["search_failed"] == 0 and counts["download_failed"] == 0 and counts["exception"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
