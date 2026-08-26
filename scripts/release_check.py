"""Post-deploy health gate for the bind-mounted production service."""

from __future__ import annotations

import argparse
import json
import urllib.request


def check(base_url: str) -> dict:
    result = {}
    for path in ("/", "/data-center", "/watch-pool", "/api/health/details"):
        request = urllib.request.Request(base_url.rstrip("/") + path)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                result[path] = response.status
        except Exception as exc:
            result[path] = str(exc)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("base_url", nargs="?", default="http://127.0.0.1:9000")
    args = parser.parse_args()
    result = check(args.base_url)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if all(value in (200, 302, 401) for value in result.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
