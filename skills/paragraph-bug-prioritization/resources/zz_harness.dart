// Differential harness: runs the same dart:ui paragraph ops on whichever text stack the current
// felt suite selects (WebParagraph or SkParagraph) and prints JSON results for offline diffing.
// Untracked scratch file, never commit.

import 'dart:convert';
import 'dart:math' as math;
import 'dart:typed_data';

import 'package:test/test.dart';
import 'package:ui/src/engine.dart' as engine;
import 'package:ui/ui.dart' as ui;

import '../common/test_initialization.dart';
import 'zz_cases.dart';

String get zzSide => engine.isWebParagraphEnabled ? 'wp' : 'skp';

ui.FontWeight? _weight(Object? v) => v == null ? null : ui.FontWeight(v as int);

ui.TextDecoration? _decoration(Object? v) {
  if (v == null) {
    return null;
  }
  final list = <ui.TextDecoration>[];
  for (final String d in (v as List<dynamic>).cast<String>()) {
    list.add(switch (d) {
      'underline' => ui.TextDecoration.underline,
      'overline' => ui.TextDecoration.overline,
      'lineThrough' => ui.TextDecoration.lineThrough,
      _ => ui.TextDecoration.none,
    });
  }
  return ui.TextDecoration.combine(list);
}

List<ui.FontFeature>? _features(Object? v) => v == null
    ? null
    : <ui.FontFeature>[
        for (final dynamic f in v as List<dynamic>)
          ui.FontFeature((f as List<dynamic>)[0] as String, f[1] as int),
      ];

List<ui.FontVariation>? _variations(Object? v) => v == null
    ? null
    : <ui.FontVariation>[
        for (final dynamic f in v as List<dynamic>)
          ui.FontVariation(
            (f as List<dynamic>)[0] as String,
            (f[1] as num).toDouble(),
          ),
      ];

ui.TextLeadingDistribution? _ld(Object? v) => v == null
    ? null
    : (v == 'even'
          ? ui.TextLeadingDistribution.even
          : ui.TextLeadingDistribution.proportional);

double? _d(Object? v) => v == null ? null : (v as num).toDouble();

ui.TextStyle textStyleFrom(Map<String, dynamic> s) {
  return ui.TextStyle(
    color: s['color'] == null ? null : ui.Color(s['color'] as int),
    decoration: _decoration(s['decoration']),
    decorationColor: s['decorationColor'] == null
        ? null
        : ui.Color(s['decorationColor'] as int),
    decorationThickness: _d(s['decorationThickness']),
    fontWeight: _weight(s['fontWeight']),
    fontStyle: s['italic'] == true ? ui.FontStyle.italic : null,
    fontFamily: s['fontFamily'] as String?,
    fontFamilyFallback: (s['fontFamilyFallback'] as List<dynamic>?)
        ?.cast<String>(),
    fontSize: _d(s['fontSize']),
    letterSpacing: _d(s['letterSpacing']),
    wordSpacing: _d(s['wordSpacing']),
    height: _d(s['height']),
    leadingDistribution: _ld(s['leadingDistribution']),
    locale: s['locale'] == null ? null : ui.Locale(s['locale'] as String),
    fontFeatures: _features(s['fontFeatures']),
    fontVariations: _variations(s['fontVariations']),
  );
}

ui.TextAlign? _align(Object? v) => switch (v) {
  'left' => ui.TextAlign.left,
  'right' => ui.TextAlign.right,
  'center' => ui.TextAlign.center,
  'justify' => ui.TextAlign.justify,
  'start' => ui.TextAlign.start,
  'end' => ui.TextAlign.end,
  _ => null,
};

ui.ParagraphStyle paragraphStyleFrom(Map<String, dynamic> p) {
  final Map<String, dynamic>? strut = p['strut'] as Map<String, dynamic>?;
  final Map<String, dynamic>? thb = p['thb'] as Map<String, dynamic>?;
  return ui.ParagraphStyle(
    textAlign: _align(p['textAlign']),
    textDirection: p['rtl'] == true
        ? ui.TextDirection.rtl
        : ui.TextDirection.ltr,
    maxLines: p['maxLines'] as int?,
    fontFamily: p['fontFamily'] as String?,
    fontSize: _d(p['fontSize']),
    height: _d(p['height']),
    fontWeight: _weight(p['fontWeight']),
    ellipsis: p['ellipsis'] as String?,
    textHeightBehavior: thb == null
        ? null
        : ui.TextHeightBehavior(
            applyHeightToFirstAscent: thb['first'] as bool? ?? true,
            applyHeightToLastDescent: thb['last'] as bool? ?? true,
            leadingDistribution:
                _ld(thb['ld']) ?? ui.TextLeadingDistribution.proportional,
          ),
    strutStyle: strut == null
        ? null
        : ui.StrutStyle(
            fontFamily: strut['fontFamily'] as String?,
            fontSize: _d(strut['fontSize']),
            height: _d(strut['height']),
            leading: _d(strut['leading']),
            forceStrutHeight: strut['force'] as bool?,
          ),
  );
}

