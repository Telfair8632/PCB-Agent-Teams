#!/usr/bin/env python3
"""多分销商查询：库存 + 价格 + lifecycle status。

DEPRECATED for component-preparing BOM gate decisions.

This script is kept only for ad-hoc diagnostics / historical audits. The
pre-design gate must consume component-selecting evidence instead, because
component-selecting owns USER.md locale routing and verified vendor checks.
Do not import query_all()/best_stock() from check_readiness.py.

此模块自包含，无运行时依赖。

支持：
  - DigiKey（需要 DIGIKEY_CLIENT_ID + DIGIKEY_CLIENT_SECRET 环境变量）
  - Mouser（需要 MOUSER_SEARCH_API_KEY）
  - element14（需要 ELEMENT14_API_KEY）
  - LCSC（无需 key，用 jlcsearch.tscircuit.com 公开 API）

返回统一格式：
    {
      "distributor": "lcsc",
      "in_stock": True/False,
      "stock_qty": int,
      "status": str,           # active / nrnd / obsolete 等
      "price_breaks": [{qty, price_usd}, ...],  # 仅 digikey/mouser
      "lead_time": str,
      "raw": {...}             # 调试用
    }

用法（模块）:
    from distributor_query import query_all
    rep = query_all("AMC1311DWV")
    # → {"lcsc": {...}, "digikey": {...}, "mouser": None, "element14": None}
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
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional


# ============================================================
# DigiKey
# ============================================================

def _get_digikey_token() -> Optional[tuple[str, str]]:
    """OAuth2 token，缓存在 /tmp。"""
    client_id = os.environ.get("DIGIKEY_CLIENT_ID", "")
    client_secret = os.environ.get("DIGIKEY_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        return None

    cache_path = os.path.join(tempfile.gettempdir(), "digikey_token_cache.json")
    try:
        with open(cache_path, encoding="utf-8") as f:
            cache = json.load(f)
        if cache.get("expires_at", 0) > time.time():
            return cache["access_token"], client_id
    except (OSError, json.JSONDecodeError, KeyError):
        pass

    try:
        data = urllib.parse.urlencode({
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "client_credentials",
        }).encode()
        req = urllib.request.Request(
            "https://api.digikey.com/v1/oauth2/token",
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            token_data = json.loads(resp.read())
        token = token_data["access_token"]
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump({"access_token": token, "expires_at": time.time() + 540}, f)
        return token, client_id
    except (urllib.error.URLError, OSError, json.JSONDecodeError, KeyError):
        return None


def query_digikey(mpn: str) -> Optional[dict]:
    """DigiKey 查 stock + 价格阶梯 + lifecycle。"""
    auth = _get_digikey_token()
    if not auth:
        return None
    token, client_id = auth

    try:
        body = json.dumps({"Keywords": mpn, "Limit": 3}).encode()
        req = urllib.request.Request(
            "https://api.digikey.com/products/v4/search/keyword",
            data=body,
            headers={
                "Authorization": f"Bearer {token}",
                "X-DIGIKEY-Client-Id": client_id,
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return None

    for product in data.get("Products", []):
        prod_mpn = product.get("ManufacturerProductNumber", "")
        if not prod_mpn.upper().startswith(mpn.upper()[:6]):
            continue

        result = {"distributor": "digikey"}

        # Status / discontinued
        status = product.get("ProductStatus", {})
        if isinstance(status, dict):
            result["status"] = status.get("Status")
        elif isinstance(status, str):
            result["status"] = status
        result["discontinued"] = product.get("Discontinued", False)

        # Stock — DigiKey API v4 在 ProductVariations 里
        for var in product.get("ProductVariations", []):
            qty = var.get("QuantityAvailableforPackageType")
            if qty is not None:
                result["stock_qty"] = qty
                result["in_stock"] = qty > 0
                break

        # 价格阶梯 — 在 StandardPricing
        breaks = []
        for var in product.get("ProductVariations", []):
            for sp in var.get("StandardPricing", []):
                breaks.append({
                    "qty": sp.get("BreakQuantity"),
                    "price_usd": sp.get("UnitPrice"),
                })
            if breaks:
                break
        if breaks:
            result["price_breaks"] = breaks

        return result
    return None


# ============================================================
# Mouser
# ============================================================

def query_mouser(mpn: str) -> Optional[dict]:
    api_key = os.environ.get("MOUSER_SEARCH_API_KEY") or os.environ.get("MOUSER_PART_API_KEY")
    if not api_key:
        return None

    try:
        body = json.dumps({
            "SearchByPartRequest": {
                "mouserPartNumber": mpn,
                "partSearchOptions": "",
            }
        }).encode()
        url = f"https://api.mouser.com/api/v1/search/partnumber?apiKey={api_key}"
        req = urllib.request.Request(url, data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return None

    for part in data.get("SearchResults", {}).get("Parts", []):
        result = {"distributor": "mouser"}
        result["status"] = part.get("LifecycleStatus")
        result["discontinued"] = str(part.get("IsDiscontinued", "")).lower() == "true"
        result["lead_time"] = part.get("LeadTime")
        result["suggested_replacement"] = part.get("SuggestedReplacement")

        # Stock
        try:
            stock_str = part.get("Availability", "0")
            stock_num = int("".join(c for c in stock_str if c.isdigit()) or "0")
            result["stock_qty"] = stock_num
            result["in_stock"] = stock_num > 0
        except (ValueError, TypeError):
            pass

        # 价格阶梯
        breaks = []
        for pb in part.get("PriceBreaks", []):
            try:
                price_str = pb.get("Price", "")
                price = float("".join(c for c in price_str if c.isdigit() or c == "."))
            except (ValueError, TypeError):
                price = None
            breaks.append({
                "qty": pb.get("Quantity"),
                "price": price,
                "currency": pb.get("Currency", "USD"),
            })
        if breaks:
            result["price_breaks"] = breaks

        return result
    return None


# ============================================================
# LCSC（jlcsearch.tscircuit.com — 无需 key）
# ============================================================

def query_lcsc(mpn: str) -> Optional[dict]:
    try:
        url = (f"https://jlcsearch.tscircuit.com/api/search?"
               f"q={urllib.parse.quote(mpn)}&limit=3&full=true")
        req = urllib.request.Request(url, headers={
            "User-Agent": "bom-readiness/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return None

    for comp in data.get("components", []):
        extra = comp.get("extra", {})
        comp_mpn = extra.get("mpn", "")
        if not comp_mpn or not comp_mpn.upper().startswith(mpn.upper()[:6]):
            continue

        result = {"distributor": "lcsc"}
        stock = comp.get("stock", 0)
        result["in_stock"] = stock > 0
        result["stock_qty"] = stock
        result["lcsc_part"] = comp.get("lcsc")
        result["price"] = comp.get("price")  # LCSC 通常单价 USD
        return result
    return None


# ============================================================
# element14
# ============================================================

def query_element14(mpn: str) -> Optional[dict]:
    api_key = os.environ.get("ELEMENT14_API_KEY")
    if not api_key:
        return None

    try:
        params = urllib.parse.urlencode({
            "callInfo.apiKey": api_key,
            "term": f"manuPartNum:{mpn}",
            "storeInfo.id": "us.newark.com",
            "resultsSettings.offset": 0,
            "resultsSettings.numberOfResults": 3,
            "resultsSettings.responseGroup": "medium",
        })
        url = f"https://api.element14.com/catalog/products?{params}"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return None

    products = data.get("manufacturerPartNumberSearchReturn", {}).get("products", [])
    for product in products:
        result = {"distributor": "element14"}
        for attr in product.get("attributes", []):
            label = attr.get("attributeLabel", "").lower()
            value = attr.get("attributeValue", "")
            if "lifecycle" in label or "status" in label:
                result["status"] = value
        if "inv" in product:
            try:
                stock = int(product["inv"])
                result["stock_qty"] = stock
                result["in_stock"] = stock > 0
            except (ValueError, TypeError):
                pass
        # 价格
        breaks = []
        for p in product.get("prices", []):
            breaks.append({
                "qty": p.get("from"),
                "price": p.get("cost"),
                "currency": "USD",
            })
        if breaks:
            result["price_breaks"] = breaks
        if len(result) > 1:  # 至少有一个字段
            return result
    return None


# ============================================================
# 综合查询
# ============================================================

def query_all(mpn: str) -> dict:
    """所有分销商并查（顺序，每个 ≤10 秒）。返回 dict。"""
    return {
        "lcsc": query_lcsc(mpn),
        "digikey": query_digikey(mpn),
        "mouser": query_mouser(mpn),
        "element14": query_element14(mpn),
    }


def best_stock(query_results: dict) -> Optional[dict]:
    """从多分销商结果挑库存 > 0 的（优先级 LCSC > DigiKey > Mouser > element14）。"""
    for d in ("lcsc", "digikey", "mouser", "element14"):
        r = query_results.get(d)
        if r and r.get("in_stock") and r.get("stock_qty", 0) > 0:
            return r
    return None


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("mpn")
    args = ap.parse_args()

    print("⚠ deprecated: diagnostics only; component-preparing's BOM gate must use component-selecting evidence")
    print(f"=== {args.mpn} 多分销商查询 ===")
    rep = query_all(args.mpn)
    for d, r in rep.items():
        if r is None:
            print(f"  {d}: ❌ 没数据 / API key 没配 / 网络问题")
        else:
            stock = r.get("stock_qty", "?")
            in_stock = "✅" if r.get("in_stock") else "❌"
            print(f"  {d}: {in_stock} stock={stock}, status={r.get('status', '?')}")
            if r.get("price_breaks"):
                pb = r["price_breaks"][0]
                print(f"      首阶梯：{pb.get('qty')}@${pb.get('price') or pb.get('price_usd', '?')}")

    best = best_stock(rep)
    if best:
        print(f"\n✅ 推荐：{best['distributor']}（stock={best.get('stock_qty')}）")
    else:
        print(f"\n⚠️  所有分销商都没现货")


if __name__ == "__main__":
    main()
