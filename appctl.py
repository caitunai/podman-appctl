#!/usr/bin/env python3
# appctl V4.2 — Compose -> Podlet -> Quadlet -> systemd (Rootless Podman)
#
# Usage (run as the configured non-root deployment account):
#   appctl deploy APP [--pull] [--force]
#   appctl {start,stop,restart,status} APP [SERVICE]
#   appctl history APP [--limit N]
#   appctl gc
#   appctl --help | --version
#   Global output flags: --verbose/-v, --no-color (also accepted after command).
#
# Environment configuration (set before invoking appctl):
#   APPCTL_USER=www                 Expected non-root account; does not switch users.
#   APPCTL_BASE_DIR=/var/www         Absolute root containing APP/compose.yml.
#   APPCTL_PODMAN_VERSION=5.4        Podlet compatibility target (major.minor).
#   Unset variables use the defaults above; empty or invalid values are rejected.
#   The target must be supported by Podlet and compatible with installed Podman.
#   State, ownership and locking remain per deployment account (XDG directories).
#   Use consistent settings across invocations; changing accounts does not migrate state.
#
# Features:
#   * Reads APPCTL_BASE_DIR/APP/compose.yml; generates .container/.network/.volume
#     via Podlet for APPCTL_PODMAN_VERSION. No podman compose up/down.
#   * Immutable image tags: use local cache; --pull checks registry even if cached.
#   * Per-service fingerprints: Quadlet, image ID, regular bind-mounted files,
#     EnvironmentFile, and local Quadlet drop-ins. No recursive data-dir hashing.
#   * Updates only changed/inactive services; --force restarts all desired services.
#   * Atomic state/file writes, global flock, ownership manifest, crash retry.
#   * Stops/removes deleted owned services and prunes their current state.
#   * Detects cross-project unit/resource names and existing unmanaged units/objects.
#   * Fresh installations only: rejects unmanaged Quadlets and runtime objects.
#   * start/stop/restart/status operate only on already deployed project-owned
#     container units; optionally select one Compose service (Quadlet filename stem).
#     No compose conversion, image pull, daemon-reload, or deletion of definitions.
#     Unit source, identity and existing container ownership are checked first.
#     systemd dependencies may affect other units. Stop is temporary: deploy or
#     a later boot may start the service again. Quadlet can remove stopped runtime
#     containers; images, bind data and named volumes remain. No persistent disable.
#   * start/restart use installed unit definitions, not pending Compose changes;
#     may read current bind files. Last successful deploy fingerprint is retained.
#     status displays systemd state and actual container state; does not deploy.
#   * JSON history for deploy/start/stop/restart, including failure and no-op;
#     read-only status/history calls are not logged. No config values in history.
#     Retain 90 days AND at most 100 attempts/project (constants below).
#     GC runs on every mutating deployment/control, across all projects.
#   * State: $XDG_STATE_HOME/appctl (default ~/.local/state/appctl).
#     State/ownership is not aged out; deleted service state is pruned.
#   * Operator output: phase headings, per-service change reasons, concise image
#     names, status/history tables, elapsed time and operation totals.
#     Color only on a TTY; NO_COLOR / --no-color disables ANSI. --verbose shows
#     child commands and non-query output. Failures show diagnostics and journalctl hints.
#     No secrets or config values intentionally stored in operation history.
#   * Preserves named volumes, networks, bind data and images. Removed resource
#     definitions stay reserved; changed network/volume definitions require manual migration.
#   * No HTTP health check, automatic rollback, or blue/green. Success means systemd
#     active and expected container running; not business readiness.
#   * Scope: ordinary long-running, image-based services, static literal names,
#     explicit absolute bind paths (Podlet resolves relative host paths), normal
#     Volume/EnvironmentFile and Mount=type=bind. No pods/builds/oneshot/template
#     units, custom aliases, arbitrary PodmanArgs/GlobalArgs, or dynamic specifiers
#     in resource names/config paths. Do not put credentials in image references.
#     Put identity, mounts and lifecycle in Compose, not local drop-ins.
#     Config directories, secret values, remote dependencies, ports across projects,
#     and arbitrary external runtime changes are not comprehensively audited.
#   * Install as root-owned /usr/local/bin/appctl; Python 3 stdlib only.
#     Requires podlet, podman, systemctl and deployment user linger/session bus.
#     Never use concurrently with another deployment tool or edit units during deploy.

import argparse
import csv
import datetime as dt
import fcntl
import pwd
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
from collections import Counter
import subprocess
import sys
import tempfile
import time
import uuid

VERSION = '4.2'
DEFAULT_USER = 'www'
DEFAULT_BASE_DIR = '/var/www'
DEFAULT_PODMAN_VERSION = '5.4'
DEPLOY_USER = DEFAULT_USER
BASE = Path(DEFAULT_BASE_DIR)
HOME = Path.home()
QUAD = Path(os.environ.get('XDG_CONFIG_HOME', HOME / '.config')) / 'containers/systemd'
ROOT = Path(os.environ.get('XDG_STATE_HOME', HOME / '.local/state')) / 'appctl'
DAYS = 90
MAX_HISTORY = 100
PODMAN_VERSION = DEFAULT_PODMAN_VERSION
NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]*\Z')
TYPES = {'.container': ('Container', 'ContainerName', ''),
         '.network': ('Network', 'NetworkName', '-network'),
         '.volume': ('Volume', 'VolumeName', '-volume')}

