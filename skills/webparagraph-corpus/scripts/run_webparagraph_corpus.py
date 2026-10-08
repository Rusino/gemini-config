#!/usr/bin/env python3
"""Run the 2-part Flutter WebParagraph test corpus across one or more refs.

Part 1 (`engine/src/flutter/lib/web_ui`):
  - `dart analyze --fatal-infos`
  - `test/webparagraph/*_test.dart` (suite `chrome-dart2js-webparagraph-ui`)
  - `test/ui/{paragraph_builder,paragraph_style,text,text_style}_test.dart`
    (or all `test/ui/*_test.dart` with `--full`)
    (temporary suite `chrome-dart2js-webparagraph-ui-text`)

Part 2 (`packages/flutter`):
  - Rebuilds `ninja -C engine/src/out/wasm_release flutter/web_sdk`
  - Runs `flutter test --local-web-sdk=wasm_release --platform=chrome`
    with `preferWebParagraph: true` and `--enable-experimental-web-platform-features`:
      * Default (fast): 3 paragraph/TextPainter test files
      * `--full`: all 916 web unit test files in `packages/flutter/test`
        sharded in parallel with multi-browser concurrency and JSON reporting
"""

import argparse
import hashlib
import json
import os
import pathlib
import random
import re
import shutil
import signal
import subprocess
import sys
import time

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
PARSER_SCRIPT = SCRIPT_DIR / "parse_corpus_logs.py"

UI_TEXT_TESTS = [
    "test/ui/paragraph_builder_test.dart",
    "test/ui/paragraph_style_test.dart",
    "test/ui/text_test.dart",
    "test/ui/text_style_test.dart",
]

FRAMEWORK_TEXT_TESTS = [
    "test/rendering/paragraph_intrinsics_test.dart",
    "test/rendering/paragraph_test.dart",
    "test/painting/text_painter_test.dart",
]

# Known non-compilable / disabled web test files from dev/bots/suite_runners/run_web_tests.dart
KNOWN_WEB_FAILURES = {
    "test/services/message_codecs_vm_test.dart",
    "test/examples/sector_layout_test.dart",
    "test/material/text_field_test.dart",
    "test/widgets/performance_overlay_test.dart",
    "test/widgets/html_element_view_test.dart",
    "test/cupertino/scaffold_test.dart",
    "test/rendering/platform_view_test.dart",
}

FELT_SUITE_ANCHOR = """  - name: chrome-dart2js-webparagraph-ui
    test-bundle: dart2js-canvaskit-webparagraph
    run-config: chrome-webparagraph
    artifact-deps: [ canvaskit_webparagraph ]"""

FELT_TEMP_SUITE = """  - name: chrome-dart2js-webparagraph-ui
    test-bundle: dart2js-canvaskit-webparagraph
    run-config: chrome-webparagraph
    artifact-deps: [ canvaskit_webparagraph ]

  - name: chrome-dart2js-webparagraph-ui-text
    test-bundle: dart2js-canvaskit-ui
    run-config: chrome-webparagraph
    artifact-deps: [ canvaskit_webparagraph ]"""


def log(msg, driver_log=None):
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    line = f"{ts} {msg}"
    print(line, flush=True)
    if driver_log:
        with open(driver_log, "a") as f:
            f.write(line + "\n")


def find_ninja(repo):
    candidates = [
        repo / "engine/src/flutter/third_party/ninja/ninja",
        pathlib.Path.home() / ".gemini/jetski/scratch/depot_tools/ninja",
        shutil.which("ninja"),
    ]
    for c in candidates:
        if c and pathlib.Path(c).exists():
            return str(c)
    raise RuntimeError("Could not find `ninja` binary")


def build_env(repo):
    env = os.environ.copy()
    depot_tools = pathlib.Path.home() / ".gemini/jetski/scratch/depot_tools"
    dart_bin = repo / "engine/src/flutter/prebuilts/linux-x64/dart-sdk/bin"
    felt_bin = repo / "engine/src/flutter/lib/web_ui/dev"
    extra_paths = [str(p) for p in (depot_tools, dart_bin, felt_bin) if p.exists()]
    env["PATH"] = ":".join(extra_paths + [env.get("PATH", "")])

    stamp = repo / "bin/cache/engine-dart-sdk.stamp"
    if stamp.exists():
        env.setdefault("FLUTTER_PREBUILT_ENGINE_VERSION", stamp.read_text().strip())
    return env


def ensure_felt_temp_suite(felt_config_path):
    text = felt_config_path.read_text()
    if "chrome-dart2js-webparagraph-ui-text:" in text or "name: chrome-dart2js-webparagraph-ui-text" in text:
        return
    if FELT_SUITE_ANCHOR not in text:
        raise RuntimeError(f"Could not find anchor in {felt_config_path}")
    felt_config_path.write_text(text.replace(FELT_SUITE_ANCHOR, FELT_TEMP_SUITE, 1))


