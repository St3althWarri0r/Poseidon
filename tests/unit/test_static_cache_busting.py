"""The dashboard's asset cache-busting stamp.

``index.html`` carries ``?v=__V__`` on every script and stylesheet so a browser
can never pair a new backend with cached old assets. That was stamped with the
RELEASE VERSION, which fails for the most common case there is: a
dashboard-only change ships without a version bump, the URL is byte-identical,
and the browser serves the stale file.

That is not hypothetical — it happened on the Settings view. The app window
(a Chromium profile under ~/.local/share/poseidon/webview-profile) kept serving
the pre-Settings ``app.js`` while a browser with a cold cache showed the new UI,
so the feature looked broken in the app and fine everywhere else.

The stamp is therefore derived from the CONTENT of the assets: edit any served
JS or CSS and the URL changes, version bump or not.
"""

from __future__ import annotations

from pathlib import Path

from poseidon import __version__
from poseidon.api import server as server_module


def _stamp() -> str:
    server_module.asset_stamp.cache_clear()
    return server_module.asset_stamp()


def test_stamp_is_stable_across_calls() -> None:
    server_module.asset_stamp.cache_clear()
    assert server_module.asset_stamp() == server_module.asset_stamp()


def test_stamp_still_carries_the_version_for_human_legibility() -> None:
    # Reading "2.16.0-a1b2c3…" in devtools should still tell you which release
    # you are looking at.
    assert _stamp().startswith(f"{__version__}-")


def test_editing_a_served_asset_changes_the_stamp(tmp_path: Path,
                                                  monkeypatch) -> None:
    # THE regression. A dashboard-only change must bust the cache even though
    # the release version is untouched.
    fake_static = tmp_path / "static"
    fake_static.mkdir()
    (fake_static / "index.html").write_text("<html>__V__</html>", encoding="utf-8")
    (fake_static / "app.js").write_text("console.log(1);", encoding="utf-8")
    monkeypatch.setattr(server_module, "STATIC_DIR", fake_static)

    before = _stamp()
    (fake_static / "app.js").write_text("console.log(2);", encoding="utf-8")
    after = _stamp()
    assert before != after, "editing app.js must change the cache-busting stamp"


def test_adding_a_new_asset_changes_the_stamp(tmp_path: Path, monkeypatch) -> None:
    # The Settings view ADDED settings_view.js; that alone must bust the cache.
    fake_static = tmp_path / "static"
    fake_static.mkdir()
    (fake_static / "app.js").write_text("console.log(1);", encoding="utf-8")
    monkeypatch.setattr(server_module, "STATIC_DIR", fake_static)

    before = _stamp()
    (fake_static / "settings_view.js").write_text("void 0;", encoding="utf-8")
    assert _stamp() != before


def test_stamp_ignores_files_the_page_does_not_load(tmp_path: Path,
                                                    monkeypatch) -> None:
    # Only JS/CSS are versioned in the markup. Churning an unrelated file
    # should not invalidate every client's cache for nothing.
    fake_static = tmp_path / "static"
    fake_static.mkdir()
    (fake_static / "app.js").write_text("console.log(1);", encoding="utf-8")
    monkeypatch.setattr(server_module, "STATIC_DIR", fake_static)

    before = _stamp()
    (fake_static / "notes.txt").write_text("scratch", encoding="utf-8")
    assert _stamp() == before


def test_the_real_index_html_has_no_unreplaced_placeholder() -> None:
    # A missed __V__ would reach the browser as a literal URL and 404.
    html = (server_module.STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert "__V__" in html, "the template must still carry the placeholder"
    rendered = html.replace("__V__", _stamp())
    assert "__V__" not in rendered