class DeployError(Exception): pass

class CommandError(DeployError):
    def __init__(self, args, code, stdout, stderr):
        self.args_run = args
        self.details = '\n'.join(x.strip() for x in (stdout, stderr) if x and x.strip())
        super().__init__(f'{shlex.join(args)} failed (exit {code})')

class Output:
    def __init__(self):
        self.verbose = False
        self.color = sys.stdout.isatty() and 'NO_COLOR' not in os.environ and os.environ.get('TERM') != 'dumb'
    def style(self, text, code):
        return f'\033[{code}m{text}\033[0m' if self.color else text
    def header(self, action, app=None, service=None):
        target = ' / '.join(x for x in (app, service) if x)
        print(self.style(f'appctl {VERSION}', '1;36') + f'  |  {action.upper()}' + (f'  {target}' if target else ''), flush=True)
        print(self.style('-' * 64, '2'), flush=True)
    def phase(self, label):
        print('\n' + self.style(label, '1;36'), flush=True)
    def line(self, label, message, detail=None):
        colors = {'OK':'32','DONE':'32','START':'36','STOP':'36','RESTART':'36',
                  'PULL':'36','CACHE':'2','SKIP':'2','PLAN':'33','REMOVE':'33',
                  'WARN':'33','ERROR':'31','INFO':'36','INSTALL':'36','GC':'36'}
        print('  ' + self.style(f'{label:<8}', colors.get(label, '0')) + str(message), flush=True)
        if detail: print('           ' + self.style(str(detail), '2'), flush=True)
    def diagnostic(self, message, stream=None):
        stream = stream or sys.stderr
        for line in message.splitlines():
            print('    ' + line, file=stream, flush=True)
    def error(self, exc):
        print(f'  ERROR    {exc}', file=sys.stderr, flush=True)
        if isinstance(exc, CommandError) and exc.details:
            # Preserve diagnostic context on failure, without copying raw child
            # output into persistent history (it may contain sensitive values).
            self.diagnostic(exc.details)
    def table(self, headers, rows, colored=None):
        widths = [max(len(str(r[i])) for r in [headers, *rows]) for i in range(len(headers))]
        def format_row(r, header=False):
            values = []
            for i, v in enumerate(r):
                raw = str(v).ljust(widths[i])
                code = '1' if header else (colored or {}).get(str(v))
                values.append(self.style(raw, code) if code else raw)
            return '  ' + '  '.join(values).rstrip()
        print(format_row(headers, True))
        print('  ' + '  '.join('-' * w for w in widths))
        for r in rows: print(format_row(r))
    def summary(self, result, seconds, events):
        counts = Counter(e['action'] for e in events)
        fields = [('restarted','updated'),('started','started'),('stopped','stopped'),
                  ('removed','removed'),('skip','unchanged'),('image_pulled','pulled'),
                  ('image_cached','cached')]
        stats = '  |  '.join(f'{counts[k]} {label}' for k,label in fields if counts[k])
        print('\n' + self.style('-' * 64, '2'), flush=True)
        title = {'success':'SUCCESS','noop':'NO CHANGES','failed':'FAILED'}[result]
        print(self.style(title, '1;31' if result=='failed' else '1;32') + f'  |  {seconds:.2f}s' + (f'  |  {stats}' if stats else ''), flush=True)
        if result == 'failed':
            self.line('WARN','Deployment/operation may be partially applied; no automatic rollback.')

OUT = Output()

def fail(message): raise DeployError(message)


def configure():
    """Read and validate environment overrides before touching deployment state."""
    global DEPLOY_USER, BASE, PODMAN_VERSION
    user = os.environ.get('APPCTL_USER', DEFAULT_USER)
    base = os.environ.get('APPCTL_BASE_DIR', DEFAULT_BASE_DIR)
    version = os.environ.get('APPCTL_PODMAN_VERSION', DEFAULT_PODMAN_VERSION)
    if not user or any(c.isspace() for c in user):
        fail('APPCTL_USER must be a non-empty deployment account name')
    if not base or not Path(base).is_absolute() or not base.strip():
        fail('APPCTL_BASE_DIR must be a non-empty absolute path')
    if not re.fullmatch(r'[0-9]+\.[0-9]+', version):
        fail('APPCTL_PODMAN_VERSION must use major.minor format (e.g. 5.4)')
    # Do not switch identities or let environment overrides enable rootful use.
    if os.getuid() == 0 or os.geteuid() == 0:
        fail('Run appctl as a non-root deployment account, not root')
    actual_user = pwd.getpwuid(os.getuid()).pw_name
    if actual_user != user:
        fail(f'Run as {user} (APPCTL_USER); current user is {actual_user}')
    DEPLOY_USER, BASE, PODMAN_VERSION = user, Path(base), version

def now(): return dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')

def run(*args, ok=(0,), capture=True):
    # Capture noisy tools even in normal mode; show success warnings and full
    # failure diagnostics. Query exit codes (e.g. not-found) are expected.
    if OUT.verbose: OUT.line('INFO', '$ ' + shlex.join(args))
    p = subprocess.run(args, text=True, errors='replace', stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE)
    if p.returncode not in ok:
        raise CommandError(args, p.returncode, p.stdout, p.stderr)
    if OUT.verbose and not capture and p.stdout: OUT.diagnostic(p.stdout, sys.stdout)
    if p.stderr and (OUT.verbose or (p.returncode == 0 and not capture and args[0] != 'podman')):
        message=p.stderr if OUT.verbose else '\n'.join(line for line in p.stderr.splitlines() if not line.lstrip().startswith('Wrote to file:'))
        if message: OUT.diagnostic(message)
    return p

