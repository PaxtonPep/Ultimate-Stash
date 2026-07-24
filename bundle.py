#!/usr/bin/env python3
"""
Bundle a webpack-style HTML+bundle.js+assets directory into a single self-contained .html.

Usage:
    bundle.py --source DIR [--out FILE]

Strategy:
  - Walk SOURCE_DIR, base64-encode every asset (audio/img/font/wasm/track/model/etc.)
    into a JSON virtual filesystem held in a <script type="application/json"> tag.
  - At boot, install fetch / XMLHttpRequest / importScripts overrides on the main
    thread that look up requests in the VFS and answer with Blob-backed Responses.
  - Inline the entry bundles (`*.bundle.js` excluding the worker) as <script> blocks
    placed at end-of-body to preserve the original `<script defer>` ordering.
  - Build the simulation worker as a Blob URL whose source is:
        VFS JSON literal + worker preamble (same overrides + Ammo Module hook)
        + worker bundle (with project-specific placeholder substitutions applied).
  - Patch window.Worker so any request for the worker bundle name boots the
    inlined worker instead of fetching it.

CURRENT SCOPE
-------------
This started life as a polytrack-specific tool and still hard-codes a few
polytrack-shaped assumptions:
  - The output HTML body is reconstructed from a fixed template (canvas + ui +
    transition-layer divs) — the source index.html is checked for existence but
    its body markup is not preserved.
  - The worker bundle is identified by name containing "worker".
  - `replacethisplease` placeholder substitution and the `vps.kodub.com` URL
    rewrite are baked into the runtime overrides.
When a second project arrives, expect to either lift these into a per-project
config (JSON next to the source) or refactor to read+patch the source index.html
in place. Keep both options on the table until we see what the next repo needs.
"""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import sys
from pathlib import Path

EXTRA_MIME = {
    ".ogg": "audio/ogg",
    ".woff2": "font/woff2",
    ".glb": "model/gltf-binary",
    ".track": "application/octet-stream",
    ".wasm": "application/wasm",
    ".svg": "image/svg+xml",
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".json": "application/json",
}

SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules", ".cache"}

# Assets the browser loads natively (CSS @font-face url(), <img src>, etc.) bypass
# our window.fetch override, so we additionally substitute their filename literals
# in the bundle text with data: URLs.
INLINE_DATA_URL_EXTS = {".woff2", ".woff", ".ttf", ".otf", ".png", ".jpg", ".jpeg", ".gif", ".svg"}


def mime_for(p: Path) -> str:
    ext = p.suffix.lower()
    if ext in EXTRA_MIME:
        return EXTRA_MIME[ext]
    m, _ = mimetypes.guess_type(str(p))
    return m or "application/octet-stream"


def collect_vfs(root: Path, skip_files: set[Path], bundle_names: set[str]) -> dict[str, dict]:
    vfs: dict[str, dict] = {}

    def walk(d: Path):
        for entry in sorted(d.iterdir()):
            if entry.is_dir():
                if entry.name in SKIP_DIRS:
                    continue
                walk(entry)
            elif entry.is_file():
                if entry.resolve() in skip_files:
                    continue
                if entry.name in bundle_names:
                    continue
                if entry.suffix.lower() == ".html":
                    continue
                rel = "/".join(entry.relative_to(root).parts)
                vfs[rel] = {
                    "mime": mime_for(entry),
                    "b64": base64.b64encode(entry.read_bytes()).decode("ascii"),
                }

    walk(root)
    return vfs


def inline_data_urls_in_bundles(bundles: dict[str, str], vfs: dict[str, dict]) -> int:
    """Replace quoted occurrences of CSS/img-style asset filenames in bundle text
    with data: URLs. Required because CSS @font-face url(...) and <img src> use
    the browser's native loader, bypassing window.fetch."""
    candidates: list[tuple[str, str]] = []
    for path, entry in vfs.items():
        ext = "." + path.rsplit(".", 1)[-1].lower() if "." in path else ""
        if ext not in INLINE_DATA_URL_EXTS:
            continue
        data_url = f"data:{entry['mime']};base64,{entry['b64']}"
        candidates.append((path, data_url))
        base = path.rsplit("/", 1)[-1]
        if base != path:
            candidates.append((base, data_url))
    # Longest-needle-first so "images/smoke.png" matches before "smoke.png"
    # and we don't double-replace.
    candidates.sort(key=lambda c: len(c[0]), reverse=True)

    replacements = 0
    for k in list(bundles.keys()):
        text = bundles[k]
        for needle, data_url in candidates:
            for quote in ('"', "'"):
                token = f"{quote}{needle}{quote}"
                if token in text:
                    new_token = f"{quote}{data_url}{quote}"
                    replacements += text.count(token)
                    text = text.replace(token, new_token)
        bundles[k] = text
    return replacements