const List<ui.PlaceholderAlignment> _phAlign = ui.PlaceholderAlignment.values;

String _err(Object e, StackTrace st) {
  final List<String> frames = st
      .toString()
      .split('\n')
      .where((String l) => l.trim().isNotEmpty)
      .take(6)
      .toList();
  return '${e.runtimeType}: ${e.toString().split('\n').first} @@ ${frames.join(' | ')}';
}

double _r(double v) => v.isFinite
    ? (v * 1000).roundToDouble() / 1000
    : (v.isNaN ? -999999 : (v > 0 ? 999999 : -999999));

List<double> _box(ui.TextBox b) => <double>[
  _r(b.left),
  _r(b.top),
  _r(b.right),
  _r(b.bottom),
  if (b.direction == ui.TextDirection.rtl) 1 else 0,
];

/// Builds and lays out one case and returns every observable as plain JSON data
Map<String, dynamic> runCase(Map<String, dynamic> c, {bool full = true}) {
  final out = <String, dynamic>{'id': c['id'], 'side': zzSide};
  final errors = <String, String>{};
  out['errors'] = errors;
  final Map<String, dynamic> pstyle =
      (c['p'] as Map<String, dynamic>?) ?? <String, dynamic>{};
  final List<dynamic> ops = c['ops'] as List<dynamic>;
  final double width = c['w'] is String
      ? double.nan
      : ((c['w'] as num).toDouble() == -1
            ? double.infinity
            : (c['w'] as num).toDouble());
  final buf = StringBuffer();
  ui.Paragraph? para;
  try {
    final builder = ui.ParagraphBuilder(paragraphStyleFrom(pstyle));
    for (final dynamic op in ops) {
      final List<dynamic> o = op as List<dynamic>;
      switch (o[0]) {
        case 'push':
          builder.pushStyle(textStyleFrom(o[1] as Map<String, dynamic>));
        case 'pop':
          builder.pop();
        case 't':
          builder.addText(o[1] as String);
          buf.write(o[1] as String);
        case 'ph':
          builder.addPlaceholder(
            (o[1] as num).toDouble(),
            (o[2] as num).toDouble(),
            _phAlign[o[3] as int],
            baselineOffset: _d(o.length > 4 ? o[4] : null),
            baseline: ui.TextBaseline.alphabetic,
          );
          buf.write('\uFFFC');
      }
    }
    para = builder.build();
  } catch (e, st) {
    errors['build'] = _err(e, st);
    return out;
  }
  try {
    // Optional earlier layouts of the same object, as TextPainter does on constraint changes.
    for (final dynamic pw
        in (c['pre'] as List<dynamic>?) ?? const <dynamic>[]) {
      para.layout(
        ui.ParagraphConstraints(
          width: (pw as num).toDouble() == -1 ? double.infinity : pw.toDouble(),
        ),
      );
    }
    final Object wSpec = c['w'] as Object;
    if (wSpec is String) {
      // Width derived from intrinsics: lay out unconstrained first, as TextPainter does.
      para.layout(const ui.ParagraphConstraints(width: double.infinity));
      final double base = wSpec.startsWith('max')
          ? para.maxIntrinsicWidth
          : para.minIntrinsicWidth;
      final double delta = wSpec.length > 5
          ? double.parse(wSpec.substring(5))
          : 0;
      out['wUsed'] = _r(base + delta);
      para.layout(ui.ParagraphConstraints(width: base + delta));
    } else {
      para.layout(ui.ParagraphConstraints(width: width));
    }
  } catch (e, st) {
    errors['layout'] = _err(e, st);
    return out;
  }
  final String text = buf.toString();
  final int n = text.length;
  out['n'] = n;
  void q(String key, Object? Function() f) {
    try {
      out[key] = f();
    } catch (e, st) {
      errors[key] = _err(e, st);
    }
  }

  final ui.Paragraph p = para;
  q('height', () => _r(p.height));
  q('width', () => _r(p.width));
  q('longestLine', () => _r(p.longestLine));
  q('minIW', () => _r(p.minIntrinsicWidth));
  q('maxIW', () => _r(p.maxIntrinsicWidth));
  q('alpha', () => _r(p.alphabeticBaseline));
  q('ideo', () => _r(p.ideographicBaseline));
  q('exceed', () => p.didExceedMaxLines);
  q('numLines', () => p.numberOfLines);
  q(
    'lines',
    () => <List<Object>>[
      for (final ui.LineMetrics m in p.computeLineMetrics())
        <Object>[
          m.lineNumber,
          m.hardBreak,
          _r(m.ascent),
          _r(m.descent),
          _r(m.height),
          _r(m.width),
          _r(m.left),
          _r(m.baseline),
        ],
    ],
  );
  q(
    'phBoxes',
    () => <List<double>>[
      for (final ui.TextBox b in p.getBoxesForPlaceholders()) _box(b),
    ],
  );
  q(
    'allBoxes',
    () => <List<double>>[
      for (final ui.TextBox b in p.getBoxesForRange(0, n)) _box(b),
    ],
  );
  if (!full) {
    return out;
  }
  _queries(p, n, out, errors);
  // Reflow invariants (mutates layout, so it runs last): a paragraph laid out at its own
  // maxIntrinsicWidth must not wrap more than unconstrained, and at minIntrinsicWidth no line
  // may be wider than minIntrinsicWidth.
  if (c['reflow'] != false) {
    q('reflow', () {
      final double maxIW = p.maxIntrinsicWidth;
      final double minIW = p.minIntrinsicWidth;
      p.layout(const ui.ParagraphConstraints(width: double.infinity));
      final int linesInf = p.numberOfLines;
      p.layout(ui.ParagraphConstraints(width: maxIW));
      final int linesMax = p.numberOfLines;
      final double llMax = p.longestLine;
      p.layout(ui.ParagraphConstraints(width: minIW));
      final int linesMin = p.numberOfLines;
      final double llMin = p.longestLine;
      return <Object>[
        linesInf,
        linesMax,
        _r(llMax),
        linesMin,
        _r(llMin),
        _r(maxIW),
        _r(minIW),
      ];
    });
  }
  return out;
}