def system(*args, **kw): return run('systemctl', '--user', *args, **kw)

def atomic(path, data, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.appctl-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            os.fchmod(f.fileno(), mode)
            f.write(data)
            f.flush(); os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)

def save(path, obj):
    atomic(path, (json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=True)+'\n').encode())

def load(path, default):
    return json.loads(path.read_text()) if path.exists() else default

def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for part in iter(lambda: f.read(1024*1024), b''): h.update(part)
    return h.hexdigest()

def exists(kind, name):
    return run('podman', kind, 'exists', name, ok=(0,1)).returncode == 0

def unit_info(unit):
    p = system('show', '--property=LoadState,SourcePath,FragmentPath,NeedDaemonReload,ActiveState,SubState',
               unit, ok=(0,1,4))
    info = dict(line.split('=',1) for line in p.stdout.splitlines() if '=' in line)
    if not info.get('LoadState'): fail(f'Cannot inspect systemd unit {unit}')
    return info

def same_path(a, b): return bool(a) and Path(a).resolve() == Path(b).resolve()

def active(unit):
    return system('is-active', '--quiet', unit, ok=(0,3,4)).returncode == 0

def inspect_container(name):
    if not exists('container', name): return None
    return json.loads(run('podman','container','inspect',name).stdout)[0]

def assert_runtime(meta, allowed):
    kind, name, unit = meta['kind'], meta['resource'], meta['unit']
    if kind == 'container':
        obj = inspect_container(name)
        if obj is not None:
            labels = obj.get('Config', {}).get('Labels') or {}
            if not allowed or labels.get('PODMAN_SYSTEMD_UNIT') != unit:
                fail(f'Container name conflict or unverified owner: {name}')
        return obj
    if exists(kind, name) and not allowed:
        fail(f'Existing unmanaged {kind}: {name}')
    return None

def parse(path):
    sections = {}; group = None
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith(('#',';')): continue
        if line.startswith('[') and line.endswith(']'):
            group = line[1:-1]; sections.setdefault(group, {}); continue
        if group is None or '=' not in line or line.endswith('\\'):
            fail(f'Unsupported Quadlet syntax in {path.name}')
        k,v = line.split('=',1)
        sections[group].setdefault(k.strip(), []).append(v.strip())
    return sections

def one(section, key, default=''):
    values = section.get(key, [])
    if len(values)>1: fail(f'Duplicate {key} unsupported')
    return values[0] if values else default

def literal(s, label):
    if not NAME.fullmatch(s): fail(f'Unsupported static {label}: {s}')
    return s

def metadata(path):
    group, key, suffix = TYPES[path.suffix]
    parsed = parse(path); fields = parsed.get(group, {})
    if not fields: fail(f'Missing [{group}] in {path.name}')
    if any(k in fields for k in ('GlobalArgs','PodmanArgs','Pod')):
        fail(f'Unsupported raw arguments or pod in {path.name}')
    if any(k in parsed.get('Install', {}) for k in ('Alias',)):
        fail(f'Custom unit alias unsupported in {path.name}')
    stem = literal(path.stem, 'Quadlet name')
    unitbase = literal(one(fields, 'ServiceName', stem+suffix), 'unit name')
    resource = literal(one(fields,key,'systemd-'+(unitbase if group=='Container' else stem)), 'resource name')
    if group == 'Container':
        if one(parsed.get('Service',{}),'Type') == 'oneshot': fail('Oneshot unsupported')
        image = one(fields,'Image')
        if not image or any(c.isspace() for c in image) or '%' in image or image.endswith(('.image','.build')):
            fail(f'Expected literal image reference in {path.name}')
        if one(fields,'Pull','never') != 'never': fail(f'Use pull_policy: never in {path.name}')
        # Ensure the generator never performs an implicit registry pull.
        if 'Pull' not in fields: fail(f'Explicit pull_policy: never required in {path.name}')
    if group == 'Network' and one(fields,'NetworkDeleteOnStop','false').lower() in ('yes','true','1'):
        fail('NetworkDeleteOnStop is unsupported')
    return {'kind':group.lower(),'unit':unitbase+'.service','resource':resource}

def dropins(path):
    # Local global, prefix and exact Quadlet drop-ins. Fingerprints also include
    # local systemd service drop-ins. External admin drop-ins require --force.
    stem = path.stem; suffix = path.suffix
    dirs = [QUAD/(suffix[1:]+'.d'), QUAD/(path.name+'.d')]
    for i,c in enumerate(stem):
        if c == '-': dirs.append(QUAD/(stem[:i+1]+suffix+'.d'))
    service_dir = HOME/'.config/systemd/user'
    dirs += [service_dir/'service.d', service_dir/(metadata(path)['unit']+'.d')]
    paths = sorted({p for d in dirs for p in d.glob('*.conf')})
    for p in paths:
        section = parse(p)
        if any(k in section.get('Container', {}) for k in ('ContainerName','ServiceName','Image','Pull','Volume','Mount','EnvironmentFile','PodmanArgs','GlobalArgs','Pod')) or 'Network' in section or 'Volume' in section or any(k in section.get('Service',{}) for k in ('ExecStart','ExecStop','ExecStopPost','ExecStartPre')) or 'Alias' in section.get('Install',{}):
            fail(f'Identity, mount or lifecycle override in drop-in: {p}; move it into Compose')
    return paths