def patch_flutter_tools_for_web_corpus(repo):
    fwp_path = repo / "packages/flutter_tools/lib/src/test/flutter_web_platform.dart"
    cmd_test_path = repo / "packages/flutter_tools/lib/src/commands/test.dart"
    chrome_path = repo / "packages/flutter_tools/lib/src/web/chrome.dart"
    wtc_path = repo / "packages/flutter_tools/lib/src/test/web_test_compiler.dart"
    tc_path = repo / "packages/flutter_tools/lib/src/test/test_compiler.dart"

    # 1. flutter_web_platform.dart
    text = fwp_path.read_text()
    if "preferWebParagraph:" not in text:
        anchor1 = "    final File canvasKitFile = _canvasKitFile(relativePath);"
        repl1 = (
            "    final File canvasKitFile = _canvasKitFile(relativePath);\n"
            "    _logger.printStatus('[CanvasKit served]: $relativePath');"
        )
        anchor2 = '        canvasKitBaseUrl: "/canvaskit/",'
        repl2 = (
            '        canvasKitBaseUrl: "/canvaskit/",\n'
            "        preferWebParagraph: true,"
        )
        anchor3 = "        '--window-size=800,600',"
        repl3 = (
            "        '--window-size=800,600',\n"
            "        '--enable-experimental-web-platform-features',"
        )
        anchor_cache1 = """    if (request.url.path.endsWith('.dart.lib.js')) {
      final String path = request.url.path;
      return shelf.Response.ok(
        webMemoryFS.files[path],
        headers: <String, String>{HttpHeaders.contentTypeHeader: 'text/javascript'},
      );
    }"""
        repl_cache1 = """    if (request.url.path.endsWith('.dart.lib.js')) {
      final String path = request.url.path;
      return shelf.Response.ok(
        webMemoryFS.files[path],
        headers: <String, String>{
          HttpHeaders.contentTypeHeader: 'text/javascript',
          HttpHeaders.cacheControlHeader: 'public, max-age=3600',
        },
      );
    }"""
        anchor_cache2 = """    } else if (request.requestedUri.path.contains('dart_sdk.js')) {
      return shelf.Response.ok(
        _dartSdk.openRead(),
        headers: <String, String>{'Content-Type': 'text/javascript'},
      );"""
        repl_cache2 = """    } else if (request.requestedUri.path.contains('dart_sdk.js')) {
      return shelf.Response.ok(
        _dartSdk.openRead(),
        headers: <String, String>{
          'Content-Type': 'text/javascript',
          'Cache-Control': 'public, max-age=3600',
        },
      );"""
        anchor_pool = """  final _suiteLock = Pool(1);

  BrowserManager? _browserManager;"""
        repl_pool = """  final _suiteLock = Pool(16);
  final _launchLock = Pool(1);
  final _goldenLock = Pool(1);
  final _idleBrowsers = <BrowserManager>[];
  final _allBrowsers = <BrowserManager>[];"""

        anchor_golden = """  Future<shelf.Response> _goldenFileHandler(shelf.Request request) async {
    if (request.url.path.contains('flutter_goldens')) {
      final body = json.decode(await request.readAsString()) as Map<String, Object?>;
      final Uri goldenKey = Uri.parse(body['key']! as String);
      final Uri testUri = Uri.parse(body['testUri']! as String);
      Uint8List bytes;

      if (body.containsKey('bytes')) {
        bytes = base64.decode(body['bytes']! as String);
      } else {
        return shelf.Response.ok('Request must contain bytes in the body.');
      }
      if (updateGoldens) {
        return switch (await _testGoldenComparator.update(testUri, bytes, goldenKey)) {
          TestGoldenUpdateDone() => shelf.Response.ok('true'),
          TestGoldenUpdateError(error: final String error) => shelf.Response.ok(error),
        };
      } else {
        return switch (await _testGoldenComparator.compare(testUri, bytes, goldenKey)) {
          TestGoldenComparisonDone(matched: final bool matched) => shelf.Response.ok('$matched'),
          TestGoldenComparisonError(error: final String error) => shelf.Response.ok(error),
        };
      }
    } else {
      return shelf.Response.notFound('Not Found');
    }
  }"""
        repl_golden = """  Future<shelf.Response> _goldenFileHandler(shelf.Request request) async => _goldenLock.withResource(() async {
    if (request.url.path.contains('flutter_goldens')) {
      final body = json.decode(await request.readAsString()) as Map<String, Object?>;
      final Uri goldenKey = Uri.parse(body['key']! as String);
      final Uri testUri = Uri.parse(body['testUri']! as String);
      Uint8List bytes;

      if (body.containsKey('bytes')) {
        bytes = base64.decode(body['bytes']! as String);
      } else {
        return shelf.Response.ok('Request must contain bytes in the body.');
      }
      if (updateGoldens) {
        return switch (await _testGoldenComparator.update(testUri, bytes, goldenKey)) {
          TestGoldenUpdateDone() => shelf.Response.ok('true'),
          TestGoldenUpdateError(error: final String error) => shelf.Response.ok(error),
        };
      } else {
        return switch (await _testGoldenComparator.compare(testUri, bytes, goldenKey)) {
          TestGoldenComparisonDone(matched: final bool matched) => shelf.Response.ok('$matched'),
          TestGoldenComparisonError(error: final String error) => shelf.Response.ok(error),
        };
      }
    } else {
      return shelf.Response.notFound('Not Found');
    }
  });"""

        anchor_load = """    final PoolResource lockResource = await _suiteLock.request();

    final Runtime browser = platform.runtime;
    try {
      _browserManager ??= await _launchBrowser(browser);
    } on Error catch (_) {
      await _suiteLock.close();
      rethrow;
    }

    if (_closed) {
      throw StateError('Load called on a closed FlutterWebPlatform');
    }

    if (_logger.isVerbose) {
      _logger.printTrace('Running test suite $relativePath.');
    }

    final RunnerSuite suite = await _browserManager!.load(
      relativePath,
      suiteUrl,
      suiteConfig,
      message,
      onDone: () async {
        lockResource.release();
        if (_logger.isVerbose) {
          _logger.printTrace('Test suite $relativePath finished.');
        }
      },
    );"""
        repl_load = """    final PoolResource lockResource = await _suiteLock.request();

    final Runtime browser = platform.runtime;
    final BrowserManager browserManager;
    try {
      BrowserManager? candidate;
      while (_idleBrowsers.isNotEmpty) {
        final BrowserManager b = _idleBrowsers.removeLast();
        if (!b._closed) {
          candidate = b;
          break;
        }
      }
      if (candidate != null) {
        browserManager = candidate;
      } else {
        browserManager = await _launchLock.withResource(() async {
          while (_idleBrowsers.isNotEmpty) {
            final BrowserManager b = _idleBrowsers.removeLast();
            if (!b._closed) {
              return b;
            }
          }
          final BrowserManager b = await _launchBrowser(browser);
          _allBrowsers.add(b);
          return b;
        });
      }
    } on Error catch (_) {
      lockResource.release();
      rethrow;
    }

    if (_closed) {
      lockResource.release();
      throw StateError('Load called on a closed FlutterWebPlatform');
    }

    if (_logger.isVerbose) {
      _logger.printTrace('Running test suite $relativePath.');
    }

    var released = false;
    void releaseBrowser() {
      if (!released) {
        released = true;
        if (!_closed && !browserManager._closed) {
          _idleBrowsers.add(browserManager);
        }
        lockResource.release();
      }
    }

    final RunnerSuite suite;
    try {
      suite = await browserManager.load(
        relativePath,
        suiteUrl,
        suiteConfig,
        message,
        onDone: () async {
          releaseBrowser();
          if (_logger.isVerbose) {
            _logger.printTrace('Test suite $relativePath finished.');
          }
        },
      );
    } catch (_) {
      releaseBrowser();
      rethrow;
    }"""

        anchor_close = """  Future<BrowserManager> _launchBrowser(Runtime browser) {
    if (_browserManager != null) {
      throw StateError('Another browser is currently running.');
    }"""
        repl_close = """  Future<BrowserManager> _launchBrowser(Runtime browser) {"""

        anchor_close2 = """  @override
  Future<void> closeEphemeral() async {
    if (_browserManager != null) {
      await _browserManager!.close();
    }
  }

  @override
  Future<void> close() => _closeMemo.runOnce(() async {
    await Future.wait<void>(<Future<dynamic>>[
      ?_browserManager?.close(),
      _server.close(),
      _testGoldenComparator.close(),
    ]);
  });"""
        repl_close2 = """  @override
  Future<void> closeEphemeral() async {
    await Future.wait<void>(<Future<dynamic>>[
      for (final BrowserManager b in _allBrowsers) b.close(),
    ]);
  }

  @override
  Future<void> close() => _closeMemo.runOnce(() async {
    await Future.wait<void>(<Future<dynamic>>[
      for (final BrowserManager b in _allBrowsers) b.close(),
      _server.close(),
      _testGoldenComparator.close(),
    ]);
  });"""

        for name, a in (
            ("canvasKitFile", anchor1),
            ("canvasKitBaseUrl", anchor2),
            ("window-size", anchor3),
            ("cache1", anchor_cache1),
            ("cache2", anchor_cache2),
            ("pool", anchor_pool),
            ("golden", anchor_golden),
            ("load", anchor_load),
            ("close", anchor_close),
            ("close2", anchor_close2),
        ):
            if a not in text:
                raise RuntimeError(f"Could not find `{name}` anchor in {fwp_path}")

        text = (
            text.replace(anchor1, repl1, 1)
            .replace(anchor2, repl2, 1)
            .replace(anchor3, repl3, 1)
            .replace(anchor_cache1, repl_cache1, 1)
            .replace(anchor_cache2, repl_cache2, 1)
            .replace(anchor_pool, repl_pool, 1)
            .replace(anchor_golden, repl_golden, 1)
            .replace(anchor_load, repl_load, 1)
            .replace(anchor_close, repl_close, 1)
            .replace(anchor_close2, repl_close2, 1)
        )
        fwp_path.write_text(text)

    # 2. commands/test.dart
    cmd_text = cmd_test_path.read_text()
    cmd_anchor = """    if (_isIntegrationTest || isWeb) {
      if (argResults!.wasParsed('concurrency')) {"""
    cmd_repl = """    if (_isIntegrationTest) {
      if (argResults!.wasParsed('concurrency')) {"""
    if cmd_anchor in cmd_text:
        cmd_test_path.write_text(cmd_text.replace(cmd_anchor, cmd_repl, 1))

    # 3. web/chrome.dart
    ch_text = chrome_path.read_text()
    ch_anchor1 = """    if (currentCompleter.isCompleted) {
      throwToolExit('Only one instance of chrome can be started.');
    }"""
    ch_anchor2 = "    currentCompleter.complete(chrome);"
    ch_repl2 = "    if (!currentCompleter.isCompleted) {\n      currentCompleter.complete(chrome);\n    }"
    if ch_anchor1 in ch_text:
        ch_text = ch_text.replace(ch_anchor1, "", 1)
    if ch_anchor2 in ch_text and "if (!currentCompleter.isCompleted)" not in ch_text:
        ch_text = ch_text.replace(ch_anchor2, ch_repl2, 1)
    chrome_path.write_text(ch_text)

    # 4. test/web_test_compiler.dart
    wtc_text = wtc_path.read_text()
    wtc_anchor = """    if (output == null || output.errorCount > 0) {
      throwToolExit('Failed to compile');
    }
    // Cache the output kernel file to speed up subsequent compiles.
    fs.file(cachedKernelPath).parent.createSync(recursive: true);
    fs.file(output.outputFilename).copySync(cachedKernelPath);"""
    wtc_repl = """    if (output == null || output.errorCount > 0) {
      throwToolExit('Failed to compile');
    }
    // Cache the output kernel file to speed up subsequent compiles.
    if (!fs.file(cachedKernelPath).existsSync()) {
      fs.file(cachedKernelPath).parent.createSync(recursive: true);
      final File tempKernel = fs.file('$cachedKernelPath.${DateTime.now().microsecondsSinceEpoch}.tmp');
      fs.file(output.outputFilename).copySync(tempKernel.path);
      try {
        tempKernel.renameSync(cachedKernelPath);
      } on FileSystemException {
        if (tempKernel.existsSync()) {
          tempKernel.deleteSync();
        }
      }
    }"""
    if wtc_anchor in wtc_text:
        wtc_path.write_text(wtc_text.replace(wtc_anchor, wtc_repl, 1))

    # 5. test/test_compiler.dart
    tc_text = tc_path.read_text()
    tc_anchor = """          if (firstCompile ||
              !testCache.existsSync() ||
              (testCache.lengthSync() < outputFile.lengthSync())) {
            // The idea is to keep the cache file up-to-date and include as
            // much as possible in an effort to re-use as many packages as
            // possible.
            if (!testCache.parent.existsSync()) {
              testCache.parent.createSync(recursive: true);
            }
            await outputFile.copy(testFilePath);
          }"""
    tc_repl = """          if (!testCache.existsSync()) {
            if (!testCache.parent.existsSync()) {
              testCache.parent.createSync(recursive: true);
            }
            final File tempCache = fs.file('$testFilePath.${DateTime.now().microsecondsSinceEpoch}.tmp');
            await outputFile.copy(tempCache.path);
            try {
              await tempCache.rename(testFilePath);
            } on FileSystemException {
              if (tempCache.existsSync()) {
                tempCache.deleteSync();
              }
            }
          }"""
    if tc_anchor in tc_text:
        tc_path.write_text(tc_text.replace(tc_anchor, tc_repl, 1))


