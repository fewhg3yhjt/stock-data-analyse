from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_shared_ui_utils_has_global_safe_json_fallback():
    source = (ROOT / "web/static/ui-utils.js").read_text(encoding="utf-8")
    assert "Response.prototype.json" in source
    assert "接口返回空响应" in source
    assert "接口返回非 JSON 响应" in source
    base = (ROOT / "web/templates/base.html").read_text(encoding="utf-8")
    assert "ui-utils.js" in base


def test_new_shared_fetch_helper_does_not_expose_native_parse_error():
    source = (ROOT / "web/static/ui-utils.js").read_text(encoding="utf-8")
    assert "async fetchJson" in source
    assert "Unexpected end of JSON input" not in source