def file_inputs(path):
    fields = parse(path)['Container']; result = {}
    def track(s, optional=False, directory=True):
        if '%' in s or not s.startswith('/'): fail(f'Non-literal config path in {path.name}')
        p = Path(s)
        if p.is_file(): result[s] = digest(p)
        elif p.is_dir() and directory: return
        elif optional and not p.exists(): result[s] = 'missing-optional'
        else: fail(f'Missing/invalid config or bind source: {s}')
    for v in fields.get('Volume', []):
        v = v.strip('"')
        source = v.split(':',1)[0]
        if source.startswith('/'): track(source)
        elif source.startswith(('.', '~', '%')): fail(f'Unresolved bind path in {path.name}')
    for v in fields.get('EnvironmentFile', []):
        for s in shlex.split(v): track(s.lstrip('-'), s.startswith('-'), False)
    for v in fields.get('Mount', []):
        tokens = next(csv.reader([v])); opts = dict(t.split('=',1) for t in tokens if '=' in t)
        if opts.get('type') == 'bind':
            source = opts.get('source') or opts.get('src')
            if not source: fail(f'Missing Mount source in {path.name}')
            track(source)
    return result

def fingerprint_inputs(path, image_id):
    return {'quadlet':digest(path), 'image_id':image_id, 'files':file_inputs(path),
            'dropins':{str(p):digest(p) for p in dropins(path)}}

def fingerprint(path, image_id):
    return hash_inputs(fingerprint_inputs(path,image_id))