# Runtime: installed in the main page. Reads window.__VFS__ and patches fetch / XHR.
MAIN_RUNTIME = r"""
(function () {
  const vfs = window.__VFS__;
  const blobCache = new Map();

  function pathFromUrl(url) {
    if (!url) return null;
    if (url.startsWith("data:")) return null;
    let path = url.split("?")[0].split("#")[0];
    try {
      const u = new URL(path, location.href);
      path = u.pathname;
    } catch (e) { /* keep raw */ }
    path = path.replace(/^\/+/, "");
    if (vfs[path]) return path;
    const parts = path.split("/");
    while (parts.length > 0) {
      const p = parts.join("/");
      if (vfs[p]) return p;
      parts.shift();
    }
    // Fall back: try basename match (covers Emscripten's blob: + filename concatenation).
    const base = path.split("/").pop();
    if (base) {
      for (const k of Object.keys(vfs)) {
        if (k === base || k.endsWith("/" + base)) return k;
      }
    }
    return null;
  }

  function blobFor(path) {
    let b = blobCache.get(path);
    if (b) return b;
    const entry = vfs[path];
    const bin = atob(entry.b64);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    b = new Blob([bytes], { type: entry.mime });
    blobCache.set(path, b);
    return b;
  }

  function blobUrlFor(path) {
    const k = path + "::url";
    let u = blobCache.get(k);
    if (u) return u;
    u = URL.createObjectURL(blobFor(path));
    blobCache.set(k, u);
    return u;
  }

  window.__vfsBlobUrl = blobUrlFor;
  window.__vfsBlob = blobFor;
  window.__vfsPathFromUrl = pathFromUrl;

  function urlOf(input) {
    if (typeof input === "string") return input;
    if (input instanceof URL) return input.href;
    if (input && typeof input.url === "string") return input.url;  // Request
    return "";
  }

  const ogfetch = window.fetch.bind(window);
  window.fetch = function (input, init) {
    let url = urlOf(input).replace("vps.kodub.com", "vpskodub.tmena1565.workers.dev");
    const path = pathFromUrl(url);
    if (path) {
      return Promise.resolve(new Response(blobFor(path), {
        status: 200,
        headers: { "Content-Type": vfs[path].mime }
      }));
    }
    if (typeof input === "string") return ogfetch(url, init);
    if (input instanceof URL) return ogfetch(url, init);
    if (input && input.url !== url) return ogfetch(new Request(url, input), init);
    return ogfetch(input, init);
  };

  const ogXhrOpen = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function (method, url, ...rest) {
    if (typeof url === "string") {
      url = url.replace("vps.kodub.com", "vpskodub.tmena1565.workers.dev");
      const path = pathFromUrl(url);
      if (path) url = blobUrlFor(path);
    }
    return ogXhrOpen.call(this, method, url, ...rest);
  };

  // Browser-internal loaders (`<img src>`, `<audio src>`, etc.) bypass fetch
  // entirely. Patch the `src`/`href` setters and setAttribute on the affected
  // element prototypes so dynamic assignments route through the VFS.
  function patchUrlAttr(proto, attr) {
    const desc = Object.getOwnPropertyDescriptor(proto, attr);
    if (!desc || !desc.set) return;
    Object.defineProperty(proto, attr, {
      set(value) {
        if (typeof value === "string") {
          const p = pathFromUrl(value);
          if (p) value = blobUrlFor(p);
        }
        desc.set.call(this, value);
      },
      get: desc.get,
      configurable: true,
    });
  }
  if (window.HTMLScriptElement) patchUrlAttr(HTMLScriptElement.prototype, "src");
  if (window.HTMLImageElement) patchUrlAttr(HTMLImageElement.prototype, "src");
  if (window.HTMLMediaElement) patchUrlAttr(HTMLMediaElement.prototype, "src");
  if (window.HTMLSourceElement) patchUrlAttr(HTMLSourceElement.prototype, "src");
  if (window.HTMLLinkElement) patchUrlAttr(HTMLLinkElement.prototype, "href");

  const URL_ATTRS = new Set(["src", "href", "xlink:href"]);
  const ogSetAttr = Element.prototype.setAttribute;
  Element.prototype.setAttribute = function (name, value) {
    if (URL_ATTRS.has(name) && typeof value === "string") {
      const p = pathFromUrl(value);
      if (p) value = blobUrlFor(p);
    }
    return ogSetAttr.call(this, name, value);
  };
})();
"""

