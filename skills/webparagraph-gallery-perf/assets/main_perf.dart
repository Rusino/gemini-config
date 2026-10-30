// Copyright 2014 The Flutter Authors. All rights reserved.
// Use of this source code is governed by a BSD-style license that can be
// found in the LICENSE file.

// Benchmark entry point of the webparagraph-gallery-perf skill: drives Flutter
// Gallery through text-heavy pages and prints per-phase frame statistics from
// FrameTiming. Trace marks keep the historical "R31:" prefix that
// gallery_analysis.py looks for. Not for upload.

import 'dart:async';
import 'dart:convert';
import 'dart:js_interop';
import 'dart:math' as math;
import 'dart:js_interop_unsafe';
import 'dart:ui' as ui;

import 'package:flutter/gestures.dart';
import 'package:flutter/material.dart';
import 'package:flutter/scheduler.dart';
import 'package:gallery/main.dart' show GalleryApp;
import 'package:google_fonts/google_fonts.dart';

double _nowMs() =>
    (globalContext.getProperty<JSObject>('performance'.toJS))
        .callMethod<JSNumber>('now'.toJS)
        .toDartDouble;

final List<ui.FrameTiming> _timings = <ui.FrameTiming>[];

NavigatorState _rootNavigator() {
  NavigatorState? found;
  void visit(Element e) {
    if (found != null) {
      return;
    }
    if (e is StatefulElement && e.state is NavigatorState) {
      found = e.state as NavigatorState;
      return;
    }
    e.visitChildren(visit);
  }

  WidgetsBinding.instance.rootElement!.visitChildren(visit);
  return found!;
}

Future<void> _settle(int ms) => Future<void>.delayed(Duration(milliseconds: ms));

Offset get _center {
  final ui.FlutterView view = WidgetsBinding.instance.platformDispatcher.implicitView!;
  final Size size = view.physicalSize / view.devicePixelRatio;
  return Offset(size.width / 2, size.height * 0.6);
}

Future<void> _wheel(int steps, double dy) async {
  for (var i = 0; i < steps; i++) {
    GestureBinding.instance.handlePointerEvent(
      PointerScrollEvent(
        kind: PointerDeviceKind.mouse,
        position: _center,
        scrollDelta: Offset(0, dy),
      ),
    );
    await SchedulerBinding.instance.endOfFrame;
  }
}

double _pct(List<double> sorted, double p) {
  if (sorted.isEmpty) {
    return 0;
  }
  final int i = ((sorted.length - 1) * p).round();
  return sorted[i];
}

void _mark(String what) {
  globalContext
      .getProperty<JSObject>('performance'.toJS)
      .callMethod<JSAny?>('mark'.toJS, 'R31:$what'.toJS);
  print('GALLERY_MARK $what');
}

// Number of input events of the current real-time phase (0 for paced phases).
int _rtSteps = 0;

double _maxInterval(List<ui.FrameTiming> frames) {
  final List<int> v =
      frames.map((ui.FrameTiming t) => t.timestampInMicroseconds(ui.FramePhase.vsyncStart)).toList()
        ..sort();
  var best = 0;
  for (var i = 1; i < v.length; i++) {
    best = math.max(best, v[i] - v[i - 1]);
  }
  return best / 1000;
}

// Fires one input per 16.7 ms tick without waiting for frames, like a real
// wheel or drag; ticks the engine cannot keep up with get coalesced (dropped).
Future<void> _realtime(int steps, void Function(int i) step) async {
  _rtSteps = steps;
  final done = Completer<void>();
  var i = 0;
  Timer.periodic(const Duration(microseconds: 16667), (Timer t) {
    if (i == steps) {
      t.cancel();
      done.complete();
      return;
    }
    step(i++);
  });
  await done.future;
  await SchedulerBinding.instance.endOfFrame;
}

void _wheelOnce(double dy) {
  GestureBinding.instance.handlePointerEvent(
    PointerScrollEvent(
      kind: PointerDeviceKind.mouse,
      position: _center,
      scrollDelta: Offset(0, dy),
    ),
  );
}

void _resizeStep(int i, int steps) {
  final ui.FlutterView view = WidgetsBinding.instance.platformDispatcher.implicitView!;
  final double full = view.physicalSize.width / view.devicePixelRatio;
  final int half = steps ~/ 2;
  final int k = i < half ? i + 1 : steps - i - 1;
  _hostWidth.value = k <= 0 ? null : full * (1.0 - 0.4 * k / half);
}

Future<void> _phase(String name, Future<void> Function() body) async {
  // Drain pending timings so they are not attributed to this phase.
  await _settle(300);
  _mark('start:$name');
  final double startMs = _nowMs();
  final sw = Stopwatch()..start();
  await body();
  sw.stop();
  final double endMs = _nowMs();
  _mark('end:$name');
  // FrameTiming batches are submitted every 100 ms.
  await _settle(400);
  final List<ui.FrameTiming> mine = _timings.where((ui.FrameTiming t) {
    final double vsyncMs = t.timestampInMicroseconds(ui.FramePhase.vsyncStart) / 1000;
    return vsyncMs >= startMs && vsyncMs <= endMs;
  }).toList();
  List<double> sorted(double Function(ui.FrameTiming) f) => mine.map(f).toList()..sort();
  final List<double> total = sorted((ui.FrameTiming t) => t.totalSpan.inMicroseconds / 1000);
  final List<double> build = sorted((ui.FrameTiming t) => t.buildDuration.inMicroseconds / 1000);
  final List<double> raster = sorted((ui.FrameTiming t) => t.rasterDuration.inMicroseconds / 1000);
  double sum(List<double> l) => l.fold(0.0, (double a, double b) => a + b);
  final stats = <String, Object>{
    'phase': name,
    'wallMs': sw.elapsedMicroseconds / 1000,
    'frames': mine.length,
    'jank16': total.where((double v) => v > 16.7).length,
    'totalSum': sum(total),
    'totalP50': _pct(total, 0.5),
    'totalP90': _pct(total, 0.9),
    'totalMax': total.isEmpty ? 0 : total.last,
    'buildSum': sum(build),
    'rasterSum': sum(raster),
    'steps': _rtSteps,
    'intervalMax': _maxInterval(mine),
  };
  print('GALLERY_PHASE ${jsonEncode(stats)}');
  _rtSteps = 0;
}