def hash_inputs(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

def changed_paths(old, new):
    return sorted(k for k in set(old)|set(new) if old.get(k)!=new.get(k))

def short_image(image):
    # Keep namespace/repository:tag for readability; full refs in verbose/history.
    return image.split('/',1)[-1] if '/' in image and ('.' in image.split('/')[0] or ':' in image.split('/')[0] or image.startswith('localhost/')) else image


def gc():
    cutoff = time.time()-DAYS*86400
    count = 0
    for project in ROOT.iterdir():
        if not project.is_dir() or project.is_symlink(): continue
        hist = project/'history'
        entries = sorted((p for p in hist.glob('*.json') if p.is_file()),
                         key=lambda p:p.name, reverse=True)
        for i,p in enumerate(entries):
            if i >= MAX_HISTORY or p.stat().st_mtime < cutoff:
                p.unlink(); count += 1
        # Only our own abandoned atomic-write temps, never live project manifests.
        for d in (project, hist):
            for p in d.glob('.appctl-*'):
                if p.is_file() and p.stat().st_mtime < cutoff: p.unlink(); count += 1
    return count

def check_collisions(app, desired, owned):
    keys = {}
    for project in ROOT.iterdir():
        if not project.is_dir() or project.name == app: continue
        other = load(project/'manifest.json', {}).get('files', {})
        for f,m in other.items():
            for k in [('file',f),('unit',m['unit']),(m['kind'],m['resource'])]:
                keys[k] = project.name
    seen = set()
    for f,m in desired.items():
        for k in [('file',f),('unit',m['unit']),(m['kind'],m['resource'])]:
            if k in keys: fail(f'Name conflict {k}: owned by project {keys[k]}')
            if k in seen: fail(f'Duplicate name in project: {k}')
            seen.add(k)
        # No rename/reuse of an existing owned identity within the same transaction.
        for oldf,old in owned.items():
            if oldf == f:
                if old != m: fail(f'Identity changed for {f}; manual migration required')
            elif old['unit']==m['unit'] or (old['kind'],old['resource'])==(m['kind'],m['resource']):
                fail(f'Name {f} conflicts with reserved {oldf}; remove it first')
    # Include all existing local Quadlets, even if not managed by appctl.
    roots = [QUAD, Path('/etc/containers/systemd/users') / str(os.getuid()),
             Path('/etc/containers/systemd/users')]
    if os.environ.get('XDG_RUNTIME_DIR'):
        roots.append(Path(os.environ['XDG_RUNTIME_DIR'])/'containers/systemd')
    # Admin-wide directory: do not descend into other users' numeric subdirectories.
    paths = set(QUAD.rglob('*'))
    for root in roots[1:]:
        if root == Path('/etc/containers/systemd/users'):
            paths.update(root.glob('*'))
        else: paths.update(root.rglob('*'))
    for p in paths:
        if not p.is_file() or p.suffix not in TYPES: continue
        if p.parent == QUAD and p.name in desired: continue
        if p.parent == QUAD and p.name in owned: continue
        # Only inspect literal identities; unrelated advanced units remain outside scope.
        try: m = metadata(p)
        except DeployError:
            group,key,suffix = TYPES[p.suffix]
            fields = parse(p).get(group,{})
            ub = one(fields,'ServiceName',p.stem+suffix)
            m = {'unit':ub+'.service','kind':group.lower(),
                 'resource':one(fields,key,'systemd-'+(ub if group=='Container' else p.stem))}
        if ('unit',m['unit']) in seen or (m['kind'],m['resource']) in seen:
            fail(f'Existing Quadlet name conflict: {p}')

    for f,m in {**owned,**desired}.items():
        dest=QUAD/f; info=unit_info(m['unit'])
        source_ok=same_path(info.get('SourcePath'),dest)
        if info['LoadState'] != 'not-found' and not source_ok:
            fail(f'Systemd unit conflict: {m["unit"]}')
        if dest.is_symlink(): fail(f'Refusing symlink: {dest}')
        if f in owned and dest.exists() and metadata(dest)!=owned[f]:
            fail(f'Managed Quadlet identity differs: {dest}; resolve manually')
        allowed = f in owned
        if dest.exists() and not allowed:
            fail(f'Unmanaged Quadlet: {dest}; refusing to overwrite. Resolve ownership manually.')
        elif not allowed and source_ok and info['LoadState']!='not-found':
            fail(f'Unmanaged generated unit: {m["unit"]}')
        assert_runtime(m, allowed)

def deploy(args, project, event):
    app=args.app; appdir=BASE/app; compose=appdir/'compose.yml'
    if not compose.is_file(): fail(f'Compose not found: {compose}')
    mp=project/'manifest.json'; sp=project/'state.json'
    manifest=load(mp,{'files':{},'reload_pending':False}); owned=manifest['files']
    state=load(sp,{})
    OUT.phase('[1/4] Generate & validate Quadlet')
    # Preflight all generated files and config sources before changing services.
    with tempfile.TemporaryDirectory(prefix='appctl-') as tmp:
        run('podlet','--podman-version',PODMAN_VERSION,'--file',tmp,
            '--absolute-host-paths',str(appdir),'--install','--wanted-by','default.target',
            '--skip-services-check','compose',str(compose),capture=False)
        desired_paths={p.name:p for p in Path(tmp).iterdir()}
        if not desired_paths or any(not p.is_file() or p.suffix not in TYPES for p in desired_paths.values()):
            fail('Unexpected Podlet output or no units')
        desired={f:metadata(p) for f,p in desired_paths.items()}
        containers={f:m for f,m in desired.items() if m['kind']=='container'}
        if not containers: fail('No container units generated; empty project not supported')
        check_collisions(app,desired,owned)
        # Retain resource definitions and ownership even if removed from Compose.
        for f,m in desired.items():
            dest=QUAD/f
            if m['kind']!='container' and dest.exists() and dest.read_bytes()!=desired_paths[f].read_bytes():
                fail(f'Network/volume changed: {f}; manual migration required')
        # All new config paths must be valid before any pull or stop.
        for f in containers: file_inputs(desired_paths[f])
        OUT.line('OK', f'{len(containers)} service(s), {len(desired)-len(containers)} resource definition(s) validated')
        OUT.phase('[2/4] Resolve images & build deployment plan')
        plans={}; images={}; reload_needed=False
        for f,m in sorted(containers.items()):
            p=desired_paths[f]; image=one(parse(p)['Container'],'Image')
            if image not in images:
                if args.pull or not exists('image',image):
                    OUT.line('PULL', short_image(image))
                    run('podman','pull','--quiet','--policy','always',image,capture=False)
                    event('image_pulled',image=image)
                else: event('image_cached',image=image)
                images[image]=run('podman','image','inspect','--format','{{.Id}}',image).stdout.strip()
                if not images[image]: fail('Empty image ID')
            image_id=images[image]; inputs=fingerprint_inputs(p,image_id); fp=hash_inputs(inputs)
            obj=inspect_container(m['resource']); info=unit_info(m['unit'])
            running=bool(obj and obj.get('State',{}).get('Running') and
                         obj.get('Image','').removeprefix('sha256:')==image_id.removeprefix('sha256:'))
            prev=state.get(f,{})
            same= (QUAD/f).is_file() and (QUAD/f).read_bytes()==p.read_bytes()
            reasons=[]
            details=[]
            if args.force: reasons.append('forced')
            if not prev:
                reasons.append('new service' if f not in owned and not same else 'incomplete deployment state')
                if inputs['dropins']: reload_needed=True
            elif prev.get('fingerprint')!=fp:
                old=prev.get('inputs',{})
                if prev.get('image_id')!=image_id or prev.get('image')!=image: reasons.append('image changed')
                if old.get('quadlet')!=inputs['quadlet']: reasons.append('Quadlet changed')
                for key, title in [('files','config'),('dropins','drop-in')]:
                    paths=changed_paths(old.get(key,{}),inputs[key])
                    if paths:
                        reasons.append(title+' changed')
                        details.extend(paths)
                        if key=='dropins': reload_needed=True
                if not reasons: reasons.append('deployment fingerprint changed')
            if prev and not same and 'Quadlet changed' not in reasons: reasons.append('Quadlet differs')
            if (prev or same) and (not running or not active(m['unit'])): reasons.append('not running expected image')
            if info.get('NeedDaemonReload')=='yes':
                reasons.append('systemd reload pending'); reload_needed=True
            update=bool(reasons)
            plans[f]={'fingerprint':fp,'inputs':inputs,'image':image,'image_id':image_id,'update':update}
            event('plan_update' if update else 'skip',unit=m['unit'],image=image,image_id=image_id,
                  reason=', '.join(reasons) if update else 'unchanged',paths=details)
        stale={f:m for f,m in owned.items() if f not in desired and m['kind']=='container'}
        changed=[f for f,p in desired_paths.items() if not (QUAD/f).is_file() or (QUAD/f).read_bytes()!=p.read_bytes()]
        pending=manifest.get('reload_pending',False)
        reload_needed=bool(reload_needed or changed or stale or pending)
        updates=[f for f in plans if plans[f]['update']]
        for f,m in stale.items(): OUT.line('PLAN',m['unit'],'removed from Compose')
        OUT.line('INFO',f'{len(updates)} to update, {len(plans)-len(updates)} unchanged, {len(stale)} to remove')
        if not (changed or stale or updates or pending):
            return 'noop'
        OUT.phase('[3/4] Apply changes')
        # Reserve names and persist a reload marker BEFORE modifying anything.
        manifest={'files':{**owned,**desired},'reload_pending':reload_needed}
        save(mp,manifest)
        for f in updates: state.pop(f,None)
        save(sp,state)
        # Stop deleted services before unlinking; never podman rm -f by name.
        for f,m in stale.items():
            event('remove_begin',unit=m['unit'])
            if unit_info(m['unit'])['LoadState']!='not-found': system('stop',m['unit'],capture=False)
            if exists('container',m['resource']): fail(f'Container remains after stop: {m["resource"]}')
            (QUAD/f).unlink(missing_ok=True)
            state.pop(f,None); save(sp,state)
            event('removed',unit=m['unit'])
        for f in changed:
            atomic(QUAD/f,desired_paths[f].read_bytes())
            event('installed',file=f)
        if reload_needed:
            OUT.line('INFO','Reloading systemd user manager')
            system('daemon-reload',capture=False)
        else: OUT.line('SKIP','systemd daemon-reload not required')
        # Check generated source paths before restarting anything.
        for f,m in desired.items():
            info=unit_info(m['unit'])
            if info['LoadState']!='loaded' or not same_path(info.get('SourcePath'),QUAD/f):
                fail(f'Quadlet generation failed or shadowed: {m["unit"]}')
        # Release removed names only after successful reload and unit removal.
        for f,m in stale.items():
            info=unit_info(m['unit'])
            if info['LoadState']!='not-found': fail(f'Removed unit still loaded: {m["unit"]}')
            manifest['files'].pop(f,None)
        manifest['reload_pending']=False; save(mp,manifest)
        OUT.phase('[4/4] Start / restart & verify')
        if not updates: OUT.line('SKIP','No service restart required')
        # Start/restart changed services only; no rollback on failure.
        for f in updates:
            m=containers[f]; plan=plans[f]
            action='restart' if active(m['unit']) else 'start'
            event(action+'_begin',unit=m['unit'],image=plan['image'],image_id=plan['image_id'])
            system(action,m['unit'],capture=False)
            if not active(m['unit']): fail(f'Service not active: {m["unit"]}')
            obj=assert_runtime(m,True)
            if not obj or not obj.get('State',{}).get('Running') or obj.get('Image','').removeprefix('sha256:')!=plan['image_id'].removeprefix('sha256:'):
                fail(f'Container not running expected image: {m["resource"]}')
            # Detect config mutation during deployment. No stale fingerprint recorded.
            if fingerprint(desired_paths[f],plan['image_id'])!=plan['fingerprint']:
                fail(f'Config changed during deployment: {m["unit"]}; retry')
            state[f]={k:v for k,v in plan.items() if k!='update'}
            state[f]['deployed_at']=now(); save(sp,state)
            event('restarted' if action=='restart' else 'started',unit=m['unit'],image=plan['image'],image_id=plan['image_id'])
        # State contains only desired, successfully deployed services.
        state={f:v for f,v in state.items() if f in containers}; save(sp,state)
        return 'success'

def selected_units(args, project):
    mp = project/'manifest.json'
    if not mp.is_file():
        fail(f'Project not managed: {args.app}; run appctl deploy first')
    manifest = load(mp, {})
    if manifest.get('reload_pending') and args.command in ('start','restart'):
        fail('Previous deployment incomplete; run appctl deploy first')
    files = manifest.get('files', {})
    selected = {f:m for f,m in files.items() if m['kind']=='container'}
    if args.service:
        name = args.service + '.container'
        if name not in selected:
            fail(f'Unknown service {args.service} in {args.app}; available: '+
                 ', '.join(Path(f).stem for f in sorted(selected)))
        selected = {name:selected[name]}
    if not selected:
        fail(f'No managed container services in {args.app}')
    # Verify every selected identity before any mutating systemctl call.
    for other in ROOT.iterdir():
        if not other.is_dir() or other == project: continue
        for f,m in load(other/'manifest.json',{}).get('files',{}).items():
            for sel,sm in selected.items():
                if f == sel or m['unit'] == sm['unit'] or (m['kind'],m['resource']) == (sm['kind'],sm['resource']):
                    fail(f'Ownership conflict: {sel} / {other.name}')
    for f,m in sorted(selected.items()):
        dest = QUAD/f
        if dest.is_symlink(): fail(f'Refusing symlink: {dest}')
        info = unit_info(m['unit'])
        if info['LoadState'] != 'not-found' and not same_path(info.get('SourcePath'),dest):
            fail(f'Systemd unit conflict: {m["unit"]}')
        # Status can report incomplete/missing units. Mutations require valid units.
        if args.command != 'status':
            if not dest.is_file() or metadata(dest) != m or info['LoadState'] != 'loaded':
                fail(f'Missing or changed managed unit {m["unit"]}; run appctl deploy first')
            if args.command in ('start','restart'):
                if info.get('NeedDaemonReload')=='yes':
                    fail(f'Unit needs daemon-reload: {m["unit"]}; run appctl deploy first')
                # Prevent local identity/mount/lifecycle overrides; no file hashing here.
                dropins(dest)
        assert_runtime(m, True)
    return dict(sorted(selected.items()))


def runtime_status(meta):
    info = unit_info(meta['unit'])
    obj = assert_runtime(meta, True)
    return info, obj


def show_status(args, project):
    selected = selected_units(args, project)
    OUT.header('status',args.app,args.service)
    rows = []
    deployed=load(project/'state.json',{})
    for f,m in selected.items():
        info,obj = runtime_status(m)
        sd = info.get('ActiveState','unknown')+'/'+info.get('SubState','unknown')
        if info.get('LoadState') != 'loaded': sd = info.get('LoadState','unknown')
        if info.get('NeedDaemonReload')=='yes': sd += '*'
        state = (obj or {}).get('State',{})
        cs = ('running' if state.get('Running') else state.get('Status','stopped')) if obj else 'absent'
        iid = (obj or {}).get('Image','').removeprefix('sha256:')[:12] or '-'
        image=(obj or {}).get('ImageName') or deployed.get(f,{}).get('image','-')
        rows.append((Path(f).stem,sd,cs,short_image(image),iid))
    OUT.table(('SERVICE','SYSTEMD','CONTAINER','IMAGE','IMAGE ID'),rows,
              {'active/running':'32','running':'32','inactive/dead':'2','absent':'2','failed/failed':'31','not-found':'33'})
    if any('*' in r[1] for r in rows): OUT.line('WARN','* systemd reports daemon-reload needed')
    running=sum(r[1].rstrip('*')=='active/running' and r[2]=='running' for r in rows)
    OUT.line('INFO',f'{running}/{len(rows)} services running; image tag for absent containers is last deployed')
    return 0


def control(args, project, event):
    action = args.command
    selected = selected_units(args, project)
    OUT.phase('Resolve managed services')
    targets = []
    for f,m in selected.items():
        info,obj = runtime_status(m)
        running = bool(obj and obj.get('State',{}).get('Running'))
        if action == 'start' and info.get('ActiveState')=='active' and running:
            event('skip',unit=m['unit'],reason='already running')
        elif action == 'stop' and info.get('ActiveState')=='inactive' and not running:
            event('skip',unit=m['unit'],reason='already stopped')
        else:
            targets.append(m)
    if not targets: return 'noop'
    OUT.phase('Apply & verify')
    for m in targets: event(action+'_begin',unit=m['unit'])
    # Submit the selected units in one systemd transaction; honor dependencies.
    system(action,*(m['unit'] for m in targets),capture=False)
    # Verify all selected units, including ones initially skipped (dependencies).
    for m in selected.values():
        info,obj = runtime_status(m)
        running = bool(obj and obj.get('State',{}).get('Running'))
        if action == 'stop':
            if info.get('ActiveState') not in ('inactive','failed') or running:
                fail(f'Service did not stop: {m["unit"]}')
        elif info.get('ActiveState')!='active' or not running:
            fail(f'Service not active or container not running: {m["unit"]}')
        if m in targets:
            event({'start':'started','stop':'stopped','restart':'restarted'}[action],unit=m['unit'])
    # Operational actions do not claim a new successful Compose deployment.
    return 'success'


def parse_args(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(
        prog='appctl', description='Compose / Podlet / Quadlet application management',
        epilog=(f'Environment: APPCTL_USER (default: {DEFAULT_USER}), '
                f'APPCTL_BASE_DIR (default: {DEFAULT_BASE_DIR}), '
                f'APPCTL_PODMAN_VERSION (default: {DEFAULT_PODMAN_VERSION}).'))
    parser.add_argument('--version',action='version',version='appctl '+VERSION)
    def output_flags(p, default):
        p.add_argument('--verbose','-v',action='store_true',default=default,help='Show child commands and detailed operation output')
        p.add_argument('--no-color',action='store_true',default=default,help='Disable ANSI colors')
    output_flags(parser, False)
    subs=parser.add_subparsers(dest='command',required=True)
    p=subs.add_parser('deploy',help='Incrementally deploy a project from compose.yml')
    p.add_argument('app',help=f'Project directory under APPCTL_BASE_DIR (default: {DEFAULT_BASE_DIR})')
    p.add_argument('--pull',action='store_true',help='Check registry even for cached images')
    p.add_argument('--force',action='store_true',help='Restart all desired services')
    for action,helptext in [('start','Start deployed services'),('stop','Temporarily stop deployed services'),('restart','Restart deployed services'),('status','Show systemd and Podman runtime state')]:
        p=subs.add_parser(action,help=helptext)
        p.add_argument('app',help=f'Project directory under APPCTL_BASE_DIR (default: {DEFAULT_BASE_DIR})')
        p.add_argument('service',nargs='?',help='Compose service name (default: all owned container services)')
    p=subs.add_parser('history',help='List retained deployment and operation history')
    p.add_argument('app')
    p.add_argument('--limit',type=int,default=20,help='Maximum entries to list (default: 20)')
    subs.add_parser('gc',help='Prune expired/excess history across all projects')
    for sub in subs.choices.values(): output_flags(sub,argparse.SUPPRESS)
    args=parser.parse_args(argv)
    if getattr(args,'limit',1)<1: parser.error('--limit must be positive')
    for field in ('app','service'):
        val=getattr(args,field,None)
        if val is not None and not NAME.fullmatch(val): parser.error(f'Invalid {field} name')
    return args


def main(argv=None):
    args=parse_args(argv)
    OUT.verbose=args.verbose
    if args.no_color: OUT.color=False
    configure()
    if OUT.verbose:
        OUT.line('INFO', f'User: {DEPLOY_USER} | Base: {BASE} | Podman target: {PODMAN_VERSION}')
    if args.command not in ('history','gc'):
        required=['podman','systemctl'] + (['podlet'] if args.command=='deploy' else [])
        for cmd in required:
            if not shutil.which(cmd): fail(f'Required command not found: {cmd}')
    os.umask(0o077); ROOT.mkdir(parents=True,exist_ok=True)
    os.environ.setdefault('REGISTRY_AUTH_FILE',str(HOME/'.config/containers/auth.json'))
    if args.command not in ('status','history'):
        OUT.header(args.command,getattr(args,'app',None),getattr(args,'service',None))
    started=time.monotonic()
    with (ROOT/'deploy.lock').open('a') as lock:
        try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            OUT.line('INFO','Waiting for another appctl operation to finish')
            fcntl.flock(lock,fcntl.LOCK_EX)
        if args.command=='gc':
            count=gc(); OUT.line('GC',f'{count} expired/excess history or temporary file(s) removed')
            OUT.summary('success' if count else 'noop',time.monotonic()-started,[]); return 0
        project=ROOT/args.app
        if args.command=='history':
            OUT.header('history',args.app)
            rows=[]
            for p in sorted((project/'history').glob('*.json'),reverse=True)[:args.limit]:
                h=load(p,{})
                rows.append((h.get('started_at','-'),h.get('command','-'),h.get('service') or '*',h.get('result','-'),f'{h["elapsed_seconds"]:.2f}s' if 'elapsed_seconds' in h else '-',p.stem))
            if rows:
                OUT.table(('STARTED (UTC)','COMMAND','SERVICE','RESULT','TIME','RECORD'),rows,{'success':'32','failed':'31','noop':'2','running':'33'})
                OUT.line('INFO',f'{len(rows)} record(s); details: {project / "history"}')
            else: OUT.line('INFO','No retained history')
            return 0
        if args.command=='status': return show_status(args,project)
        gc()
        if args.command=='deploy': QUAD.mkdir(parents=True,exist_ok=True)
        project.mkdir(parents=True,exist_ok=True)
        hist=project/'history'/(dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'-'+uuid.uuid4().hex[:8]+'.json')
        record={'version':VERSION,'app':args.app,'command':args.command,'service':getattr(args,'service',None),
                'started_at':now(),'result':'running',
                'options':{k:getattr(args,k,False) for k in ('pull','force')},'events':[]}
        save(hist,record)
        def event(action,**fields):
            record['events'].append({'at':now(),'action':action,**fields}); save(hist,record)
            unit=fields.get('unit',''); image=fields.get('image',''); reason=fields.get('reason')
            if action=='image_cached': OUT.line('CACHE',short_image(image))
            elif action=='image_pulled': OUT.line('OK','Image ready: '+short_image(image))
            elif action in ('plan_update','skip'):
                detail=reason
                if fields.get('paths'): detail=(detail or '')+' | '+', '.join(fields['paths'])
                OUT.line('PLAN' if action=='plan_update' else 'SKIP',unit,detail)
            elif action=='installed': OUT.line('INSTALL',fields['file'])
            elif action.endswith('_begin'): OUT.line(action.split('_')[0].upper(),unit)
            elif action in ('restarted','started','stopped','removed'):
                detail=short_image(image)+' @ '+fields['image_id'].removeprefix('sha256:')[:12] if image and fields.get('image_id') else None
                OUT.line('OK',f'{unit} {action}',detail)
            else: OUT.line('INFO',action+' '+unit)
        rc=0
        try:
            record['result']=deploy(args,project,event) if args.command=='deploy' else control(args,project,event)
        except (Exception, KeyboardInterrupt) as e:
            record['result']='failed'; rc=130 if isinstance(e,KeyboardInterrupt) else 1
            record['error']=str(e) if isinstance(e,DeployError) else type(e).__name__
            OUT.error(e if not isinstance(e,KeyboardInterrupt) else 'Interrupted by operator')
            if record['events']:
                last=next((x['unit'] for x in reversed(record['events']) if x['action'] in ('restart_begin','start_begin','stop_begin','remove_begin') and 'unit' in x),None)
                if last: OUT.line('INFO',f'Inspect: journalctl --user -u {last} -n 80 --no-pager')
            if OUT.verbose and not isinstance(e,(DeployError,KeyboardInterrupt)):
                import traceback
                traceback.print_exc()
        finally:
            record['finished_at']=now(); record['elapsed_seconds']=round(time.monotonic()-started,3)
            save(hist,record)
            cleaned=gc()
            if cleaned: OUT.line('GC',f'{cleaned} expired/excess file(s) removed')
        OUT.summary(record['result'],record['elapsed_seconds'],record['events'])
        if rc or OUT.verbose: OUT.line('INFO',f'History: {hist}')
        return rc

if __name__=='__main__':
    try: sys.exit(main())
    except KeyboardInterrupt:
        OUT.error('Interrupted by operator'); sys.exit(130)
    except (DeployError,OSError,ValueError) as e:
        OUT.error(e); sys.exit(1)