# Worker preamble: injected at the top of the worker source. Mirrors MAIN_RUNTIME,
# plus an Emscripten Module.locateFile hook so Ammo's wasm filename stays bare and
# our fetch override catches it.
WORKER_PREAMBLE = r"""
self.Module = self.Module || {};
if (!self.Module.locateFile) self.Module.locateFile = function (s) { return s; };

(function () {
  const vfs = self.__VFS__;
  const blobCache = new Map();

  function pathFromUrl(url) {
    if (!url) return null;
    if (url.startsWith("data:")) return null;
    let path = url.split("?")[0].split("#")[0];
    try {
      const base = (self.location && self.location.href) || "http://__worker__/";
      path = new URL(path, base).pathname;
    } catch (e) { /* keep raw */ }
    path = path.replace(/^\/+/, "");
    if (vfs[path]) return path;
    const parts = path.split("/");
    while (parts.length > 0) {
      const p = parts.join("/");
      if (vfs[p]) return p;
      parts.shift();
    }
    const baseName = path.split("/").pop();
    if (baseName) {
      for (const k of Object.keys(vfs)) {
        if (k === baseName || k.endsWith("/" + baseName)) return k;
      }
    }
    return null;
  }

  function blobFor(path) {
    let b = blobCache.get(path);
    if (b) return b;
    const entry = vfs[path];
    const bin = atob(entry.b64);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    b = new Blob([bytes], { type: entry.mime });
    blobCache.set(path, b);
    return b;
  }

  function textFor(path) {
    const entry = vfs[path];
    const bin = atob(entry.b64);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    return new TextDecoder("utf-8").decode(bytes);
  }

  const ogfetch = self.fetch ? self.fetch.bind(self) : null;
  self.fetch = function (input, init) {
    let url = typeof input === "string" ? input : (input && input.url) || "";
    url = url.replace("vps.kodub.com", "vpskodub.tmena1565.workers.dev");
    const path = pathFromUrl(url);
    if (path) {
      return Promise.resolve(new Response(blobFor(path), {
        status: 200,
        headers: { "Content-Type": vfs[path].mime }
      }));
    }
    if (!ogfetch) return Promise.reject(new Error("fetch not available and no VFS match for " + url));
    if (typeof input === "string") return ogfetch(url, init);
    if (input && input.url !== url) return ogfetch(new Request(url, input), init);
    return ogfetch(input, init);
  };

  const ogImport = self.importScripts.bind(self);
  self.importScripts = function () {
    const remap = Array.prototype.map.call(arguments, function (u) {
      const p = pathFromUrl(u);
      if (p) {
        const text = textFor(p);
        return URL.createObjectURL(new Blob([text], { type: "application/javascript" }));
      }
      return u;
    });
    return ogImport.apply(self, remap);
  };

  if (self.XMLHttpRequest) {
    const ogXhrOpen = XMLHttpRequest.prototype.open;
    XMLHttpRequest.prototype.open = function (method, url) {
      const rest = Array.prototype.slice.call(arguments, 2);
      if (typeof url === "string") {
        url = url.replace("vps.kodub.com", "vpskodub.tmena1565.workers.dev");
        const p = pathFromUrl(url);
        if (p) {
          url = URL.createObjectURL(blobFor(p));
        }
      }
      return ogXhrOpen.call.apply(ogXhrOpen, [this, method, url].concat(rest));
    };
  }
})();
"""


def html_safe(s: str) -> str:
    """Escape sequences that would prematurely close a <script> tag."""
    return s.replace("</script", "<\\/script").replace("<!--", "<\\!--").replace("-->", "--\\>")


def discover_bundles(src_dir: Path) -> tuple[list[str], str | None]:
    """Return (entry_bundles, worker_bundle_or_None).

    Convention: anything matching `*.bundle.js`. The bundle whose name contains
    "worker" is treated as the worker; the others are entry bundles loaded in
    source-HTML order (preserved by sorting on filename).
    """
    bundles = sorted(p.name for p in src_dir.iterdir() if p.is_file() and p.name.endswith(".bundle.js"))
    worker = next((b for b in bundles if "worker" in b.lower()), None)
    entries = [b for b in bundles if b != worker]
    return entries, worker


def patch_publicpath(text: str) -> str:
    """Webpack auto-publicPath probes document.currentScript.src — empty for inline
    scripts, so its runtime throws. Set publicPath to "" so URLs flow through the
    fetch override and pre-substituted data URLs aren't prefixed with anything."""
    pp_throw = 'if (!e) throw new Error("Automatic publicPath is not supported in this browser");'
    pp_replacement = 'if (!e) e = "";'
    return text.replace(pp_throw, pp_replacement)


