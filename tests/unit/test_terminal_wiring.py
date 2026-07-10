"""Terminal endpoints/mount are wired into build_app and token-exempt."""

from __future__ import annotations

from poseidon.terminal.routes import router


def test_router_paths_are_namespaced() -> None:
    paths = {r.path for r in router.routes}
    assert paths == {
        "/api/terminal/quote", "/api/terminal/chart", "/api/terminal/search",
        "/api/terminal/fundamentals", "/api/terminal/news", "/api/terminal/market",
    }


def test_server_source_wires_terminal() -> None:
    # The build_app factory needs a full kernel; assert wiring at source level
    # (cheap, dependency-free) — ui_verify covers the runtime path end-to-end.
    import pathlib

    src = pathlib.Path("src/poseidon/api/server.py").read_text(encoding="utf-8")
    assert "from ..terminal.routes import router as terminal_router" in src
    assert "app.include_router(terminal_router)" in src
    assert 'STATIC_DIR / "terminal"' in src
    assert "_token_exempt(request.url.path)" in src  # auth exemption via helper


def test_token_exemption_is_path_boundary_aware() -> None:
    from poseidon.api.server import _token_exempt

    for exempt in ("/terminal", "/terminal/", "/terminal/index.html",
                   "/api/terminal/quote", "/api/terminal/market", "/static/app.js"):
        assert _token_exempt(exempt), exempt
    for protected in ("/terminalfoo", "/api/terminalx", "/api/quote/AAPL",
                      "/api/portfolio", "/", "/ws"):
        assert not _token_exempt(protected), protected


def test_nav_has_terminal_entry() -> None:
    import pathlib

    html = pathlib.Path("src/poseidon/api/static/index.html").read_text(encoding="utf-8")
    assert 'href="/terminal/"' in html