void _queries(
  ui.Paragraph p,
  int n,
  Map<String, dynamic> out,
  Map<String, String> errors,
) {
  void q(String key, Object? Function() f) {
    try {
      out[key] = f();
    } catch (e, st) {
      errors[key] = _err(e, st);
    }
  }

  q(
    'allBoxesMax',
    () => <List<double>>[
      for (final ui.TextBox b in p.getBoxesForRange(
        0,
        n,
        boxHeightStyle: ui.BoxHeightStyle.max,
        boxWidthStyle: ui.BoxWidthStyle.max,
      ))
        _box(b),
    ],
  );
  // Per code unit boxes, merged per index; ranges inside a grapheme may legitimately be empty.
  final cuBoxes = <Object?>[];
  for (int i = 0; i < n; i++) {
    try {
      cuBoxes.add(<List<double>>[
        for (final ui.TextBox b in p.getBoxesForRange(i, i + 1)) _box(b),
      ]);
    } catch (e, st) {
      errors['box@$i'] ??= _err(e, st);
      cuBoxes.add(null);
    }
  }
  out['cuBoxes'] = cuBoxes;
  final lineNo = <Object?>[];
  final wordB = <Object?>[];
  final lineB = <Object?>[];
  for (int i = 0; i <= n; i++) {
    try {
      lineNo.add(p.getLineNumberAt(i));
    } catch (e, st) {
      errors['lineNo@$i'] ??= _err(e, st);
      lineNo.add('E');
    }
    try {
      final ui.TextRange r = p.getWordBoundary(ui.TextPosition(offset: i));
      wordB.add(<int>[r.start, r.end]);
    } catch (e, st) {
      errors['word@$i'] ??= _err(e, st);
      wordB.add(null);
    }
    try {
      final ui.TextRange r = p.getLineBoundary(ui.TextPosition(offset: i));
      lineB.add(<int>[r.start, r.end]);
    } catch (e, st) {
      errors['lineB@$i'] ??= _err(e, st);
      lineB.add(null);
    }
  }
  out['lineNo'] = lineNo;
  out['wordB'] = wordB;
  out['lineB'] = lineB;
  // Hit-test grid: rows at each line's vertical middle plus above/below the paragraph.
  final pos = <Object?>[];
  try {
    final List<ui.LineMetrics> lm = p.computeLineMetrics();
    final ys = <double>[-5];
    for (final ui.LineMetrics m in lm) {
      ys.add(m.baseline - m.ascent / 2);
    }
    ys.add(p.height + 5);
    final double w = p.width.isFinite ? p.width : p.maxIntrinsicWidth;
    const int steps = 24;
    for (final double y in ys) {
      final row = <Object>[];
      for (int k = -1; k <= steps + 1; k++) {
        // Offset avoids exact glyph-midpoint ties with the test font.
        final double x = w * k / steps + 0.37;
        try {
          final ui.TextPosition tp = p.getPositionForOffset(ui.Offset(x, y));
          row.add(
            tp.affinity == ui.TextAffinity.upstream
                ? -tp.offset - 1
                : tp.offset,
          );
        } catch (e, st) {
          errors['pos@$x,$y'] ??= _err(e, st);
          row.add('E');
        }
      }
      pos.add(<Object>[_r(y), row]);
    }
  } catch (e, st) {
    errors['posgrid'] = _err(e, st);
  }
  out['pos'] = pos;
}

