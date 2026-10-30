#!/usr/bin/env python3
# Gallery driver: serves build/web with COOP/COEP, starts Chrome with a CDP port,
# records a Chrome trace (toplevel + user timing), samples per-process CPU time
# from /proc (Linux), and collects GALLERY_* console lines.
# Usage: drive_gallery.py <build/web dir> <out prefix> wp|skp [flags]
# Writes <out prefix>.json and <out prefix>.trace.json.gz; exit 0 only on GALLERY_DONE
import argparse
import asyncio
import base64
import functools
import gzip
import http.server
import json
import os
import re
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

import websockets


# Body runs only as a script: the config pre-commit imports every skill script
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('build')
    ap.add_argument('out_prefix')
    ap.add_argument('mode', choices=['wp', 'skp'])
    ap.add_argument('--chrome', default=os.environ.get('CHROME', '/usr/bin/google-chrome'))
    ap.add_argument('--port', type=int, default=8765)
    ap.add_argument('--cdp-port', type=int, default=9333)
    ap.add_argument('--headful', action='store_true')
    ap.add_argument('--require-gpu', action='store_true',
                    help='abort unless WebGL runs on a hardware GPU')
    ap.add_argument('--timeout', type=int, default=900)
    ap.add_argument('--dpr', type=float, default=1.0)
    ap.add_argument('--cpu-throttle', type=float, default=1.0,
                    help='CDP Emulation.setCPUThrottlingRate for the page renderer')
    ap.add_argument('--no-dbus', action='store_true',
                    help='hide the session DBus from Chrome (over SSH it can stall all navigation)')
    ap.add_argument('--chrome-flag', action='append', default=[],
                    help='extra Chrome flag, e.g. --chrome-flag=--use-angle=gl-egl')
    args = ap.parse_args()

    SOFTWARE = ('swiftshader', 'llvmpipe', 'software', 'none', 'masked')


    class Handler(http.server.SimpleHTTPRequestHandler):
        def end_headers(self):
            self.send_header('Cross-Origin-Opener-Policy', 'same-origin')
            self.send_header('Cross-Origin-Embedder-Policy', 'require-corp')
            self.send_header('Cache-Control', 'no-store')
            super().end_headers()

        def log_message(self, *a):
            pass


    class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
        allow_reuse_address = True
        daemon_threads = True


    httpd = None
    for port in range(args.port, args.port + 20):
        try:
            httpd = Server(('127.0.0.1', port), functools.partial(Handler, directory=args.build))
            args.port = port
            break
        except OSError:
            continue
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    url = f'http://127.0.0.1:{args.port}/index.html' + (
        '?web_paragraph=true' if args.mode == 'wp' else '')
    profile = tempfile.mkdtemp(prefix='wp_gallery_chrome_')
    cmd = [
        args.chrome, f'--user-data-dir={profile}', f'--remote-debugging-port={args.cdp_port}',
        '--enable-experimental-web-platform-features',
        '--window-size=1280,1024', f'--force-device-scale-factor={args.dpr:g}',
        '--no-first-run', '--no-default-browser-check', '--disable-extensions',
        '--disable-background-timer-throttling', '--disable-renderer-backgrounding',
        '--disable-backgrounding-occluded-windows',
        '--enable-logging=stderr', '--v=0', url,
    ]
    if not args.headful:
        cmd.insert(1, '--headless=new')
    cmd[1:1] = args.chrome_flag

    t0 = time.monotonic()
    lines, marks = [], []
    done = threading.Event()
    failed = threading.Event()
    pat = re.compile(r'"(GALLERY_[A-Z_]+(?: .*?)?)", source')
    env = dict(os.environ)
    if args.no_dbus:
        env['DBUS_SESSION_BUS_ADDRESS'] = 'disabled:'
    proc = subprocess.Popen(cmd, stderr=subprocess.PIPE, stdout=subprocess.DEVNULL, text=True,
                            errors='replace', env=env)


    def handle(raw):
        # Page console arrives over CDP (Runtime.consoleAPICalled); some managed
        # Chrome builds never copy it to stderr
        if ('CONSOLE' in raw and ('rror' in raw or 'xception' in raw)
                and 'chrome-extension' not in raw):
            lines.append('CONSOLE_ERROR ' + raw.strip()[:2000])


    def handle_console(line):
        now = time.monotonic() - t0
        lines.append(line)
        if line.startswith('GALLERY_MARK '):
            what = line.split(' ', 1)[1]
            marks.append((now, what))
            if what.startswith('start:'):
                print(f'{now:7.1f}s {what}', flush=True)
        elif line.startswith('GALLERY_ATTESTATION '):
            print(line, flush=True)
            att = json.loads(line.split(' ', 1)[1])
            renderer = str(att.get('webgl2Renderer', 'none')).lower()
            if args.require_gpu and any(s in renderer for s in SOFTWARE):
                print(f'ABORT: no hardware GPU ({renderer})', flush=True)
                failed.set()
                done.set()
        elif line.startswith('GALLERY_FAILED'):
            print(line[:800], flush=True)
            failed.set()
        elif line.startswith('GALLERY_DONE'):
            done.set()


    def read_stderr():
        for raw in proc.stderr:
            try:
                handle(raw)
            except Exception as e:  # never let one bad line stop the reader
                lines.append(f'DRIVER_LINE_ERROR {e!r} {raw.strip()[:300]}')


    threading.Thread(target=read_stderr, daemon=True).start()

    cpu_samples = []


    def chrome_pids():
        if not os.path.isdir('/proc'):
            return {}
        kids = {}
        for d in os.listdir('/proc'):
            if not d.isdigit():
                continue
            try:
                with open(f'/proc/{d}/stat') as f:
                    st = f.read()
                ppid = int(st[st.rindex(')') + 2:].split()[1])
                kids.setdefault(ppid, []).append(int(d))
            except (OSError, ValueError):
                pass
        out, stack = {}, [proc.pid]
        while stack:
            p = stack.pop()
            try:
                with open(f'/proc/{p}/cmdline', 'rb') as f:
                    # Chrome rewrites child argv into one space-joined string
                    argv = f.read().replace(b'\0', b' ').split()
            except OSError:
                continue
            typ = 'browser'
            for a in argv:
                if a.startswith(b'--type='):
                    typ = a[7:].decode()
                    if typ == 'utility':
                        for b in argv:
                            if b.startswith(b'--utility-sub-type='):
                                typ = 'utility:' + b.split(b'=', 1)[1].decode().split('.')[-1]
            out[p] = typ
            stack.extend(kids.get(p, []))
        return out


    TICK_MS = 1000.0 / os.sysconf('SC_CLK_TCK') if hasattr(os, 'sysconf') else 10.0


    def sample_cpu():
        # Rescanning /proc is the expensive part: refresh the pid set once a second
        pids, last_scan = {}, 0.0
        while not done.is_set():
            if time.monotonic() - last_scan > 1.0:
                pids, last_scan = chrome_pids(), time.monotonic()
            snap = {}
            for p, typ in pids.items():
                try:
                    with open(f'/proc/{p}/stat') as f:
                        st = f.read()
                    fields = st[st.rindex(')') + 2:].split()
                    snap[typ] = snap.get(typ, 0.0) + (int(fields[11]) + int(fields[12])) * TICK_MS
                except (OSError, ValueError, IndexError):
                    pass
            cpu_samples.append((time.monotonic() - t0, snap))
            time.sleep(0.1)


    async def cdp():
        ver = None
        for _ in range(100):
            try:
                ver = json.load(urllib.request.urlopen(
                    f'http://127.0.0.1:{args.cdp_port}/json/version', timeout=1))
                break
            except OSError:
                await asyncio.sleep(0.2)
        async with websockets.connect(ver['webSocketDebuggerUrl'], max_size=None) as ws:
            seq = 0
            pending = {}
            events = asyncio.Queue()

            async def reader():
                async for msg in ws:
                    m = json.loads(msg)
                    if 'id' in m and m['id'] in pending:
                        pending.pop(m['id']).set_result(m)
                    else:
                        await events.put(m)

            rt = asyncio.create_task(reader())

            async def call(method, params=None):
                nonlocal seq
                seq += 1
                fut = asyncio.get_event_loop().create_future()
                pending[seq] = fut
                await ws.send(json.dumps({'id': seq, 'method': method, 'params': params or {}}))
                return await fut

            page = None
            for _ in range(100):
                ts = json.load(urllib.request.urlopen(
                    f'http://127.0.0.1:{args.cdp_port}/json/list', timeout=1))
                page = next((t for t in ts
                             if t['type'] == 'page' and str(args.port) in t['url']), None)
                if page:
                    break
                await asyncio.sleep(0.1)
            console_ws = await websockets.connect(page['webSocketDebuggerUrl'], max_size=None)

            async def console_reader():
                async for msg in console_ws:
                    m = json.loads(msg)
                    if m.get('method') == 'Runtime.consoleAPICalled':
                        text = ' '.join(str(a.get('value', a.get('description', '')))
                                        for a in m['params']['args'])
                        if text.startswith('GALLERY_'):
                            try:
                                handle_console(text)
                            except Exception as e:
                                lines.append(f'DRIVER_LINE_ERROR {e!r} {text[:300]}')
                    elif m.get('method') == 'Runtime.exceptionThrown':
                        d = m['params']['exceptionDetails']
                        lines.append('PAGE_EXCEPTION ' + json.dumps(d)[:2000])
                        desc = d.get('exception', {}).get('description', d.get('text', ''))
                        print('PAGE_EXCEPTION ' + desc[:300], flush=True)

            crt = asyncio.create_task(console_reader())
            await console_ws.send(json.dumps({'id': 1, 'method': 'Runtime.enable'}))
            if args.cpu_throttle != 1.0:
                await console_ws.send(json.dumps({
                    'id': 2, 'method': 'Emulation.setCPUThrottlingRate',
                    'params': {'rate': args.cpu_throttle}}))
            await call('Tracing.start', {
                'transferMode': 'ReturnAsStream',
                'traceConfig': {'recordMode': 'recordAsMuchAsPossible',
                                'includedCategories': ['toplevel', 'blink.user_timing']},
            })
            threading.Thread(target=sample_cpu, daemon=True).start()
            # The page is opened from the command line: a second tab created over
            # CDP can end up in the background with throttled rAF and stall
            while not done.is_set() and time.monotonic() - t0 < args.timeout:
                last = marks[-1][0] if marks else 0.0
                if time.monotonic() - t0 - last > 90:
                    lines.append('DRIVER_STALL no phase mark for 90 s')
                    print('STALL', flush=True)
                    failed.set()
                    break
                await asyncio.sleep(0.2)
            await call('Tracing.end')
            while True:
                m = await events.get()
                if m.get('method') == 'Tracing.tracingComplete':
                    stream = m['params']['stream']
                    break
            with gzip.open(args.out_prefix + '.trace.json.gz', 'wb') as f:
                while True:
                    r = (await call('IO.read', {'handle': stream, 'size': 4 << 20}))['result']
                    data = r['data']
                    f.write(base64.b64decode(data) if r.get('base64Encoded') else data.encode())
                    if r.get('eof'):
                        break
            await call('IO.close', {'handle': stream})
            try:
                await asyncio.wait_for(call('Browser.close'), 5)
            except Exception:
                pass
            rt.cancel()
            crt.cancel()
            await console_ws.close()


    ok = True
    try:
        asyncio.run(cdp())
    except Exception as e:  # keep partial console output for diagnosis
        lines.append(f'DRIVER_ERROR {e!r}')
        ok = False
    try:
        proc.wait(10)
    except subprocess.TimeoutExpired:
        proc.kill()
    httpd.shutdown()
    # A fresh profile per run is the point; a run leaves ~50 MB behind otherwise
    shutil.rmtree(profile, ignore_errors=True)
    with open(args.out_prefix + '.json', 'w') as f:
        json.dump({'lines': lines, 'marks': marks, 'cpu': cpu_samples, 'mode': args.mode,
                   'config': {'dpr': args.dpr, 'cpuThrottle': args.cpu_throttle,
                              'chromeFlags': args.chrome_flag}}, f)
    sys.exit(0 if ok and done.is_set() and not failed.is_set() else 1)


if __name__ == '__main__':
    main()