def build_html(
    src_dir: Path,
    out_path: Path,
    script_path: Path,
) -> str:
    if not (src_dir / "index.html").exists():
        sys.exit(f"index.html not found in {src_dir}")

    entry_bundles, worker_bundle = discover_bundles(src_dir)
    if not entry_bundles:
        sys.exit(f"no *.bundle.js files found in {src_dir}")

    bundle_text: dict[str, str] = {}
    for name in entry_bundles + ([worker_bundle] if worker_bundle else []):
        bundle_text[name] = (src_dir / name).read_text(encoding="utf-8")

    skip_files = {script_path.resolve(), out_path.resolve(), (src_dir / "index.html").resolve()}
    bundle_names = set(bundle_text.keys())
    vfs = collect_vfs(src_dir, skip_files, bundle_names)
    raw_total = sum(len(base64.b64decode(e["b64"])) for e in vfs.values())
    print(
        f"VFS: {len(vfs)} files, {raw_total/1e6:.1f} MB raw "
        f"({sum(len(e['b64']) for e in vfs.values())/1e6:.1f} MB base64)"
    )

    n_repl = inline_data_urls_in_bundles(bundle_text, vfs)
    print(f"Inlined {n_repl} CSS/img-style asset references as data: URLs")

    # Polytrack-specific placeholder substitution.
    if worker_bundle:
        bundle_text[worker_bundle] = bundle_text[worker_bundle].replace("replacethisplease", "")

    for k in list(bundle_text.keys()):
        bundle_text[k] = patch_publicpath(bundle_text[k])

    vfs_json = json.dumps(vfs, separators=(",", ":"))

    out: list[str] = []
    out.append("<!doctype html>\n<html>\n<head>\n")
    out.append('<meta charset="utf-8">\n')
    out.append(
        '<meta name="viewport" content="width=device-width,initial-scale=1,'
        'minimum-scale=1,maximum-scale=1,viewport-fit=cover,user-scalable=no">\n'
    )

    out.append('<script id="__vfs_json__" type="application/json">')
    out.append(html_safe(vfs_json))
    out.append("</script>\n")

    if worker_bundle:
        out.append('<script id="__worker_preamble__" type="text/plain">')
        out.append(html_safe(WORKER_PREAMBLE))
        out.append("</script>\n")
        out.append('<script id="__worker_src__" type="text/plain">')
        out.append(html_safe(bundle_text[worker_bundle]))
        out.append("</script>\n")

    boot = (
        "window.__VFS__ = JSON.parse(document.getElementById('__vfs_json__').textContent);\n"
        + MAIN_RUNTIME
    )
    if worker_bundle:
        boot += (
            "(function () {\n"
            "  const ogWorker = window.Worker;\n"
            "  const vfsJson = document.getElementById('__vfs_json__').textContent;\n"
            "  const preamble = document.getElementById('__worker_preamble__').textContent;\n"
            "  const workerBundle = document.getElementById('__worker_src__').textContent;\n"
            f"  const workerName = {json.dumps(worker_bundle.lower())};\n"
            "  let cachedUrl = null;\n"
            "  function workerUrl() {\n"
            "    if (cachedUrl) return cachedUrl;\n"
            "    const blob = new Blob([\n"
            '      "self.__VFS__ = ", vfsJson, ";\\n",\n'
            '      preamble, "\\n",\n'
            "      workerBundle\n"
            '    ], { type: "application/javascript" });\n'
            "    cachedUrl = URL.createObjectURL(blob);\n"
            "    return cachedUrl;\n"
            "  }\n"
            "  window.Worker = function (scriptUrl, options) {\n"
            '    if (typeof scriptUrl === "string"\n'
            "        && scriptUrl.toLowerCase().indexOf(workerName) !== -1) {\n"
            "      return new ogWorker(workerUrl(), options);\n"
            "    }\n"
            "    return new ogWorker(scriptUrl, options);\n"
            "  };\n"
            "  window.Worker.prototype = ogWorker.prototype;\n"
            "})();\n"
        )
    out.append("<script>")
    out.append(boot)
    out.append("</script>\n")

    # POLYTRACK-SPECIFIC body. When a non-polytrack project arrives, this is the
    # most likely place to lift into a per-project profile.
    out.append("</head>\n<body>")
    out.append('<canvas id="screen"></canvas>')
    out.append('<div id="ui"></div>')
    out.append('<div id="transition-layer"></div>')

    # Entry bundles inlined at end-of-body (matches `<script defer>` ordering).
    for name in entry_bundles:
        out.append("<script>\n")
        out.append(html_safe(bundle_text[name]))
        out.append("\n</script>\n")

    out.append("</body>\n</html>\n")
    return "".join(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--source", required=True, type=Path, help="source directory containing index.html and *.bundle.js")
    ap.add_argument("--out", type=Path, help="output HTML path (default: <source>/<source-name>.inlined.html)")
    args = ap.parse_args()

    src_dir = args.source.resolve()
    out_path = (args.out or (src_dir / f"{src_dir.name}.inlined.html")).resolve()
    script_path = Path(__file__).resolve()

    out_html = build_html(src_dir, out_path, script_path)
    out_path.write_text(out_html, encoding="utf-8")
    print(f"Wrote {out_path} ({len(out_html)/1e6:.2f} MB)")


if __name__ == "__main__":
    main()