def ensure_force_test_fonts(repo):
    web_ui = repo / "engine/src/flutter/lib/web_ui"
    para_path = web_ui / "lib/src/engine/web_paragraph/paragraph.dart"
    dom_path = web_ui / "lib/src/engine/dom.dart"
    fc_path = web_ui / "lib/src/engine/web_paragraph/font_collection.dart"

    para_text = para_path.read_text()
    if "forceTestFonts" in para_text:
        return False

    dom_text = dom_path.read_text()
    dom_anchor = "  external DomFontFaceSet? add(DomFontFace font);"
    dom_repl = (
        "  external DomFontFaceSet? add(DomFontFace font);\n"
        "  @JS('ready')\n"
        "  external JSPromise<JSAny?> get _ready;\n"
        "  Future<void> get ready => _ready.toDart;"
    )
    if "Future<void> get ready" not in dom_text:
        if dom_anchor not in dom_text:
            raise RuntimeError(f"Could not find DomFontFaceSet.add anchor in {dom_path}")
        dom_path.write_text(dom_text.replace(dom_anchor, dom_repl, 1))

    fc_text = fc_path.read_text()
    fc_anchor = """      final DomFontFace fontFace = createDomFontFace(family, list);
      if (fontFace.status == 'error') {
        // Font failed to load.
        return false;
      }
      domDocument.fonts!.add(fontFace);

      // There might be paragraph measurements for this new font before it is
      // loaded. They were measured using fallback fonts, so we should clear the
      // cache."""
    fc_repl = """      final DomFontFace fontFace = createDomFontFace(family, list);
      await fontFace.load();
      if (fontFace.status == 'error') {
        // Font failed to load.
        return false;
      }
      domDocument.fonts!.add(fontFace);
      await domDocument.fonts!.ready;

      // There might be paragraph measurements for this new font before it is
      // loaded. They were measured using fallback fonts, so we should clear the
      // cache.
      invalidateLayoutContextFont();"""
    if "invalidateLayoutContextFont()" not in fc_text:
        if fc_anchor not in fc_text:
            raise RuntimeError(f"Could not find WebFontCollection anchor in {fc_path}")
        fc_path.write_text(fc_text.replace(fc_anchor, fc_repl, 1))

    p_anchor1 = "import 'package:ui/ui.dart' as ui;"
    p_repl1 = (
        "import 'package:ui/ui.dart' as ui;\n"
        "import 'package:ui/ui_web/src/ui_web.dart' as ui_web;\n\n"
        "const List<String> _webParagraphTestFonts = <String>['FlutterTest', 'Ahem'];"
    )
    p_anchor2 = "    createDomCanvasElement(width: 0, height: 0).context2D;"
    p_repl2 = (
        "    createDomCanvasElement(width: 0, height: 0).context2D;\n\n"
        "void invalidateLayoutContextFont() {\n"
        "  layoutContext.font = '';\n"
        "}"
    )
    p_anchor3 = """  String _buildCssFontString() {
    final String cssFontStyle = fontStyle?.toCssString() ?? StyleManager.defaultFontStyle;
    final String cssFontWeight = fontWeight?.toCssString() ?? StyleManager.defaultFontWeight;
    final double cssFontSize = fontSize ?? StyleManager.defaultFontSize;
    final String cssFontFamily = fontFamily ?? StyleManager.defaultFontFamily;
    final String fullFontName = canonicalizeFontFamily(cssFontFamily, fontFamilyFallback)!;
    return '$cssFontStyle $cssFontWeight ${cssFontSize.toStringAsFixed(2)}px $fullFontName';
  }"""
    p_repl3 = """  String _buildCssFontFamily() {
    if (ui_web.TestEnvironment.instance.forceTestFonts) {
      final String? family = fontFamily;
      final String testFont = _webParagraphTestFonts.contains(family)
          ? family!
          : _webParagraphTestFonts.first;
      return '"$testFont"';
    }
    final String cssFontFamily = fontFamily ?? StyleManager.defaultFontFamily;
    return canonicalizeFontFamily(cssFontFamily, fontFamilyFallback)!;
  }

  String _buildCssFontString() {
    final String cssFontStyle = fontStyle?.toCssString() ?? StyleManager.defaultFontStyle;
    final String cssFontWeight = fontWeight?.toCssString() ?? StyleManager.defaultFontWeight;
    final double cssFontSize = fontSize ?? StyleManager.defaultFontSize;
    final String fullFontName = _buildCssFontFamily();
    return '$cssFontStyle $cssFontWeight ${cssFontSize.toStringAsFixed(2)}px $fullFontName';
  }"""
    for name, a in (("import ui", p_anchor1), ("layoutContext", p_anchor2), ("_buildCssFontString", p_anchor3)):
        if a not in para_text:
            raise RuntimeError(f"Could not find `{name}` anchor in {para_path}")
    para_text = para_text.replace(p_anchor1, p_repl1, 1).replace(p_anchor2, p_repl2, 1).replace(p_anchor3, p_repl3, 1)
    para_path.write_text(para_text)
    return True


