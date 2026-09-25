"""Small, bounded connectivity diagnostic; never logs account data or files."""
from __future__ import annotations
import asyncio
import json
import os
from urllib.parse import urlsplit
import aiohttp
from .network import new_session, limited_body, validate_url

async def inspect_source():
    result = {"event": "source_check", "login_verified": False, "download_tested": False}
    base = os.getenv("ZLIB_BASE_URL", "").strip().rstrip("/")
    try:
        origin = urlsplit(validate_url(base))
        if origin.path or origin.query or origin.fragment:
            raise ValueError("origin_required")
        async with new_session() as session:
            async def request(method, path, **kwargs):
                async with session.request(method, base + path, allow_redirects=False,
                    timeout=aiohttp.ClientTimeout(total=12), **kwargs) as response:
                    body = await limited_body(response, 2_000_000)
                    return response.status, response.headers.get("Content-Type", "").split(";")[0], body
            status, mime, body = await request("GET", "/")
            result["home_http"], result["home_type"] = status, mime
            if status != 200:
                return result
            uid, key = os.getenv("ZLIB_USER_ID", ""), os.getenv("ZLIB_USER_KEY", "")
            if not (uid and key):
                email, password = os.getenv("ZLIB_EMAIL", ""), os.getenv("ZLIB_PASSWORD", "")
                if not (email and password):
                    result["error"] = "credentials_missing"
                    return result
                status, mime, body = await request("POST", "/eapi/user/login", data={"email": email, "password": password})
                result["login_http"], result["login_type"] = status, mime
                if status != 200: return result
                data = json.loads(body)
                user = data.get("user", {})
                uid, key = str(user.get("id") or ""), str(user.get("remix_userkey") or "")
            if not uid.isdigit() or not key or any(c in key for c in "\r\n;"):
                result["error"] = "login_not_confirmed"
                return result
            result["login_verified"] = True
            headers = {"Cookie": f"remix_userid={uid}; remix_userkey={key}", "remix-userid": uid, "remix-userkey": key}
            status, mime, body = await request("GET", "/eapi/user/profile", headers=headers)
            result["profile_http"] = status
            if status == 200:
                user = json.loads(body).get("user", {})
                result["quota_fields_present"] = all(k in user for k in ("downloads_limit", "downloads_today"))
            if status in (401, 403, 429): return result
            status, mime, body = await request("GET", "/s/Dom%20Casmurro?page=1", headers=headers)
            result["search_html_http"], result["search_html_type"] = status, mime
            if status == 200:
                lower = body.lower()
                result["has_bookcard"] = b"<z-bookcard" in lower
                result["has_result_box"] = b"searchresultbox" in lower
                result["has_empty_marker"] = b"notfound" in lower
                result["captcha_detected"] = any(x in lower for x in (b"cf-chl-", b"verify you are human"))
    except (asyncio.TimeoutError, aiohttp.ClientError, OSError, ValueError, TypeError, AttributeError) as exc:
        result["error"] = type(exc).__name__
    except Exception as exc:
        result["error"] = type(exc).__name__
    return result

if __name__ == "__main__":
    print(json.dumps(asyncio.run(inspect_source()), ensure_ascii=False), flush=True)
