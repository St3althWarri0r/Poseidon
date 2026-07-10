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
    assert '"/terminal", "/api/terminal"' in src  # auth exemption tuple


def test_nav_has_terminal_entry() -> None:
    import pathlib

    html = pathlib.Path("src/poseidon/api/static/index.html").read_text(encoding="utf-8")
    assert 'href="/terminal/"' in html