def checkout_web_ui_ref(repo, ref, worktree_patch_path):
    subprocess.run(
        ["git", "-C", str(repo), "checkout", "--", "engine/src/flutter/lib/web_ui"],
        check=True,
    )
    if ref == "WORKTREE":
        subprocess.run(
            ["git", "-C", str(repo), "checkout", "--no-overlay", "HEAD", "--", "engine/src/flutter/lib/web_ui"],
            check=True,
        )
        if worktree_patch_path and worktree_patch_path.exists() and worktree_patch_path.stat().st_size > 0:
            subprocess.run(
                ["git", "-C", str(repo), "apply", str(worktree_patch_path)],
                check=True,
            )
    else:
        subprocess.run(
            ["git", "-C", str(repo), "checkout", "--no-overlay", ref, "--", "engine/src/flutter/lib/web_ui"],
            check=True,
        )


def file_sha256_short(path):
    p = pathlib.Path(path)
    if not p.exists():
        return None
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()[:12]


def compute_web_ui_attestation(repo, ref):
    web_ui = repo / "engine/src/flutter/lib/web_ui"
    commit_target = "HEAD" if ref == "WORKTREE" else ref
    git_sha = (
        subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--short=12", commit_target],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    )
    git_subject = (
        subprocess.run(
            ["git", "-C", str(repo), "log", "-1", "--format=%s", commit_target],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    )
    dirty_files = []
    if ref == "WORKTREE":
        diff_names = subprocess.run(
            ["git", "-C", str(repo), "diff", "--name-only", "HEAD", "--", "engine/src/flutter/lib/web_ui"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
        dirty_files = [ln.strip() for ln in diff_names if ln.strip()]

    # Hash actual disk contents of web_ui/{lib,test} before temporary harness patches are injected
    h = hashlib.sha256()
    source_files = 0
    for subdir in ("lib", "test"):
        root = web_ui / subdir
        if not root.exists():
            continue
        for p in sorted(root.rglob("*")):
            if p.is_file() and p.suffix in (".dart", ".yaml"):
                rel = str(p.relative_to(web_ui))
                h.update(rel.encode("utf-8") + b"\0")
                h.update(p.read_bytes())
                h.update(b"\0")
                source_files += 1

    return {
        "ref": ref,
        "git_sha": git_sha,
        "git_subject": git_subject,
        "dirty_files": dirty_files,
        "web_ui_tree_sha256": h.hexdigest()[:12],
        "web_ui_source_files": source_files,
    }


def count_chrome_eligible_tests(repo, test_files):
    fw_root = repo / "packages/flutter"
    eligible = 0
    for rel in test_files:
        p = fw_root / rel
        if not p.exists():
            continue
        head = p.read_text(errors="replace")[:1500]
        if "@TestOn('!chrome')" not in head and '@TestOn("!chrome")' not in head:
            eligible += 1
    return eligible


def parse_ninja_actions(ninja_log_path):
    if not ninja_log_path.exists():
        return None
    text = ninja_log_path.read_text(errors="replace")
    if "no work to do" in text:
        return "0/0 (no-op)"
    steps = re.findall(r"\[(\d+)/(\d+)\]", text)
    if steps:
        last_done, total = steps[-1]
        return f"{last_done}/{total}"
    return "ran"


def collect_all_framework_web_tests(repo, custom_dirs=None):
    fw_root = repo / "packages/flutter"
    if custom_dirs:
        files = []
        for d in custom_dirs:
            target = fw_root / d
            if target.is_file():
                files.append(str(target.relative_to(fw_root)))
            elif target.is_dir():
                for p in sorted(target.rglob("*_test.dart")):
                    rel = str(p.relative_to(fw_root))
                    if rel not in KNOWN_WEB_FAILURES:
                        files.append(rel)
        return files

    test_dir = fw_root / "test"
    all_tests = sorted(
        str(p.relative_to(fw_root))
        for p in test_dir.rglob("*_test.dart")
        if p.parent != test_dir and str(p.relative_to(fw_root)) not in KNOWN_WEB_FAILURES
    )
    rng = random.Random(0)
    rng.shuffle(all_tests)
    return all_tests


def run_sharded_framework_tests(repo, env, ref_dir, label, test_files, num_shards, concurrency, timeout, driver_log):
    fw_root = repo / "packages/flutter"
    build_dir = fw_root / "build"
    if build_dir.exists():
        for dill in build_dir.glob("*cache.dill*"):
            try:
                dill.unlink()
            except OSError:
                pass

    shards_dir = ref_dir / "flutter_shards"
    if shards_dir.exists():
        shutil.rmtree(shards_dir)
    shards_dir.mkdir(parents=True, exist_ok=True)

    # 1. Warmup compile & asset build using a single fast file
    t_warm = time.time()
    warmup_log = shards_dir / "warmup.log"
    warmup_cmd = [
        str(repo / "bin/flutter"),
        "test",
        "--local-web-sdk=wasm_release",
        "--platform=chrome",
        "-j",
        "3",
        "test/rendering/paragraph_intrinsics_test.dart",
        "test/material/menu_button_theme_test.dart",
        "test/cupertino/debug_test.dart",
    ]
    with open(warmup_log, "w") as wf:
        subprocess.run(
            warmup_cmd,
            cwd=str(fw_root),
            env=env,
            stdout=wf,
            stderr=subprocess.STDOUT,
            timeout=300,
        )
    log(f"[{label}] Warmed up flutter_tools, assets, and kernel cache ({int(time.time() - t_warm)}s)", driver_log)

    # 2. Split test_files into num_shards
    actual_shards = min(num_shards, len(test_files))
    shards = [test_files[i::actual_shards] for i in range(actual_shards)]
    log(
        f"[{label}] Launching {actual_shards} shards x -j {concurrency} ({len(test_files)} test files total)...",
        driver_log,
    )

    t0 = time.time()
    procs = []
    for idx, shard_files in enumerate(shards):
        s_json = shards_dir / f"shard_{idx:02d}.json"
        s_log = shards_dir / f"shard_{idx:02d}.log"
        cmd = [
            str(repo / "bin/flutter"),
            "test",
            "--no-pub",
            "--no-test-assets",
            "--local-web-sdk=wasm_release",
            "--platform=chrome",
            "-j",
            str(concurrency),
            f"--file-reporter=json:{s_json}",
            *shard_files,
        ]
        lf = open(s_log, "w")
        p = subprocess.Popen(
            cmd,
            cwd=str(fw_root),
            env=env,
            stdout=lf,
            stderr=subprocess.STDOUT,
        )
        procs.append((idx, p, lf, time.time()))

    deadline = t0 + timeout
    worst_rc = 0
    remaining = list(procs)
    while remaining:
        now = time.time()
        if now >= deadline:
            for idx, p, lf, _ in remaining:
                p.kill()
                lf.close()
                log(f"[{label}] Shard {idx:02d} TIMED OUT after {timeout}s", driver_log)
            worst_rc = 124
            break
        still_running = []
        for item in remaining:
            idx, p, lf, st = item
            rc = p.poll()
            if rc is None:
                still_running.append(item)
            else:
                lf.close()
                if rc != 0 and worst_rc == 0:
                    worst_rc = rc
                log(
                    f"[{label}] Shard {idx:02d}/{actual_shards} finished exit={rc} ({int(time.time() - st)}s)",
                    driver_log,
                )
        remaining = still_running
        if remaining:
            time.sleep(1.0)

    dt = int(time.time() - t0)
    log(f"[{label}] Part 2 sharded flutter test exit={worst_rc} ({dt}s)", driver_log)
    return worst_rc, shards_dir


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "targets",
        nargs="*",
        default=["current=WORKTREE"],
        help="List of label=ref pairs (use WORKTREE for current working tree). Default: current=WORKTREE",
    )
    ap.add_argument(
        "--repo",
        default="/usr/local/google/home/jlavrova/.gemini/jetski/scratch/flutter",
        help="Path to flutter repository root",
    )
    ap.add_argument(
        "--out",
        default="/tmp/webparagraph_corpus",
        help="Output directory for logs and compare.md (default: /tmp/webparagraph_corpus)",
    )
    ap.add_argument(
        "--part",
        choices=["all", "web-ui", "framework"],
        default="all",
        help="Which part of the corpus to run (default: all)",
    )
    ap.add_argument(
        "--full",
        action="store_true",
        help="Run the full corpus (all 60 test/ui/* in Part 1 and all 916 packages/flutter/test files in Part 2)",
    )
    ap.add_argument(
        "--flutter-tests",
        nargs="*",
        default=None,
        help="Optional custom directories/files relative to packages/flutter to run in sharded mode",
    )
    ap.add_argument(
        "--flutter-shards",
        type=int,
        default=8,
        help="Number of parallel `flutter test` shard processes in `--full` mode (default: 8)",
    )
    ap.add_argument(
        "--flutter-concurrency",
        type=int,
        default=4,
        help="Number of concurrent Chrome browsers (`-j`) per shard in `--full` mode (default: 4)",
    )
    ap.add_argument(
        "--no-force-test-fonts",
        action="store_true",
        help="Do not inject the temporary forceTestFonts patch during Part 1 and Part 2 tests",
    )
    ap.add_argument(
        "--skip-analyze",
        action="store_true",
        help="Skip `dart analyze --fatal-infos` in Part 1",
    )
    ap.add_argument(
        "--felt-timeout",
        type=int,
        default=900,
        help="Timeout in seconds for `felt test` per ref (default: 900)",
    )
    ap.add_argument(
        "--flutter-timeout",
        type=int,
        default=1800,
        help="Timeout in seconds for `flutter test` per ref (default: 1800)",
    )
    ap.add_argument(
        "--resume",
        action="store_true",
        help="Skip Part 1 or Part 2 for a ref if its summary JSON already exists in --out",
    )
    args = ap.parse_args()

    repo = pathlib.Path(args.repo).resolve()
    web_ui = repo / "engine/src/flutter/lib/web_ui"
    felt_config = web_ui / "test/felt_config.yaml"
    tools_files = [
        repo / "packages/flutter_tools/lib/src/test/flutter_web_platform.dart",
        repo / "packages/flutter_tools/lib/src/commands/test.dart",
        repo / "packages/flutter_tools/lib/src/web/chrome.dart",
        repo / "packages/flutter_tools/lib/src/test/web_test_compiler.dart",
        repo / "packages/flutter_tools/lib/src/test/test_compiler.dart",
    ]
    tools_stamp = repo / "bin/cache/flutter_tools.stamp"
    dart_bin = repo / "engine/src/flutter/prebuilts/linux-x64/dart-sdk/bin/dart"
    ninja_bin = find_ninja(repo)
    env = build_env(repo)

    out_dir = pathlib.Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    driver_log = out_dir / "driver.log"

    orig_tools_bytes = {p: p.read_bytes() for p in tools_files}
    orig_felt_bytes = felt_config.read_bytes()

    worktree_patch = out_dir / "worktree_web_ui.patch"
    diff_res = subprocess.run(
        ["git", "-C", str(repo), "diff", "HEAD", "--", "engine/src/flutter/lib/web_ui"],
        capture_output=True,
        check=True,
    )
    worktree_patch.write_bytes(diff_res.stdout)

    run_part1 = args.part in ("all", "web-ui")
    run_part2 = args.part in ("all", "framework")

    def cleanup():
        log("=== Cleaning up temporary harness state ===", driver_log)
        try:
            checkout_web_ui_ref(repo, "WORKTREE", worktree_patch)
        except Exception as e:
            log(f"WARNING: restoring web_ui failed: {e}", driver_log)
        try:
            if felt_config.read_bytes() != orig_felt_bytes and not worktree_patch.read_bytes():
                felt_config.write_bytes(orig_felt_bytes)
        except Exception as e:
            log(f"WARNING: restoring felt_config.yaml failed: {e}", driver_log)
        try:
            restored_any = False
            for p, b in orig_tools_bytes.items():
                if p.read_bytes() != b:
                    p.write_bytes(b)
                    restored_any = True
            if restored_any and tools_stamp.exists():
                tools_stamp.unlink()
        except Exception as e:
            log(f"WARNING: restoring flutter_tools files failed: {e}", driver_log)
        if run_part2:
            try:
                subprocess.run(
                    [ninja_bin, "-C", str(repo / "engine/src/out/wasm_release"), "flutter/web_sdk"],
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception as e:
                log(f"WARNING: rebuilding clean flutter/web_sdk failed: {e}", driver_log)

    def handle_sig(signum, _frame):
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        log(f"Received signal {signum}, aborting and cleaning up...", driver_log)
        cleanup()
        sys.exit(128 + signum)

    signal.signal(signal.SIGINT, handle_sig)
    signal.signal(signal.SIGTERM, handle_sig)

    compare_args = []
    try:
        if run_part2:
            patch_flutter_tools_for_web_corpus(repo)
            if tools_stamp.exists():
                tools_stamp.unlink()

        for pair in args.targets:
            if "=" in pair:
                label, ref = pair.split("=", 1)
            else:
                label, ref = pair, pair

            ref_dir = out_dir / label
            ref_dir.mkdir(parents=True, exist_ok=True)
            status_file = ref_dir / "status.txt"
            if not (args.resume and status_file.exists()):
                status_file.write_text(f"label={label}\nref={ref}\n")

            log(f"=== [{label}] Switching web_ui to {ref} ===", driver_log)
            checkout_web_ui_ref(repo, ref, worktree_patch)

            att_path = ref_dir / "attestation.json"
            attestation = {}
            if args.resume and att_path.exists():
                try:
                    attestation = json.loads(att_path.read_text())
                except Exception:
                    attestation = {}
            attestation.update(compute_web_ui_attestation(repo, ref))
            attestation["label"] = label
            attestation["corpus_mode"] = (
                "full" if args.full else ("custom" if args.flutter_tests else "targeted")
            )
            attestation["part"] = args.part
            attestation["force_test_fonts"] = not args.no_force_test_fonts
            att_path.write_text(json.dumps(attestation, indent=2))

            felt_sum_json = ref_dir / "felt_summary.json"
            if run_part1 and args.resume and felt_sum_json.exists():
                log(f"[{label}] Skipping Part 1 (--resume: {felt_sum_json} exists)", driver_log)
            elif run_part1:
                if not args.skip_analyze:
                    t0 = time.time()
                    analyze_log = ref_dir / "analyze.log"
                    with open(analyze_log, "w") as lf:
                        subprocess.run(
                            [str(dart_bin), "pub", "get"],
                            cwd=str(web_ui),
                            env=env,
                            stdout=lf,
                            stderr=subprocess.STDOUT,
                        )
                        res = subprocess.run(
                            [str(dart_bin), "analyze", "--fatal-infos"],
                            cwd=str(web_ui),
                            env=env,
                            stdout=lf,
                            stderr=subprocess.STDOUT,
                        )
                    dt = int(time.time() - t0)
                    log(f"[{label}] dart analyze exit={res.returncode} ({dt}s)", driver_log)
                    with open(status_file, "a") as sf:
                        sf.write(f"analyze={res.returncode}\n")
                    attestation["analyze_rc"] = res.returncode

                if not args.no_force_test_fonts:
                    ftf_applied = ensure_force_test_fonts(repo)
                    if ftf_applied:
                        log(f"[{label}] Applied temporary forceTestFonts patch for Part 1", driver_log)

                ensure_felt_temp_suite(felt_config)
                wp_tests = sorted(
                    str(p.relative_to(web_ui)) for p in (web_ui / "test/webparagraph").glob("*_test.dart")
                )
                if args.full:
                    ui_tests = sorted(
                        str(p.relative_to(web_ui)) for p in (web_ui / "test/ui").glob("*_test.dart")
                    )
                else:
                    ui_tests = UI_TEXT_TESTS
                attestation["part1_expected_wp_files"] = len(wp_tests)
                attestation["part1_expected_ui_files"] = len(ui_tests)
                att_path.write_text(json.dumps(attestation, indent=2))

                felt_cmd = [
                    "./dev/felt",
                    "test",
                    "--suite",
                    "chrome-dart2js-webparagraph-ui",
                    "--suite",
                    "chrome-dart2js-webparagraph-ui-text",
                    *wp_tests,
                    *ui_tests,
                ]
                felt_log = ref_dir / "felt.log"
                t0 = time.time()
                with open(felt_log, "w") as lf:
                    try:
                        res = subprocess.run(
                            felt_cmd,
                            cwd=str(web_ui),
                            env=env,
                            stdout=lf,
                            stderr=subprocess.STDOUT,
                            timeout=args.felt_timeout,
                        )
                        rc = res.returncode
                    except subprocess.TimeoutExpired:
                        rc = 124
                dt = int(time.time() - t0)
                log(f"[{label}] Part 1 (felt test) exit={rc} ({dt}s)", driver_log)
                with open(status_file, "a") as sf:
                    sf.write(f"felt={rc}\n")
                attestation["part1_rc"] = rc
                attestation["part1_duration_s"] = dt
                att_path.write_text(json.dumps(attestation, indent=2))

                felt_sum_txt = ref_dir / "felt_summary.txt"
                with open(felt_sum_txt, "w") as sf:
                    subprocess.run(
                        [sys.executable, str(PARSER_SCRIPT), "summarize-felt", str(felt_log), "--json", str(felt_sum_json)],
                        stdout=sf,
                        stderr=subprocess.STDOUT,
                        check=True,
                    )

            fl_sum_json = ref_dir / "flutter_summary.json"
            if run_part2 and args.resume and fl_sum_json.exists():
                log(f"[{label}] Skipping Part 2 (--resume: {fl_sum_json} exists)", driver_log)
            elif run_part2:
                checkout_web_ui_ref(repo, ref, worktree_patch)
                if not args.no_force_test_fonts:
                    ftf_applied = ensure_force_test_fonts(repo)
                    if ftf_applied:
                        log(f"[{label}] Applied temporary forceTestFonts patch for Part 2", driver_log)

                t0 = time.time()
                ninja_log = ref_dir / "ninja_web_sdk.log"
                with open(ninja_log, "w") as lf:
                    subprocess.run(
                        [ninja_bin, "-C", str(repo / "engine/src/out/wasm_release"), "flutter/web_sdk"],
                        env=env,
                        stdout=lf,
                        stderr=subprocess.STDOUT,
                        check=True,
                    )
                ninja_dt = int(time.time() - t0)
                log(f"[{label}] Built flutter/web_sdk ({ninja_dt}s)", driver_log)

                dill_path = repo / "engine/src/out/wasm_release/flutter_web_sdk/kernel/ddc_outline.dill"
                attestation["part2_ninja_duration_s"] = ninja_dt
                attestation["part2_ninja_actions"] = parse_ninja_actions(ninja_log)
                attestation["part2_ddc_outline_sha256"] = file_sha256_short(dill_path)

                if args.full or args.flutter_tests:
                    fw_tests = collect_all_framework_web_tests(repo, args.flutter_tests)
                    attestation["part2_expected_files"] = len(fw_tests)
                    attestation["part2_expected_chrome_suites"] = count_chrome_eligible_tests(repo, fw_tests)
                    att_path.write_text(json.dumps(attestation, indent=2))
                    rc, shards_target = run_sharded_framework_tests(
                        repo=repo,
                        env=env,
                        ref_dir=ref_dir,
                        label=label,
                        test_files=fw_tests,
                        num_shards=args.flutter_shards,
                        concurrency=args.flutter_concurrency,
                        timeout=args.flutter_timeout,
                        driver_log=driver_log,
                    )
                    summary_input = shards_target
                else:
                    attestation["part2_expected_files"] = len(FRAMEWORK_TEXT_TESTS)
                    attestation["part2_expected_chrome_suites"] = count_chrome_eligible_tests(
                        repo, FRAMEWORK_TEXT_TESTS
                    )
                    att_path.write_text(json.dumps(attestation, indent=2))
                    flutter_log = ref_dir / "flutter.log"
                    flutter_cmd = [
                        str(repo / "bin/flutter"),
                        "test",
                        "--local-web-sdk=wasm_release",
                        "--platform=chrome",
                        "--reporter=expanded",
                        *FRAMEWORK_TEXT_TESTS,
                    ]
                    t0 = time.time()
                    with open(flutter_log, "w") as lf:
                        try:
                            res = subprocess.run(
                                flutter_cmd,
                                cwd=str(repo / "packages/flutter"),
                                env=env,
                                stdout=lf,
                                stderr=subprocess.STDOUT,
                                timeout=args.flutter_timeout,
                            )
                            rc = res.returncode
                        except subprocess.TimeoutExpired:
                            rc = 124
                    dt = int(time.time() - t0)
                    log(f"[{label}] Part 2 (flutter test) exit={rc} ({dt}s)", driver_log)
                    summary_input = flutter_log

                with open(status_file, "a") as sf:
                    sf.write(f"flutter={rc}\n")
                attestation["part2_rc"] = rc
                attestation["timestamp_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                att_path.write_text(json.dumps(attestation, indent=2))

                fl_sum_txt = ref_dir / "flutter_summary.txt"
                fl_sum_json = ref_dir / "flutter_summary.json"
                with open(fl_sum_txt, "w") as sf:
                    subprocess.run(
                        [
                            sys.executable,
                            str(PARSER_SCRIPT),
                            "summarize-flutter",
                            str(summary_input),
                            "--json",
                            str(fl_sum_json),
                        ],
                        stdout=sf,
                        stderr=subprocess.STDOUT,
                        check=True,
                    )
                fl_data = json.loads(fl_sum_json.read_text())
                if not fl_data.get("webparagraph_active"):
                    log(
                        f"WARNING: [{label}] `[CanvasKit served]: webparagraph/canvaskit.js` not found in {summary_input}!",
                        driver_log,
                    )

            compare_args.append(f"{label}={ref_dir}")

        compare_md = out_dir / "compare.md"
        with open(compare_md, "w") as cf:
            subprocess.run(
                [sys.executable, str(PARSER_SCRIPT), "compare", *compare_args],
                stdout=cf,
                stderr=subprocess.STDOUT,
                check=True,
            )
        log(f"=== Report written to {compare_md} ===", driver_log)
        print("\n" + compare_md.read_text())
    finally:
        cleanup()


if __name__ == "__main__":
    main()