/// Renders the paragraph and returns the bounding box of pixels matching an exact RGBA color
Future<List<int>?> colorBounds(ui.Paragraph p, int argb, int w, int h) async {
  final recorder = ui.PictureRecorder();
  final canvas = ui.Canvas(recorder);
  canvas.drawParagraph(p, const ui.Offset(10, 10));
  final ui.Picture pic = recorder.endRecording();
  final ui.Image img = await pic.toImage(w, h);
  final ByteData? data = await img.toByteData();
  if (data == null) {
    return null;
  }
  final int r = (argb >> 16) & 0xff, g = (argb >> 8) & 0xff, b = argb & 0xff;
  int minX = 1 << 30, minY = 1 << 30, maxX = -1, maxY = -1;
  for (int y = 0; y < h; y++) {
    for (int x = 0; x < w; x++) {
      final int o = (y * w + x) * 4;
      final int pr = data.getUint8(o),
          pg = data.getUint8(o + 1),
          pb = data.getUint8(o + 2);
      final int pa = data.getUint8(o + 3);
      if (pa > 200 &&
          (pr - r).abs() < 40 &&
          (pg - g).abs() < 40 &&
          (pb - b).abs() < 40) {
        minX = math.min(minX, x);
        minY = math.min(minY, y);
        maxX = math.max(maxX, x);
        maxY = math.max(maxY, y);
      }
    }
  }
  return maxX < 0 ? <int>[] : <int>[minX - 10, minY - 10, maxX - 10, maxY - 10];
}

void zzMain() {
  setUpUnitTests();
  test('zz differential cases', () async {
    final List<dynamic> cases =
        jsonDecode(utf8.decode(base64.decode(kZzCasesB64))) as List<dynamic>;
    // Let asset fonts settle before measuring.
    await Future<void>.delayed(const Duration(milliseconds: 300));
    final sb = StringBuffer();
    int count = 0;
    for (final dynamic c in cases) {
      final Map<String, dynamic> m = c as Map<String, dynamic>;
      Map<String, dynamic> res;
      try {
        res = runCase(m, full: m['full'] as bool? ?? true);
        if (m['pix'] != null) {
          // Pixel probe: bounding box of a given color after painting.
          final ui.ParagraphBuilder b = ui.ParagraphBuilder(
            paragraphStyleFrom(
              (m['p'] as Map<String, dynamic>?) ?? <String, dynamic>{},
            ),
          );
          for (final dynamic op in m['ops'] as List<dynamic>) {
            final List<dynamic> o = op as List<dynamic>;
            switch (o[0]) {
              case 'push':
                b.pushStyle(textStyleFrom(o[1] as Map<String, dynamic>));
              case 'pop':
                b.pop();
              case 't':
                b.addText(o[1] as String);
            }
          }
          final ui.Paragraph pp = b.build();
          pp.layout(ui.ParagraphConstraints(width: (m['w'] as num).toDouble()));
          try {
            res['pix'] = await colorBounds(pp, m['pix'] as int, 420, 200);
          } catch (e, st) {
            (res['errors'] as Map<String, String>)['pix'] = _err(e, st);
          }
        }
      } catch (e, st) {
        res = <String, dynamic>{
          'id': m['id'],
          'side': zzSide,
          'errors': <String, String>{'harness': _err(e, st)},
        };
      }
      sb.writeln('ZZRES ${jsonEncode(res)}');
      count++;
      if (count % 20 == 0) {
        print(sb.toString());
        sb.clear();
      }
    }
    print(sb.toString());
    print('ZZDONE $zzSide $count');
  }, timeout: const Timeout(Duration(minutes: 30)));
}
