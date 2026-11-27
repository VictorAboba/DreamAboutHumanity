"""The watch page takes the spectator key from the link's fragment (/watch/<id>#key=...), keeps it in sessionStorage,
removes the fragment from the address bar at once, and sends the key only as the Bearer header."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

WATCH = Path(__file__).parent.parent / "src" / "dah" / "server" / "watch.html"
KEY = "dahw_g7_secretFRAGkey"


def script() -> str:
    bodies = re.findall(r"<script>(.*?)</script>", WATCH.read_text(encoding="utf-8"), re.S)
    assert len(bodies) == 1
    return bodies[0]


def test_fragment_handling_is_in_the_page():
    js = script()
    assert "location.hash" in js and 'get("key")' in js
    assert "history.replaceState" in js and "location.pathname + location.search" in js
    assert "sessionStorage.setItem" in js and "hashchange" in js
    assert "S.key = takeFragmentKey() || loadKey();" in js
    assert 'id="keyform"' in WATCH.read_text(encoding="utf-8")  # the form stays as the fallback


def test_key_never_goes_into_a_url():
    js = script()
    # every request goes through get(): the fetch URL is built from the game id and the path only
    assert js.count("fetch(") == 1
    assert 'fetch(api + path, {headers: {"Authorization": "Bearer " + S.key}' in js
    # the one location.replace is the fallback that clears the fragment: path and query only, never the key
    assert js.count("location.replace(") == 1
    assert 'location.replace(location.pathname + location.search + "#")' in js
    for leak in ("location.href", "location.assign", "XMLHttpRequest", "sendBeacon",
                 "WebSocket", "?key=", "&key=", "window.open", "console.log(S.key", "console.warn(S.key"):
        assert leak not in js
    assert 'location.hash = ""' not in js     # assigning the hash would push a history entry and keep #key= in it


# ---------------------------------------------------------------- run the page script under node with a stub DOM

HARNESS = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
const hash = process.argv[3];
const out = {fetches: [], replaced: [], stored: {}, hrefs: [], locReplaced: [], hashSets: 0};
const throwing = process.argv[5] === "throw";
function El() {
  return {hidden: false, textContent: "", value: "", className: "", style: {}, children: [], firstChild: null,
          addEventListener() {}, removeEventListener() {}, focus() {}, setAttribute() {}, getAttribute() { return null; },
          appendChild(c) { return c; }, removeChild() {}, classList: {add() {}, remove() {}, toggle() {}},
          querySelectorAll() { return []; }};
}
const els = {};
let curHash = hash;
const location = {pathname: "/watch/g7", search: "", protocol: "http:", host: "127.0.0.1:8850",
                  get hash() { return curHash; }, set hash(v) { out.hashSets += 1; curHash = v; },
                  replace(url) { out.locReplaced.push(url); curHash = ""; }};
const listeners = {};
global.window = {addEventListener(n, f) { listeners[n] = f; }};
global.location = location;
global.history = {state: null, replaceState(s, t, url) {
  if (throwing) throw new Error("SecurityError");
  out.replaced.push(url); curHash = ""; }};
global.sessionStorage = {
  getItem(k) { return k in out.stored ? out.stored[k] : null; },
  setItem(k, v) { out.stored[k] = String(v); }, removeItem(k) { delete out.stored[k]; }};
global.document = {getElementById(id) { return els[id] || (els[id] = El()); }, createElement: El,
                   createElementNS: El, createTextNode: El, querySelectorAll() { return []; }};
global.fetch = function (url, opts) { out.fetches.push({url: url, opts: opts}); return new Promise(function () {}); };
global.setInterval = function () { return 1; }; global.clearInterval = function () {};
global.console = {warn() {}, log() {}, error() {}};
new Function(src)();
out.dialogHidden = els.keydlg ? els.keydlg.hidden : null;
if (process.argv[4] && listeners.hashchange) {
  curHash = process.argv[4]; out.fetches = []; out.replaced = [];
  listeners.hashchange();
}
out.finalHash = location.hash;
process.stdout.write(JSON.stringify(out));
"""


def run_page(tmp_path: Path, hash_: str, later: str = "", throw: bool = False) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    page = tmp_path / "page.js"
    page.write_text(script(), encoding="utf-8")
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS, encoding="utf-8")
    args = [node, str(harness), str(page), hash_, later] + (["throw"] if throw else [])
    done = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_fragment_key_is_stored_hidden_and_sent_only_as_bearer(tmp_path):
    out = run_page(tmp_path, "#key=" + KEY)
    assert out["replaced"] == ["/watch/g7"] and out["finalHash"] == ""
    assert out["stored"] == {"dah_watch_key:g7": KEY}
    assert out["fetches"], "the page should start loading with the key"
    for f in out["fetches"]:
        assert KEY not in f["url"] and f["url"].startswith("/v1/spectate/g7/")
        assert f["opts"]["headers"] == {"Authorization": "Bearer " + KEY}
        assert f["opts"]["credentials"] == "omit"


def test_encoded_fragment_key_is_decoded(tmp_path):
    out = run_page(tmp_path, "#key=a%2Bb%20c")
    assert out["stored"] == {"dah_watch_key:g7": "a+b c"}
    assert out["fetches"][0]["opts"]["headers"]["Authorization"] == "Bearer a+b c"


def test_no_fragment_falls_back_to_the_form(tmp_path):
    out = run_page(tmp_path, "")
    assert out["fetches"] == [] and out["replaced"] == [] and out["stored"] == {}
    assert out["dialogHidden"] is False


def test_empty_fragment_key_is_removed_and_asks_for_the_key(tmp_path):
    out = run_page(tmp_path, "#key=")
    assert out["replaced"] == ["/watch/g7"] and out["fetches"] == [] and out["stored"] == {}


def test_other_fragment_is_left_alone(tmp_path):
    out = run_page(tmp_path, "#turn=3")
    assert out["replaced"] == [] and out["finalHash"] == "#turn=3"


def test_key_pasted_later_into_the_fragment_is_taken(tmp_path):
    out = run_page(tmp_path, "", "#key=" + KEY)
    assert out["replaced"] == ["/watch/g7"] and out["stored"] == {"dah_watch_key:g7": KEY}
    assert out["fetches"] and all(KEY not in f["url"] for f in out["fetches"])


def test_fallback_when_replace_state_throws_replaces_the_entry_instead_of_pushing_one(tmp_path):
    """Review finding: assigning location.hash pushes a history entry, so Back would show #key= again; the fallback
    replaces the current entry with one whose fragment is empty."""
    out = run_page(tmp_path, "#key=" + KEY, throw=True)
    assert out["replaced"] == [] and out["locReplaced"] == ["/watch/g7#"]
    assert out["hashSets"] == 0 and out["finalHash"] == ""
    assert out["stored"] == {"dah_watch_key:g7": KEY}
    assert all(KEY not in f["url"] for f in out["fetches"])