const List<String> _demoSlugs = <String>[
  'typography',
  'lists',
  'data-table',
  'text-field',
  'chip',
  'tabs',
  'cupertino-picker',
  'colors',
];

const List<String> _studies = <String>['/fortnightly', '/reply', '/crane'];

Future<void> _openAndScroll(String label, String route) async {
  final NavigatorState nav = _rootNavigator();
  await _phase('open:$label', () async {
    unawaited(nav.pushNamed(route));
    await _settle(1200);
  });
  await _phase('scroll:$label', () async {
    await _wheel(30, 90);
    await _wheel(30, -90);
  });
  nav.pop();
  await _settle(800);
}

// Narrows the whole app and widens it back, one step per frame, so every
// visible paragraph is laid out (and for WebParagraph re-rasterized) again.
final ValueNotifier<double?> _hostWidth = ValueNotifier<double?>(null);

class _ResizeHost extends StatelessWidget {
  const _ResizeHost({required this.child});

  final Widget child;

  @override
  Widget build(BuildContext context) {
    return ValueListenableBuilder<double?>(
      valueListenable: _hostWidth,
      child: child,
      // Keep the same element structure for every width: switching between a
      // bare child and a wrapped one would remount GalleryApp and reset routes.
      builder: (BuildContext context, double? width, Widget? child) => Align(
        alignment: Alignment.topLeft,
        child: SizedBox(width: width ?? double.infinity, child: child),
      ),
    );
  }
}

Future<void> _resizeSweep() async {
  final ui.FlutterView view = WidgetsBinding.instance.platformDispatcher.implicitView!;
  final double full = view.physicalSize.width / view.devicePixelRatio;
  const steps = 20;
  for (var i = 1; i <= 2 * steps; i++) {
    final int k = i <= steps ? i : 2 * steps - i;
    _hostWidth.value = full * (1.0 - 0.4 * k / steps);
    await SchedulerBinding.instance.endOfFrame;
  }
  _hostWidth.value = null;
  await SchedulerBinding.instance.endOfFrame;
}

// Revisits a page several times: SkParagraph may hit its ParagraphCache here,
// WebParagraph rebuilds and re-rasterizes every visit.
Future<void> _reopen(String label, String route, int times) async {
  final NavigatorState nav = _rootNavigator();
  await _phase('reopen:$label', () async {
    for (var i = 0; i < times; i++) {
      unawaited(nav.pushNamed(route));
      await _settle(700);
      nav.pop();
      await _settle(500);
    }
  });
}

Future<void> main() async {
  GoogleFonts.config.allowRuntimeFetching = false;
  WidgetsFlutterBinding.ensureInitialized();
  SchedulerBinding.instance.addTimingsCallback(_timings.addAll);
  final double t0 = _nowMs();
  runApp(const _ResizeHost(child: GalleryApp()));
  await SchedulerBinding.instance.endOfFrame;
  print('GALLERY_FIRST_FRAME_MS ${_nowMs() - t0}');
  // Let the splash animation and deferred loading finish.
  await _settle(4000);
  GestureBinding.instance.handlePointerEvent(
    PointerAddedEvent(kind: PointerDeviceKind.mouse, position: _center),
  );
  try {
    await _scenario();
  } catch (e, st) {
    print('GALLERY_FAILED $e $st'.replaceAll('\n', ' | '));
  }
  print('GALLERY_DONE');
}

Future<void> _scenario() async {
  await _phase('scroll:home', () async {
    await _wheel(40, 90);
    await _wheel(40, -90);
  });
  await _phase('resize:home', _resizeSweep);
  for (final slug in _demoSlugs) {
    await _openAndScroll(slug, '/demo/$slug');
  }
  for (final route in _studies) {
    await _openAndScroll(route.substring(1), route);
  }
  final NavigatorState nav = _rootNavigator();
  unawaited(nav.pushNamed('/fortnightly'));
  await _settle(1200);
  await _phase('resize:fortnightly', _resizeSweep);
  nav.pop();
  await _settle(800);
  for (final (String label, String route) in <(String, String)>[
    ('fortnightly', '/fortnightly'),
    ('reply', '/reply'),
    ('typography', '/demo/typography'),
    ('data-table', '/demo/data-table'),
  ]) {
    await _reopen(label, route, 5);
  }
  await _realtimePhases('home');
  final NavigatorState nav2 = _rootNavigator();
  unawaited(nav2.pushNamed('/fortnightly'));
  await _settle(1200);
  await _realtimePhases('fortnightly');
  nav2.pop();
  await _settle(800);
}

Future<void> _realtimePhases(String label) async {
  await _phase('rt-scroll:$label', () => _realtime(120, (int i) => _wheelOnce(i < 60 ? 90 : -90)));
  await _phase('rt-resize:$label', () async {
    await _realtime(40, (int i) => _resizeStep(i, 40));
    _hostWidth.value = null;
  });
}
