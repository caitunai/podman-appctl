#!/usr/bin/env python3
# appctl V4.2 — Compose -> Podlet -> Quadlet -> systemd (Rootless Podman)
#
# Usage (run as the configured non-root deployment account):
#   appctl deploy APP [--pull] [--force] [--env-file FILE]
#   appctl {start,stop,restart,status} APP [SERVICE]
#   appctl remove APP [SERVICE] [--volumes] [--networks]
#   appctl {list,ps} [APP [SERVICE]]
#   appctl stats APP [SERVICE] [--no-stream] [--interval SECONDS | --processes]
#   appctl inspect APP [SERVICE] [--env-file FILE]
#   appctl logs APP [SERVICE] [-n N] [-f] [--since TIME] [--source journald|podman]
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
#   * Substitutes ${NAME} from the project's compose.env or --env-file.
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
#   * list (ps) shows deployed services and their runtime states across projects.
#   * stats shows Podman resource usage or a snapshot of container processes.
#   * inspect lists ports and mounts from the current Compose file.
#   * logs reads deployed containers' stdout and stderr from journald or Podman.
#   * remove uninstalls deployed services, with opt-in named volume and network cleanup.
#   * JSON history for deploy/start/stop/restart/remove, including failure and no-op;
#     read-only calls are not logged. No config values in history.
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
#   * Quadlet generator preflight and recovery of definitions after failed live
#     validation. No application-level health check or runtime rollback.
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
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
from collections import Counter
import subprocess
from graphlib import CycleError, TopologicalSorter
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
CONFIG = Path(os.environ.get('XDG_CONFIG_HOME') or HOME / '.config')
QUAD = CONFIG / 'containers/systemd'
ROOT = Path(os.environ.get('XDG_STATE_HOME', HOME / '.local/state')) / 'appctl'
DAYS = 90
MAX_HISTORY = 100
PODMAN_VERSION = DEFAULT_PODMAN_VERSION
NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]*\Z')
TYPES = {'.container': ('Container', 'ContainerName', ''),
         '.network': ('Network', 'NetworkName', '-network'),
         '.volume': ('Volume', 'VolumeName', '-volume')}
ADMIN_QUAD = Path('/etc/containers/systemd/users')

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
                  'WARN':'33','ERROR':'31','INFO':'36','INSTALL':'36','GC':'36','KEEP':'2'}
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
    def table(self, headers, rows, colored=None, *, stream=None):
        widths = [max(len(str(r[i])) for r in [headers, *rows]) for i in range(len(headers))]
        def format_row(r, header=False):
            values = []
            for i, v in enumerate(r):
                raw = str(v).ljust(widths[i])
                code = '1' if header else (colored or {}).get(str(v))
                values.append(self.style(raw, code) if code else raw)
            return '  ' + '  '.join(values).rstrip()
        print(format_row(headers, True), file=stream)
        print('  ' + '  '.join('-' * w for w in widths), file=stream)
        for r in rows: print(format_row(r), file=stream)
    def summary(self, result, seconds, events):
        counts = Counter(e['action'] for e in events)
        fields = [('restarted','updated'),('started','started'),('stopped','stopped'),
                  ('removed','removed'),('resource_removed','resources removed'),
                  ('skip','unchanged'),('image_pulled','pulled'),
                  ('image_cached','cached')]
        stats = '  |  '.join(f'{counts[k]} {label}' for k,label in fields if counts[k])
        print('\n' + self.style('-' * 64, '2'), flush=True)
        title = {'success':'SUCCESS','noop':'NO CHANGES','failed':'FAILED'}[result]
        print(self.style(title, '1;31' if result=='failed' else '1;32') + f'  |  {seconds:.2f}s' + (f'  |  {stats}' if stats else ''), flush=True)
        if result == 'failed':
            self.line('WARN','Deployment/operation may be partially applied. Check the operation history.')

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

def run(*args, ok=(0,), capture=True, env=None, input_text=None):
    # Capture noisy tools even in normal mode; show success warnings and full
    # failure diagnostics. Query exit codes (e.g. not-found) are expected.
    if OUT.verbose: OUT.line('INFO', '$ ' + shlex.join(args))
    p = subprocess.run(args, text=True, errors='replace', stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, env=env, input=input_text)
    if p.returncode not in ok:
        raise CommandError(args, p.returncode, p.stdout, p.stderr)
    if OUT.verbose and not capture and p.stdout: OUT.diagnostic(p.stdout, sys.stdout)
    if p.stderr and (OUT.verbose or (p.returncode == 0 and not capture and args[0] != 'podman')):
        message=p.stderr if OUT.verbose else '\n'.join(line for line in p.stderr.splitlines() if not line.lstrip().startswith('Wrote to file:'))
        if message: OUT.diagnostic(message)
    return p

def system(*args, **kw): return run('systemctl', '--user', *args, **kw)


def stream_command(command, env=None):
    if OUT.verbose:
        OUT.line('INFO', '$ ' + shlex.join(command))
    # Inherit stdout and stderr so native output can be displayed immediately.
    result = subprocess.run(command, env=env)
    if result.returncode == -signal.SIGINT:
        return 130
    return int(result.returncode != 0)


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
    p = system('show', '--property=LoadState,SourcePath,NeedDaemonReload,ActiveState,SubState,'
               'InvocationID,Job,After,Requires,Requisite,BindsTo,PartOf', unit, ok=(0,1,4))
    info = dict(line.split('=',1) for line in p.stdout.splitlines() if '=' in line)
    if not info.get('LoadState'): fail(f'Cannot inspect systemd unit {unit}')
    return info

def same_path(a, b): return bool(a) and Path(a).resolve() == Path(b).resolve()

def inspect_container(name):
    if not exists('container', name): return None
    return json.loads(run('podman','container','inspect',name).stdout)[0]


def assert_container_owner(name, allowed_units):
    container = inspect_container(name)
    if container is not None:
        labels = container.get('Config', {}).get('Labels') or {}
        if labels.get('PODMAN_SYSTEMD_UNIT') not in allowed_units:
            fail(f'Container name conflict or unverified owner: {name}')
    return container


def assert_runtime(meta, allowed):
    kind, name, unit = meta['kind'], meta['resource'], meta['unit']
    if kind == 'container':
        return assert_container_owner(name, (unit,) if allowed else ())
    if exists(kind, name) and not allowed:
        fail(f'Existing unmanaged {kind}: {name}')
    return None

def parse(path, data=None):
    sections = {}
    group = None
    continued = []
    for line in (path.read_text() if data is None else data.decode()).splitlines():
        line = line.strip()
        if not line or line.startswith(('#',';')): continue
        if line.endswith('\\'):
            continued.append(line[:-1])
            continue
        if continued:
            line = ' '.join([*continued, line])
            continued.clear()
        if line.startswith('[') and line.endswith(']'):
            group = line[1:-1]; sections.setdefault(group, {}); continue
        if group is None or '=' not in line:
            fail(f'Unsupported Quadlet syntax in {path.name}')
        k,v = line.split('=',1)
        sections[group].setdefault(k.strip(), []).append(v.strip())
    if continued: fail(f'Unterminated continuation in {path.name}')
    return sections

def one(section, key, default=''):
    values = section.get(key, [])
    if len(values)>1: fail(f'Duplicate {key} unsupported')
    return values[0] if values else default

def literal(s, label):
    if not NAME.fullmatch(s): fail(f'Unsupported static {label}: {s}')
    return s

def container_platform(fields, filename):
    if 'PodmanArgs' not in fields:
        return ''
    args = one(fields, 'PodmanArgs')
    platform = re.fullmatch(r'--platform(?:=|[ \t]+)(linux/[a-z0-9]+)', args)
    if not platform:
        fail(f'Unsupported PodmanArgs in {filename}; expected --platform linux/ARCH')
    return platform[1]


def metadata(path):
    group, key, suffix = TYPES[path.suffix]
    parsed = parse(path); fields = parsed.get(group, {})
    if not fields: fail(f'Missing [{group}] in {path.name}')
    if 'GlobalArgs' in fields or 'Pod' in fields or (group != 'Container' and 'PodmanArgs' in fields):
        fail(f'Unsupported raw arguments or pod in {path.name}')
    if any(k in parsed.get('Install', {}) for k in ('Alias',)):
        fail(f'Custom unit alias unsupported in {path.name}')
    stem = literal(path.stem, 'Quadlet name')
    unitbase = literal(one(fields, 'ServiceName', stem+suffix), 'unit name')
    resource = literal(one(fields,key,'systemd-'+(unitbase if group=='Container' else stem)), 'resource name')
    if group == 'Container':
        container_platform(fields, path.name)
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

def dropin_dirs(root, filename):
    stem, suffix = os.path.splitext(filename)
    return [root / (suffix[1:] + '.d'), root / (filename + '.d'),
            *(root / (stem[:i + 1] + suffix + '.d') for i, c in enumerate(stem) if c == '-')]


def validate_dropin(path, data, *, managed=False):
    sections = parse(path, data)
    container = sections.get('Container', {})
    service = sections.get('Service', {})
    if (set(container) & {'ContainerName', 'ServiceName', 'Image', 'Pull', 'Volume', 'Mount',
                         'EnvironmentFile', 'GlobalArgs', 'Pod'}
            or set(sections) & {'Network', 'Volume'}
            or set(service) & {'ExecStart', 'ExecStop', 'ExecStopPost', 'ExecStartPre'}
            or 'Alias' in sections.get('Install', {})):
        fail(f'Identity, mount or lifecycle override in drop-in: {path}; move it into Compose')
    if managed and (set(sections) - {'Unit', 'Service', 'Install', 'Container'}
                    or set(container) - {'PodmanArgs'}
                    or any(key == 'Type' or key.startswith('Exec') for key in service)):
        fail(f'Unsupported project drop-in setting: {path}')
    for value in container.get('PodmanArgs', []):
        flags = value.split()
        if not flags or any(not re.fullmatch(r'--memory=[0-9]+[bkmgBKMG]?|'
                                            r'--memory-swap=(?:-1|[0-9]+[bkmgBKMG]?)', flag)
                            for flag in flags):
            fail(f'Only --memory and --memory-swap size flags are supported in PodmanArgs: {path}')


def dropins(path, projected=None):
    # Local global, prefix and exact Quadlet drop-ins. Fingerprints also include
    # local systemd service drop-ins. External admin drop-ins require --force.
    dirs = dropin_dirs(QUAD, path.name)
    dirs += dropin_dirs(CONFIG / 'systemd/user', metadata(path)['unit'])
    projected = projected or {}
    paths = {p for d in dirs for p in d.glob('*.conf')}
    paths.update(p for p in projected if p.parent in dirs)
    hashes = {}
    for p in sorted(paths):
        data = projected[p] if p in projected else p.read_bytes()
        if data is not None:
            validate_dropin(p, data)
            hashes[str(p)] = hashlib.sha256(data).hexdigest()
    return hashes


def project_dropins(directory, containers):
    wanted = {}
    for filename in sorted(containers):
        folder = directory / (filename + '.d')
        if folder.is_symlink() or (folder.exists() and not folder.is_dir()):
            fail(f'Invalid project drop-in directory: {folder}')
        for source in sorted(folder.glob('*.conf')):
            if Path(filename).stem.endswith('-'):
                fail(f'Project drop-in directory would also match other services: {folder}')
            if source.suffix != '.conf' or source.is_symlink() or not source.is_file():
                fail(f'Invalid project drop-in file: {source}')
            data = source.read_bytes()
            validate_dropin(source, data, managed=True)
            wanted[QUAD / folder.name / source.name] = data
    return wanted


def installed_dropin(path):
    if path.parent.is_symlink() or (path.parent.exists() and not path.parent.is_dir()):
        fail(f'Invalid installed drop-in directory: {path.parent}')
    if path.is_symlink() or (path.exists() and not path.is_file()):
        fail(f'Invalid installed drop-in file: {path}')
    return path.read_bytes() if path.is_file() else None


def mount_options(value):
    options = {}
    for token in next(csv.reader([value])):
        key, separator, argument = token.partition('=')
        options[key] = argument if separator else None
    return options


def split_volume(value):
    source, separator, spec = value.strip('"').partition(':')
    if not separator:
        return '', source, ''
    target, _, options = spec.partition(':')
    return source, target, options


def volume_mounts(fields):
    for value in fields.get('Volume', []):
        source, _, _ = split_volume(value)
        if source:
            yield source
    for value in fields.get('Mount', []):
        options = mount_options(value)
        if options.get('type') == 'volume':
            source = options.get('source') or options.get('src')
            if source:
                yield source


def named_volumes(fields):
    return {source for source in volume_mounts(fields)
            if not source.startswith(('/', '.', '~', '%')) and not source.endswith('.volume')}


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
        source, _, _ = split_volume(v)
        if source.startswith('/'): track(source)
        elif source.startswith(('.', '~', '%')): fail(f'Unresolved bind path in {path.name}')
    for v in fields.get('EnvironmentFile', []):
        for s in shlex.split(v): track(s.lstrip('-'), s.startswith('-'), False)
    for v in fields.get('Mount', []):
        opts = mount_options(v)
        if opts.get('type') == 'bind':
            source = opts.get('source') or opts.get('src')
            if not source: fail(f'Missing Mount source in {path.name}')
            track(source)
    return result

def fingerprint_inputs(path, image_id, projected=None):
    return {'quadlet':digest(path), 'image_id':image_id, 'files':file_inputs(path),
            'dropins':dropins(path, projected)}

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

def load_manifest(path):
    manifest = load(path, {'files': {}, 'named_volumes': [], 'reload_pending': False})
    if (not isinstance(manifest, dict) or not isinstance(manifest.get('files'), dict)
            or not isinstance(manifest.get('named_volumes'), list)):
        fail(f'Invalid ownership manifest: {path}')
    return manifest


def owned_project_dropins(manifest):
    names = manifest.get('dropins', [])
    if not isinstance(names, list):
        fail('Invalid project drop-in ownership')
    owned = set()
    for name in names:
        path = Path(name) if isinstance(name, str) else Path()
        meta = manifest['files'].get(path.parent.stem, {})
        if (len(path.parts) != 2 or str(path) != name or path.parent.suffix != '.d'
                or meta.get('kind') != 'container' or not NAME.fullmatch(path.parent.stem)
                or Path(path.parent.stem).stem.endswith('-')
                or path.suffix != '.conf'):
            fail(f'Invalid managed drop-in: {name}')
        owned.add(QUAD / path)
    return owned


def volume_names(files):
    return {meta['resource'] for meta in files.values() if meta['kind'] == 'volume'}


def project_manifests(app=None):
    if not ROOT.exists():
        return
    for project in ROOT.iterdir():
        path = project / 'manifest.json'
        if project.name != app and path.is_file():
            yield project.name, load_manifest(path)


def runtime_quadlet_dir():
    runtime = os.environ.get('XDG_RUNTIME_DIR')
    return Path(runtime) / 'containers/systemd' if runtime else None


def shared_admin_entries():
    if ADMIN_QUAD.is_dir():
        yield from (path for path in ADMIN_QUAD.iterdir() if not path.name.isdecimal())


def check_collisions(app, desired, manifest, requested_volumes):
    owned = manifest['files']
    prior_claims = set(manifest['named_volumes'])
    keys = {}
    for project, other_manifest in project_manifests(app):
        for f,m in other_manifest['files'].items():
            for k in [('file',f),('unit',m['unit']),(m['kind'],m['resource'])]:
                keys[k] = project
        for name in other_manifest['named_volumes']:
            keys['volume', name] = project
    claims = prior_claims | requested_volumes
    seen = set()
    for f,m in desired.items():
        for k in [('file',f),('unit',m['unit']),(m['kind'],m['resource'])]:
            if k in keys: fail(f'Name conflict {k}: owned by project {keys[k]}')
            if k in seen: fail(f'Duplicate name in project: {k}')
            seen.add(k)
        # A renamed service may take over a container name from a removed service.
        for oldf,old in owned.items():
            if oldf == f:
                if old != m: fail(f'Identity changed for {f}; manual migration required')
                continue
            same_resource = (old['kind'], old['resource']) == (m['kind'], m['resource'])
            if old['unit'] == m['unit'] or (same_resource and (m['kind'] != 'container' or oldf in desired)):
                fail(f'Name {f} conflicts with reserved {oldf}; remove it first')
    owned_volumes = volume_names(owned)
    desired_volumes = volume_names(desired)
    reserved_volumes = prior_claims | owned_volumes | desired_volumes
    for name in sorted(claims):
        key = ('volume', literal(name, 'named volume'))
        if key in keys: fail(f'Named volume {name} is owned by project {keys[key]}')
        seen.add(key)
        if name not in reserved_volumes and exists('volume', name):
            fail(f'Existing unmanaged volume: {name}')
    # Include all existing local Quadlets, even if not managed by appctl.
    roots = [QUAD, ADMIN_QUAD / str(os.getuid()), runtime_quadlet_dir()]
    paths = {p for root in roots if root is not None for p in root.rglob('*')}
    for entry in shared_admin_entries():
        paths.add(entry)
        if entry.is_dir(): paths.update(entry.rglob('*'))
    for p in paths:
        if not p.is_file() or p.suffix not in TYPES: continue
        if p.parent == QUAD and p.name in desired: continue
        if p.parent == QUAD and p.name in owned: continue
        # Read effective identities without applying appctl's managed-unit restrictions.
        group, key, suffix = TYPES[p.suffix]
        fields = parse(p).get(group, {})
        unit = fields.get('ServiceName', [p.stem + suffix])[-1]
        default = 'systemd-' + (unit if group == 'Container' else p.stem)
        resource = fields.get(key, [default])[-1]
        if ('unit', unit + '.service') in seen or (group.lower(), resource) in seen:
            fail(f'Existing Quadlet name conflict: {p}')
        # A stopped unmanaged container may reference a volume that does not exist yet.
        if group == 'Container' and named_volumes(fields) & (claims | desired_volumes):
            fail(f'Named volume conflict with existing Quadlet: {p}')

    container_owners = {}
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
        if m['kind'] == 'container':
            owners = container_owners.setdefault(m['resource'], set())
            if allowed:
                owners.add(m['unit'])
        else:
            assert_runtime(m, allowed)
    # A pending handoff can reserve the same name for both the old and new
    # units. Only the units already registered for that name can own it.
    for name, owners in container_owners.items():
        assert_container_owner(name, owners)
    return claims


def generator_command():
    for parent in (Path('/usr/lib/systemd'), Path('/usr/local/lib/systemd'), Path('/lib/systemd')):
        for relative in ('user-generators/podman-user-generator', 'system-generators/podman-system-generator'):
            path = parent / relative
            if path.is_file() and os.access(path, os.X_OK):
                return str(path)
    for name in ('podman-user-generator', 'podman-system-generator', 'quadlet'):
        path = shutil.which(name)
        if path:
            return path
    fail('Podman Quadlet generator not found. Install Podman with systemd integration.')


def preflight_generator(desired, desired_paths, stale, dropin_changes):
    with tempfile.TemporaryDirectory(prefix='appctl-quadlet-') as tmp:
        temp = Path(tmp)
        staged = temp / 'quadlets'
        staged.mkdir()
        changed_dirs = {path.parent for path in dropin_changes}
        for entry in QUAD.iterdir():
            if entry not in changed_dirs and entry.name not in desired and entry.name not in stale:
                (staged / entry.name).symlink_to(entry.absolute())
        for directory in changed_dirs:
            staged_dir = staged / directory.name
            staged_dir.mkdir()
            if directory.is_dir():
                for path in directory.iterdir():
                    if path not in dropin_changes:
                        (staged_dir / path.name).symlink_to(path.absolute())
        for path, data in dropin_changes.items():
            if data is not None:
                (staged / path.relative_to(QUAD)).write_bytes(data)
        for filename, path in desired_paths.items():
            shutil.copyfile(path, staged / filename)
        shared = temp / 'admin'
        shared.mkdir()
        for entry in shared_admin_entries():
            (shared / entry.name).symlink_to(entry)
        roots = (runtime_quadlet_dir(), staged, shared, ADMIN_QUAD / str(os.getuid()))
        env = dict(os.environ, QUADLET_UNIT_DIRS=os.pathsep.join(str(p) for p in roots if p is not None))
        cmd = (generator_command(), '--user', '--dryrun')
        result = run(*cmd, env=env, ok=(0, 1))
        # The generator can report an error in another project's file and still
        # emit all of ours. Verify each requested output and its source.
        sources = {}
        unit = None
        for line in result.stdout.splitlines():
            if line.startswith('---') and line.endswith('.service---'):
                unit = Path(line[3:-3]).name
            elif unit is not None and line.startswith('SourcePath='):
                sources[unit] = line.partition('=')[2]
        for filename, meta in desired.items():
            if not same_path(sources.get(meta['unit']), staged / filename):
                if result.returncode:
                    raise CommandError(cmd, result.returncode, '', result.stderr)
                fail(f'Quadlet preflight could not generate {meta["unit"]} from {filename}')


def loaded_units(desired):
    units = {}
    for filename, meta in desired.items():
        unit = meta['unit']
        info = unit_info(unit)
        if info['LoadState'] != 'loaded' or not same_path(info.get('SourcePath'), QUAD / filename):
            fail(f'Quadlet generation failed or shadowed: {unit}')
        units[unit] = info
    return units


def reload_units():
    OUT.line('INFO', 'Reloading systemd user manager')
    system('daemon-reload', capture=False)


def install_quadlets(desired, changes, stale, reload_needed, event):
    previous = {}
    try:
        for path, data in changes.items():
            previous[path] = path.read_bytes() if path.is_file() else None
            filename = str(path.relative_to(QUAD))
            if data is None:
                path.unlink(missing_ok=True)
                event('dropin_removed', file=filename)
            else:
                atomic(path, data)
                event('installed', file=filename)
        if reload_needed: reload_units()
        else: OUT.line('SKIP', 'systemd daemon-reload not required')
        units = loaded_units(desired)
        container_units = {meta['unit']: units[meta['unit']] for meta in desired.values()
                           if meta['kind'] == 'container'}
        stale_units = {meta['unit'] for meta in stale.values()}
        for unit, info in container_units.items():
            required = {dep for key in ('Requires', 'Requisite', 'BindsTo') for dep in info.get(key, '').split()}
            removed = required & stale_units
            if removed:
                fail(f'{unit} requires removed unit(s): ' + ', '.join(sorted(removed)))
        return container_units, list(service_order(container_units))
    except (Exception, KeyboardInterrupt):
        if previous:
            try:
                for path, data in previous.items():
                    if data is None: path.unlink(missing_ok=True)
                    else: atomic(path, data)
                reload_units()
                OUT.line('INFO', 'Restored previous Quadlet definitions')
            except (DeployError, OSError) as error:
                OUT.line('WARN', f'Could not restore previous Quadlet definitions: {error}')
        raise


def service_order(units):
    graph = TopologicalSorter({unit: set(info.get('After', '').split()) & units.keys()
                               for unit, info in units.items()})
    try:
        graph.prepare()
    except CycleError as error:
        fail('Circular systemd ordering: ' + ' -> '.join(error.args[1]))
    # Prefer required services first when systemd leaves their order open.
    predecessors = {unit: {dep for key in ('Requires', 'Requisite', 'BindsTo', 'PartOf')
                           for dep in info.get(key, '').split()} & units.keys()
                    for unit, info in units.items()}
    pending = set(units)
    ready = set(graph.get_ready())
    while ready:
        candidates = sorted(ready)
        unit = next((name for name in candidates if not predecessors[name] & pending), candidates[0])
        yield unit
        pending.remove(unit)
        ready.remove(unit)
        graph.done(unit)
        ready.update(graph.get_ready())


def running_image(container, image_id):
    return bool(container and container.get('State', {}).get('Running') and
                container.get('Image', '').removeprefix('sha256:') == image_id.removeprefix('sha256:'))


def resolve_image(image, platform, refresh, event):
    def inspect_image(reference):
        fmt = '{{.Id}} {{.Os}}/{{.Architecture}}' if platform else '{{.Id}}'
        output = run('podman', 'image', 'inspect', '--format', fmt, reference).stdout.strip()
        image_id, _, detected = output.partition(' ')
        return image_id if not platform or detected == platform else ''

    if not refresh and exists('image', image):
        image_id = inspect_image(image)
        if image_id:
            event('image_cached', image=image)
            return image_id
        if not platform:
            fail(f'Empty image ID: {image}')
    OUT.line('PULL', short_image(image))
    # podman pull defaults to always, including versions without --policy.
    options = ('--platform', platform) if platform else ()
    result = run('podman', 'pull', '--quiet', *options, image, capture=False)
    reference = result.stdout.strip() if platform else image
    if not reference:
        fail(f'Podman returned no image ID for {image} ({platform})')
    image_id = inspect_image(reference)
    if not image_id:
        fail(f'Image {image} does not match platform {platform}' if platform else f'Empty image ID: {image}')
    event('image_pulled', image=image)
    return image_id


def verify_services(containers, plans):
    units = loaded_units(containers)
    for filename, meta in containers.items():
        unit = meta['unit']
        if units[unit].get('ActiveState') != 'active':
            fail(f'Service not active: {unit}')
        if units[unit].get('Job') not in (None, '', '0'):
            fail(f'Service still has a pending systemd job: {unit}')
        if not running_image(assert_runtime(meta, True), plans[filename]['image_id']):
            fail(f'Container not running expected image: {meta["resource"]}')


def invalidate_service(state, filename):
    previous = state.pop(filename, {})
    if 'deployed_at' in previous:
        state[filename] = {'deployed_at': previous['deployed_at']}


def stop_stale_containers(stale, registered_replacements, event):
    remaining = Counter(meta['resource'] for meta in stale.values())
    for filename, meta in stale.items():
        unit, resource = meta['unit'], meta['resource']
        event('remove_begin', unit=unit)
        info = unit_info(unit)
        if info['LoadState'] != 'not-found':
            if not same_path(info.get('SourcePath'), QUAD / filename):
                fail(f'Systemd unit conflict: {unit}')
            system('stop', unit, capture=False)
        remaining[resource] -= 1
        if remaining[resource]:
            continue
        replacement = registered_replacements.get(resource)
        if replacement:
            # A previous attempt may have started the replacement already.
            assert_runtime(replacement, True)
        elif exists('container', resource):
            fail(f'Container remains after stop: {resource}')


def apply_services(containers, units, order, plans, updates, state, state_path, event):
    filenames = {meta['unit']: filename for filename, meta in containers.items()}
    actions = {}
    for unit in order:
        filename = filenames[unit]
        meta, plan = containers[filename], plans[filename]
        info = unit_info(unit)
        if info.get('Job') not in (None, '', '0'):
            # Join a queued start/restart from an earlier dependency operation.
            system('start', unit, capture=False)
            info = unit_info(unit)
        running = info.get('ActiveState') == 'active' and running_image(assert_runtime(meta, True), plan['image_id'])
        if filename not in updates and running:
            continue
        action = 'restart' if units[unit].get('ActiveState') == 'active' else 'start'
        actions[filename] = action
        invocation = info.get('InvocationID', '').strip('0')
        if running and invocation and info['InvocationID'] != units[unit].get('InvocationID'):
            continue
        if filename not in updates:
            invalidate_service(state, filename)
            save(state_path, state)
        event(action + '_begin', unit=unit, image=plan['image'], image_id=plan['image_id'])
        system('restart' if info.get('ActiveState') == 'active' else 'start', unit, capture=False)
    return actions


def compose_path(app):
    compose = BASE / app / 'compose.yml'
    if not compose.is_file():
        fail(f'Compose not found: {compose}')
    return compose


def read_compose_env(path):
    values = {}
    for number, line in enumerate(path.read_text(encoding='utf-8-sig').splitlines(), 1):
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        key, separator, raw = line.lstrip().removeprefix('export ').partition('=')
        key = key.strip()
        if not separator or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key):
            fail(f'Expected NAME=VALUE in {path}:{number}')
        value = raw.lstrip()
        if value.startswith(('"', "'")):
            try:
                parts = shlex.split(value, comments=True)
            except ValueError:
                parts = []
            if len(parts) != 1:
                fail(f'Invalid quoted environment value in {path}:{number}')
            value = parts[0]
        else:
            value = re.split(r'[ \t]+#', raw, maxsplit=1)[0].strip()
        values[key] = value
    return values


def render_compose(compose, env_file):
    source = env_file if env_file is not None else compose.with_name('compose.env')
    if env_file is None and not source.exists() and not source.is_symlink():
        return None
    if not source.is_file():
        fail(f'Compose environment file not found or not a regular file: {source}')
    values = read_compose_env(source)
    text = compose.read_text(encoding='utf-8')

    def substitute(match):
        sigil, expression = match.groups()
        if sigil == '$$':
            return match.group()[1:]
        parts = re.fullmatch(r'([A-Za-z_][A-Za-z0-9_]*)(?:(:?[-?])([^{}]*))?', expression)
        if not parts:
            line = text.count('\n', 0, match.start()) + 1
            fail(f'Unsupported Compose substitution in {compose}:{line}; '
                 'use ${NAME}, ${NAME:-default}, or ${NAME:?message}')
        name, operator, operand = parts.groups()
        value = values.get(name)
        empty_is_missing = operator in (':-', ':?')
        if value is not None and (value != '' or not empty_is_missing):
            return value
        if operator in ('-', ':-'):
            return operand
        reason = f'empty in {source}' if value == '' else f'missing from {source}'
        detail = f': {operand}' if operand else ''
        fail(f'Compose variable {name} is {reason}{detail}')

    rendered = re.sub(r'(\$\$?)\{([^}\r\n]*)\}', substitute, text)
    return rendered if rendered != text else None


def generate_quadlets(compose, directory, env_file=None):
    rendered = render_compose(compose, env_file)
    run('podlet', '--podman-version', PODMAN_VERSION, '--file', directory,
        '--absolute-host-paths', str(compose.parent), '--install', '--wanted-by', 'default.target',
        '--skip-services-check', 'compose', str(compose) if rendered is None else '-',
        capture=False, input_text=rendered)
    return {path.name: path for path in Path(directory).iterdir()}


def deploy(args, project, event):
    app = args.app
    compose = compose_path(app)
    mp=project/'manifest.json'; sp=project/'state.json'
    manifest = load_manifest(mp)
    if manifest.get('remove_pending'):
        fail(removal_retry(args.app, manifest['remove_pending']))
    owned = manifest['files']
    state=load(sp,{})
    OUT.phase('[1/4] Generate & validate Quadlet')
    # Preflight all generated files and config sources before changing services.
    with tempfile.TemporaryDirectory(prefix='appctl-') as tmp:
        desired_paths = generate_quadlets(compose, tmp, args.env_file)
        if not desired_paths or any(not p.is_file() or p.suffix not in TYPES for p in desired_paths.values()):
            fail('Unexpected Podlet output or no units')
        desired={f:metadata(p) for f,p in desired_paths.items()}
        containers={f:m for f,m in desired.items() if m['kind']=='container'}
        if not containers: fail('No container units generated; empty project not supported')
        stale = {f: m for f, m in owned.items() if f not in desired and m['kind'] == 'container'}
        requested_volumes = {name for f in containers
                             for name in named_volumes(parse(desired_paths[f])['Container'])}
        claims = check_collisions(app, desired, manifest, requested_volumes)
        wanted_dropins = project_dropins(compose.parent, containers)
        owned_dropins = owned_project_dropins(manifest)
        stale_dropins = {path for path in owned_dropins if path.parent.stem in stale}
        dropin_changes = {}
        for path in sorted(owned_dropins | wanted_dropins.keys()):
            current = installed_dropin(path)
            if current is not None and path not in owned_dropins:
                fail(f'Unmanaged drop-in: {path}; move it aside before deploying')
            data = wanted_dropins.get(path)
            if path not in stale_dropins and data != current:
                dropin_changes[path] = data
        dropin_updates = {path.parent.stem for path in dropin_changes}
        wanted_names = sorted(str(path.relative_to(QUAD)) for path in wanted_dropins)
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
            p = desired_paths[f]
            fields = parse(p)['Container']
            image = one(fields, 'Image')
            platform = container_platform(fields, f)
            key = image, platform
            if key not in images:
                images[key] = resolve_image(image, platform, args.pull, event)
            image_id=images[key]; inputs=fingerprint_inputs(p,image_id,dropin_changes); fp=hash_inputs(inputs)
            obj=inspect_container(m['resource']); info=unit_info(m['unit'])
            running = info.get('ActiveState') == 'active' and running_image(obj, image_id)
            prev=state.get(f,{})
            same= (QUAD/f).is_file() and (QUAD/f).read_bytes()==p.read_bytes()
            reasons=[]
            details=[]
            if args.force: reasons.append('forced')
            if not prev.get('fingerprint'):
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
            if f in dropin_updates and 'drop-in changed' not in reasons:
                reasons.append('drop-in differs')
            if (prev or same) and not running: reasons.append('not running expected image')
            if info.get('NeedDaemonReload')=='yes':
                reasons.append('systemd reload pending'); reload_needed=True
            update=bool(reasons)
            plans[f]={'fingerprint':fp,'inputs':inputs,'image':image,'image_id':image_id,'update':update}
            event('plan_update' if update else 'skip',unit=m['unit'],image=image,image_id=image_id,
                  reason=', '.join(reasons) if update else 'unchanged',paths=details)
        changes = dict(dropin_changes)
        for filename, source in desired_paths.items():
            dest = QUAD / filename
            data = source.read_bytes()
            if not dest.is_file() or dest.read_bytes() != data:
                changes[dest] = data
        pending=manifest.get('reload_pending',False)
        reload_needed=bool(reload_needed or changes or pending)
        updates={f for f in plans if plans[f]['update']}
        for f,m in stale.items(): OUT.line('PLAN',m['unit'],'removed from Compose')
        OUT.line('INFO',f'{len(updates)} to update, {len(plans)-len(updates)} unchanged, {len(stale)} to remove')
        if not (reload_needed or stale or updates):
            if owned_dropins != wanted_dropins.keys():
                manifest['dropins'] = wanted_names
                save(mp, manifest)
            return 'noop'
        if reload_needed or stale:
            preflight_generator(desired, desired_paths, stale, dropin_changes)
        OUT.phase('[3/4] Apply changes')
        # Reserve names and persist a reload marker BEFORE modifying anything.
        manifest = {**manifest, 'files': {**owned, **desired}, 'named_volumes': sorted(claims),
                    'dropins': sorted(str(path.relative_to(QUAD)) for path in owned_dropins | wanted_dropins.keys()),
                    'reload_pending': bool(reload_needed or stale)}
        save(mp,manifest)
        container_units, order = install_quadlets(desired, changes, stale, reload_needed, event)
        # The replacement units are loaded. Old definitions stay available until
        # the replacement is running, even when it needs to reuse a released port.
        for f in updates: invalidate_service(state, f)
        save(sp,state)
        registered_replacements = {m['resource']: m for f, m in containers.items() if f in owned}
        stop_stale_containers(stale, registered_replacements, event)
        OUT.phase('[4/4] Start / restart & verify')
        actions = apply_services(containers, container_units, order, plans, updates, state, sp, event)
        if not actions: OUT.line('SKIP','No service restart required')
        verify_services(containers, plans)
        if stale:
            for f in stale: (QUAD/f).unlink(missing_ok=True)
            for path in sorted(stale_dropins):
                path.unlink(missing_ok=True)
                event('dropin_removed', file=str(path.relative_to(QUAD)))
            reload_units()
            loaded_units(desired)
            for f, m in stale.items():
                if unit_info(m['unit'])['LoadState'] != 'not-found':
                    fail(f'Removed unit still loaded: {m["unit"]}')
                manifest['files'].pop(f, None)
                event('removed', unit=m['unit'])
            verify_services(containers, plans)
        for f, action in actions.items():
            m=containers[f]; plan=plans[f]
            # Detect config mutation during deployment. No stale fingerprint recorded.
            if fingerprint(desired_paths[f],plan['image_id'])!=plan['fingerprint']:
                fail(f'Config changed during deployment: {m["unit"]}; retry')
            state[f]={k:v for k,v in plan.items() if k!='update'}
            state[f]['deployed_at']=now()
            event('restarted' if action=='restart' else 'started',unit=m['unit'],image=plan['image'],image_id=plan['image_id'])
        # State contains only desired, successfully deployed services.
        state={f:v for f,v in state.items() if f in containers}; save(sp,state)
        manifest['dropins'] = wanted_names
        manifest['reload_pending'] = False
        manifest['deployed_at'] = now()
        save(mp, manifest)
        return 'success'


def container_services(manifest):
    return {filename: meta for filename, meta in manifest.get('files', {}).items()
            if meta['kind'] == 'container'}


def select_container_service(services, app, service):
    if not service:
        return services
    filename = service + '.container'
    if filename not in services:
        fail(f'Unknown service {service} in {app}; available: ' +
             (', '.join(Path(f).stem for f in sorted(services)) or '(none)'))
    return {filename: services[filename]}


def managed_services(args, project):
    mp = project/'manifest.json'
    if not mp.is_file():
        fail(f'Project not managed: {args.app}; run appctl deploy first')
    manifest = load_manifest(mp)
    if manifest.get('remove_pending') and args.command in ('start', 'restart'):
        fail(removal_retry(args.app, manifest['remove_pending']))
    if manifest.get('reload_pending') and args.command in ('start','restart'):
        fail('Previous deployment incomplete; run appctl deploy first')
    selected = select_container_service(container_services(manifest), args.app, args.service)
    if not selected:
        fail(f'No managed container services in {args.app}')
    return manifest, dict(sorted(selected.items()))


def selected_units(args, project, runtime=None):
    manifest, selected = managed_services(args, project)
    files = manifest['files']
    # Verify every selected identity before any mutating systemctl call.
    other_manifests = dict(project_manifests(args.app))
    for other, other_manifest in other_manifests.items():
        for f,m in other_manifest['files'].items():
            for sel,sm in selected.items():
                if f == sel or m['unit'] == sm['unit'] or (m['kind'],m['resource']) == (sm['kind'],sm['resource']):
                    fail(f'Ownership conflict: {sel} / {other}')
    mounts = set()
    for f,m in selected.items():
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
                mounts.update(named_volumes(parse(dest)['Container']))
        container = assert_runtime(m, True)
        if runtime is not None:
            runtime[f] = (info, container)
    if mounts:
        unowned = mounts - (set(manifest['named_volumes']) | volume_names(files))
        if unowned:
            fail('Unregistered named volume(s); run appctl deploy first: ' + ', '.join(sorted(unowned)))
        for other, other_manifest in other_manifests.items():
            other_volumes = set(other_manifest['named_volumes']) | volume_names(other_manifest['files'])
            conflicts = mounts & other_volumes
            if conflicts:
                fail(f'Named volume owned by project {other}: ' + ', '.join(sorted(conflicts)))
    return selected


def runtime_status(meta):
    info = unit_info(meta['unit'])
    obj = assert_runtime(meta, True)
    return info, obj


def parse_timestamp(value):
    if not isinstance(value, str) or not value:
        return None
    # Podman emits RFC 3339 with up to nanosecond precision. Python 3.9
    # needs an explicit UTC offset and a supported fractional width.
    value = re.sub(r'\.(\d+)', lambda match: '.' + match[1][:6].ljust(6, '0'), value)
    if value.endswith('Z'):
        value = value[:-1] + '+00:00'
    try:
        timestamp = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    return timestamp if timestamp.tzinfo is not None and timestamp.year > 1 else None


def format_deployed(timestamp):
    return timestamp.astimezone(dt.timezone.utc).strftime('%Y-%m-%d %H:%M:%S') if timestamp else '-'


def format_uptime(seconds):
    if seconds is None:
        return '-'
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    return ' '.join(f'{value}{unit}' for value, unit in zip((days, hours, minutes, seconds), 'dhms') if value) or '0s'


def latest_deployment(project, manifest, service_dates):
    timestamp = parse_timestamp(manifest.get('deployed_at'))
    if timestamp:
        return timestamp
    # Existing deployments already have these records even if their manifest
    # predates the project timestamp. History may have been pruned.
    for path in sorted((project / 'history').glob('*.json'), reverse=True):
        try:
            record = load(path, {})
        except (OSError, ValueError):
            continue
        if isinstance(record, dict) and record.get('command') == 'deploy' and record.get('result') == 'success':
            timestamp = parse_timestamp(record.get('finished_at'))
            if timestamp:
                return timestamp
    return max((date for date in service_dates.values() if date is not None), default=None)


def deployment_times(project, manifest=None, containers=None):
    if manifest is None:
        try:
            manifest = load_manifest(project / 'manifest.json')
        except (DeployError, OSError, ValueError):
            manifest = {}
    try:
        state = load(project / 'state.json', {})
    except (OSError, ValueError):
        state = {}
    services = container_services(manifest)
    containers = dict(containers or {})
    missing = services.keys() - containers.keys()
    if missing and shutil.which('podman'):
        for filename in sorted(missing):
            try:
                containers[filename] = assert_runtime(services[filename], True)
            except (DeployError, OSError, ValueError, IndexError):
                continue
    observed = time.time()
    deployed = {}
    uptimes = {}
    for filename in services:
        deployed[filename] = parse_timestamp(state.get(filename, {}).get('deployed_at'))
        runtime = (containers.get(filename) or {}).get('State') or {}
        started = parse_timestamp(runtime.get('StartedAt')) if runtime.get('Running') else None
        uptimes[filename] = max(0, int(observed - started.timestamp())) if started else None
    project_deployed = latest_deployment(project, manifest, deployed)
    project_uptime = min(uptimes.values()) if uptimes and None not in uptimes.values() else None
    project_times = format_deployed(project_deployed), format_uptime(project_uptime)
    service_times = {filename: (format_deployed(deployed[filename]), format_uptime(uptimes[filename]))
                     for filename in services}
    return state, project_times, service_times


def show_deployment_times(app, project_times, service_times, filenames=(), *, stream=None):
    deployed, uptime = project_times
    print(f'  Project {app}  |  Deployed (UTC) {deployed}  |  Uptime {uptime}', file=stream, flush=True)
    rows = [(Path(filename).stem, *service_times.get(filename, ('-', '-'))) for filename in filenames]
    if rows:
        OUT.table(('SERVICE', 'DEPLOYED (UTC)', 'UPTIME'), rows, stream=stream)


def systemd_state(info):
    load_state = info.get('LoadState', 'unknown')
    state = (info.get('ActiveState', 'unknown') + '/' + info.get('SubState', 'unknown')
             if load_state == 'loaded' else load_state)
    return state + ('*' if info.get('NeedDaemonReload') == 'yes' else '')


def list_service_name(app, service):
    return service if app in service else f'{app}/{service}'


def list_service_state(info, container_state):
    load = info['LoadState']
    if load != 'loaded':
        return {'not-found': 'missing', 'bad-setting': 'invalid'}.get(load, load)
    active = info.get('ActiveState', 'unknown')
    if active == 'failed':
        return 'failed'
    if info.get('SubState') in ('auto-restart', 'auto-restart-queued'):
        return 'restart'
    if active in ('activating', 'deactivating', 'reloading', 'refreshing'):
        return {'activating': 'start', 'deactivating': 'stop',
                'reloading': 'reload', 'refreshing': 'reload'}[active]
    if container_state == 'running':
        return 'up' if active == 'active' else 'run-only'
    if container_state == 'paused':
        return 'paused'
    if active == 'active':
        return 'no-ctr' if container_state == 'absent' else container_state
    return 'down' if active == 'inactive' else active


def normalize_ip(value):
    addr = ipaddress.ip_address(value.strip('[]').split('%', 1)[0])
    return getattr(addr, 'ipv4_mapped', None) or addr


def host_interfaces():
    if not shutil.which('ip'):
        return {}
    try:
        interfaces = json.loads(run('ip', '-j', 'address', 'show').stdout)
        return {normalize_ip(addr['local']): interface['ifname']
                for interface in interfaces for addr in interface.get('addr_info', ())
                if addr.get('family') in ('inet', 'inet6') and addr.get('local')}
    except (DeployError, OSError, ValueError):
        return {}


def published_ports(ports, interfaces):
    mappings = []
    for port in ports:
        host_port = port.get('host_port')
        if not host_port:
            continue
        host = port.get('host_ip') or '0.0.0.0'
        try:
            address = normalize_ip(host)
        except ValueError:
            binding = 'bound'
        else:
            if address.is_unspecified:
                binding = 'all'
            elif address.is_loopback:
                binding = 'local'
            else:
                binding = interfaces.get(address, 'bound')
        width = port.get('range') or 1
        target = port.get('container_port')
        mapping = f'{binding}:{host_port}'
        if width > 1:
            mapping += f'-{host_port + width - 1}'
        if target and target != host_port:
            mapping += f'->{target}'
            if width > 1:
                mapping += f'-{target + width - 1}'
        for protocol in (port.get('protocol') or 'tcp').split(','):
            mappings.append(mapping if protocol == 'tcp' else f'{mapping}/{protocol}')
    return ', '.join(dict.fromkeys(mappings)) or '-'


def show_service_list(args):
    if args.app:
        manifest = ROOT / args.app / 'manifest.json'
        if not manifest.is_file():
            fail(f'Project not managed: {args.app}; run appctl deploy first')
        projects = [(args.app, load_manifest(manifest))]
    else:
        projects = sorted(project_manifests())
    selected = []
    for app, manifest in projects:
        services = select_container_service(container_services(manifest), app, args.service)
        selected.extend((app, filename, services[filename])
                        for filename in sorted(services, key=lambda name: Path(name).stem))

    target = list_service_name(args.app, args.service) if args.service else args.app
    OUT.header('list', target)
    if not selected:
        OUT.line('INFO', 'No deployed services')
        return 0

    # Take one Podman snapshot and match names and ownership labels from the manifest.
    snapshot = json.loads(run('podman', 'ps', '--all', '--format', 'json').stdout)
    containers = {item['Names'][0]: item for item in snapshot if item.get('Names')}
    observed = int(time.time())
    interfaces = None
    rows = []
    for app, filename, meta in selected:
        unit = meta['unit']
        info = unit_info(unit)
        path = QUAD / filename
        unit_conflict = path.is_symlink() or (
            info['LoadState'] == 'loaded' and not same_path(info.get('SourcePath'), path))
        container = containers.get(meta['resource'])
        labels = (container or {}).get('Labels') or {}
        conflict = unit_conflict or (container is not None and labels.get('PODMAN_SYSTEMD_UNIT') != unit)
        state = (container.get('State') or 'unknown') if container is not None else 'absent'
        status = 'conflict' if conflict else list_service_state(info, state)
        uptime, ports = '-', '-'
        if not conflict and state in ('running', 'paused'):
            started = container.get('StartedAt') or 0
            if started > 0:
                uptime = format_uptime(max(0, observed - started)).replace(' ', '')
            if container.get('Ports'):
                if interfaces is None:
                    interfaces = host_interfaces()
                ports = published_ports(container['Ports'], interfaces)
        if info.get('NeedDaemonReload') == 'yes':
            status += '*'
        rows.append((list_service_name(app, Path(filename).stem), status, uptime, ports))
    OUT.table(('SERVICE', 'STATUS', 'UPTIME', 'PORTS'), rows,
              {'up': '32', 'down': '2', 'failed': '31', 'conflict': '31',
               'missing': '33', 'no-ctr': '33', 'restart': '33', 'paused': '33'})
    if any(status.endswith('*') for _, status, _, _ in rows):
        OUT.line('WARN', '* systemd reports daemon-reload needed')
    return 0


def show_status(args, project):
    runtime = {}
    selected = selected_units(args, project, runtime)
    deployed, project_times, service_times = deployment_times(
        project, containers={filename: obj for filename, (_, obj) in runtime.items()})
    OUT.header('status',args.app,args.service)
    show_deployment_times(args.app, project_times, service_times)
    rows = []
    for f in selected:
        info, obj = runtime[f]
        sd = systemd_state(info)
        state = (obj or {}).get('State',{})
        cs = ('running' if state.get('Running') else state.get('Status','stopped')) if obj else 'absent'
        iid = (obj or {}).get('Image','').removeprefix('sha256:')[:12] or '-'
        image=(obj or {}).get('ImageName') or deployed.get(f,{}).get('image','-')
        rows.append((Path(f).stem, sd, cs, short_image(image), iid, *service_times.get(f, ('-', '-'))))
    OUT.table(('SERVICE', 'SYSTEMD', 'CONTAINER', 'IMAGE', 'IMAGE ID', 'DEPLOYED (UTC)', 'UPTIME'), rows,
              {'active/running':'32','running':'32','inactive/dead':'2','absent':'2','failed/failed':'31','not-found':'33'})
    if any('*' in r[1] for r in rows): OUT.line('WARN','* systemd reports daemon-reload needed')
    running=sum(r[1].rstrip('*')=='active/running' and r[2]=='running' for r in rows)
    OUT.line('INFO',f'{running}/{len(rows)} services running; image tag for absent containers is last deployed')
    return 0


def require_container_id(container, service):
    container_id = container.get('Id')
    if not container_id:
        fail(f'Podman did not report a container ID for {service}')
    return container_id


def show_stats(args, project):
    _, selected = managed_services(args, project)
    targets = {}
    for filename, meta in selected.items():
        service = Path(filename).stem
        container = assert_runtime(meta, True)
        if container is None:
            OUT.diagnostic(f'Skipping {service}: no container named {meta["resource"]}')
            continue
        state = container.get('State') or {}
        if not state.get('Running'):
            OUT.diagnostic(f'Skipping {service}: container is {state.get("Status") or "not running"}')
            continue
        targets[service] = require_container_id(container, service)
    if not targets:
        fail(f'No running containers in {args.app}')

    if args.processes:
        result = 0
        for service, container_id in targets.items():
            OUT.phase(service)
            status = stream_command(['podman', 'top', '--', container_id,
                                     'user', 'pid', 'ppid', 'state', 'pcpu', 'etime', 'args'])
            if status == 130:
                return status
            result = max(result, status)
        return result

    command = ['podman', 'stats']
    if args.no_stream:
        command.append('--no-stream')
    if args.no_stream or not OUT.color:
        command.append('--no-reset')
    if args.interval is not None:
        command.append(f'--interval={args.interval}')
    command.extend(('--', *targets.values()))
    return stream_command(command)


def journal_log_matches(selected):
    # Match each unit and container name as a pair, scoped to the deployment UID.
    matches = []
    uid = os.getuid()
    for meta in selected.values():
        if matches:
            matches.append('+')
        matches.extend((f'_UID={uid}', f'_SYSTEMD_USER_UNIT={meta["unit"]}',
                        f'CONTAINER_NAME={meta["resource"]}'))
    return matches


def podman_log_ids(selected, containers):
    container_ids = []
    skipped = []
    for filename, meta in selected.items():
        service = Path(filename).stem
        container = assert_runtime(meta, True)
        containers[filename] = container
        if container is None:
            skipped.append(f'{service}: container {meta["resource"]} no longer exists')
            continue
        log_config = (container.get('HostConfig') or {}).get('LogConfig') or {}
        if log_config.get('Type') == 'none':
            skipped.append(f'{service}: the none log driver stores no output')
            continue
        container_ids.append(require_container_id(container, service))
    if not container_ids:
        fail('No Podman container logs available. ' + '. '.join(skipped))
    for reason in skipped:
        OUT.diagnostic(f'Skipping {reason}')
    return container_ids


def show_logs(args, project):
    manifest, selected = managed_services(args, project)
    containers = {}
    if args.source == 'podman':
        command = ['podman', 'logs', '--names', '--timestamps', f'--tail={args.lines}']
        targets = podman_log_ids(selected, containers)
    else:
        command = ['journalctl', '--no-pager', '--output=with-unit', f'--lines={args.lines}']
        targets = journal_log_matches(selected)
    if args.follow:
        command.append('--follow')
    if args.since is not None:
        command.append(f'--since={args.since}')
    command.extend(('--', *targets))
    _, project_times, service_times = deployment_times(project, manifest, containers)
    show_deployment_times(args.app, project_times, service_times, selected, stream=sys.stderr)
    env = dict(os.environ, SYSTEMD_COLORS='0') if args.source == 'journald' and not OUT.color else None
    return stream_command(command, env)


def display_port_mapping(value):
    parts = value.rsplit(':', 2)
    if len(parts) == 3:
        host, published, target = parts
        # Podlet can print an IPv6 address without the Compose brackets.
        if ':' in host and not host.startswith('['):
            return f'[{host}]:{published}:{target}'
    return value


def format_mount_options(options):
    return ','.join(key if value is None else f'{key}={value}' for key, value in options.items())


def container_mounts(fields):
    for value in fields.get('Volume', []):
        source, target, options = split_volume(value)
        kind = 'bind' if source.startswith(('/', '.', '~', '%')) else 'volume'
        yield kind, source or '(anonymous)', target, options
    for value in fields.get('Mount', []):
        options = mount_options(value)
        kind = options.pop('type', None) or 'mount'
        source = options.get('source') or options.get('src')
        target = options.get('target') or options.get('destination') or options.get('dst')
        coordinates = {'source', 'src', 'target', 'destination', 'dst'}
        extra = format_mount_options({key: value for key, value in options.items() if key not in coordinates})
        yield kind, source or ('(anonymous)' if kind == 'volume' else '-'), target, extra
    for value in fields.get('Tmpfs', []):
        target, _, options = value.partition(':')
        yield 'tmpfs', '-', target, options
    for value in fields.get('Secret', []):
        name, _, spec = value.partition(',')
        options = mount_options(spec)
        if options.pop('type', 'mount') == 'mount':
            target = options.pop('target', None) or name
            target = target if target.startswith('/') else f'/run/secrets/{target}'
            yield 'secret', name, target, format_mount_options(options)


def show_compose_resources(args):
    compose = compose_path(args.app)
    with tempfile.TemporaryDirectory(prefix='appctl-inspect-') as tmp:
        generated = generate_quadlets(compose, tmp, args.env_file)
        services = {path.stem: path for path in generated.values()
                    if path.suffix == '.container' and path.is_file()}
        if not services:
            fail(f'No container services generated from {compose}')
        if args.service:
            if args.service not in services:
                fail(f'Unknown service {args.service} in {args.app}. Available: ' + ', '.join(sorted(services)))
            services = {args.service: services[args.service]}
        volumes = {
            path.name: one(parse(path).get('Volume', {}), 'VolumeName', 'systemd-' + path.stem)
            for path in generated.values() if path.suffix == '.volume' and path.is_file()
        }
        ports = []
        mounts = []
        for service, path in sorted(services.items()):
            fields = parse(path).get('Container')
            if fields is None:
                fail(f'Missing [Container] in {path.name}')
            service_ports = [(service, 'published', display_port_mapping(value))
                             for value in fields.get('PublishPort', [])]
            service_ports.extend((service, 'exposed', value) for value in fields.get('ExposeHostPort', []))
            if 'host' in fields.get('Network', []):
                service_ports.append((service, 'host network', 'Uses host network'))
            ports.extend(service_ports or [(service, 'none', '-')])
            service_mounts = []
            for kind, source, target, options in container_mounts(fields):
                if kind == 'volume' and source in volumes:
                    source = f'{source} ({volumes[source]})'
                service_mounts.append((service, kind, source, target or '-', options or '-'))
            mounts.extend(service_mounts or [(service, 'none', '-', '-', '-')])

    _, project_times, service_times = deployment_times(ROOT / args.app)
    OUT.header('inspect', args.app, args.service)
    show_deployment_times(args.app, project_times, service_times, sorted(path.name for path in services.values()))
    OUT.phase('Ports')
    OUT.table(('SERVICE', 'KIND', 'PORT / MAPPING'), ports)
    OUT.phase('Mounts')
    OUT.table(('SERVICE', 'KIND', 'SOURCE', 'CONTAINER PATH', 'OPTIONS'), mounts)
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


def removal_retry(app, plan):
    request = plan['request']
    command = ['appctl', 'remove', app]
    if request['service']:
        command.append(request['service'])
    command.extend(f'--{flag}' for flag in ('volumes', 'networks') if request[flag])
    return 'Previous removal incomplete. Retry with ' + shlex.join(command)


def removal_unit(filename, meta):
    path = QUAD / filename
    if path.name != filename or path.suffix != f'.{meta["kind"]}':
        fail(f'Invalid managed filename: {filename}')
    literal(path.stem, 'Quadlet name')
    literal(meta['unit'], 'unit name')
    if path.is_symlink() or (path.exists() and (not path.is_file() or metadata(path) != meta)):
        fail(f'Managed Quadlet identity differs: {path}')
    info = unit_info(meta['unit'])
    if info['LoadState'] != 'not-found' and not same_path(info.get('SourcePath'), path):
        fail(f'Systemd unit conflict: {meta["unit"]}')
    return info


def service_resource_refs(filename, files):
    fields = parse(QUAD / filename)['Container']
    mounts = {('volume', name) for name in volume_mounts(fields)}
    networks = list(fields.get('Network', []))
    # Network can also be set in a local Quadlet drop-in.
    for directory in dropin_dirs(QUAD, filename):
        for dropin in directory.glob('*.conf'):
            networks.extend(parse(dropin).get('Container', {}).get('Network', []))
    mounts.update(('network', spec.strip('"').split(':', 1)[0]) for spec in networks)
    refs = set()
    for kind, name in mounts:
        meta = files.get(name)
        refs.add((kind, meta['resource'] if meta and meta['kind'] == kind else name))
    return refs


def removal_resources(args, manifest, selected, kept, resource_files, event):
    kinds = {kind for kind, enabled in (('volume', args.volumes), ('network', args.networks)) if enabled}
    candidates = set(resource_files) | {('volume', name) for name in manifest['named_volumes']}
    candidates = {key for key in candidates if key[0] in kinds}
    if not args.service or not candidates:
        return candidates

    filename = next(iter(selected))
    if not (QUAD / filename).is_file():
        fail(f'Cannot determine resources for {filename}. Its installed Quadlet is missing.')
    candidates &= service_resource_refs(filename, manifest['files'])
    for name, info in kept.items():
        if not candidates:
            break
        if not (QUAD / name).is_file():
            fail(f'Cannot verify shared resources. Installed Quadlet {name} is missing.')
        refs = service_resource_refs(name, manifest['files'])
        dependencies = {unit for key in ('Requires', 'Requisite', 'BindsTo', 'PartOf')
                        for unit in info.get(key, '').split()}
        refs.update(key for key, resource_file in resource_files.items()
                    if manifest['files'][resource_file]['unit'] in dependencies)
        for kind, resource in sorted(candidates & refs):
            event('resource_kept', kind=kind, resource=resource, reason=f'used by {Path(name).stem}')
        candidates -= refs
    return candidates


def check_removal_ownership(app, targets, resources):
    names = {('file', name) for name in targets}
    names.update(('unit', meta['unit']) for meta in targets.values())
    names.update((meta['kind'], meta['resource']) for meta in targets.values())
    names.update(resources)
    for other, manifest in project_manifests(app):
        for name, meta in manifest['files'].items():
            if names.intersection((('file', name), ('unit', meta['unit']), (meta['kind'], meta['resource']))):
                fail(f'Removal conflicts with resources owned by project {other}')
        if any(('volume', name) in names for name in manifest['named_volumes']):
            fail(f'Removal conflicts with a volume owned by project {other}')


def remove_deployment(args, project, event):
    mp = project / 'manifest.json'
    if not mp.is_file():
        fail(f'Project not managed: {args.app}. Run appctl deploy first.')
    manifest = load_manifest(mp)
    files = manifest['files']
    services = container_services(manifest)
    resource_files = {(meta['kind'], meta['resource']): name for name, meta in files.items()
                      if meta['kind'] in ('network', 'volume')}
    request = {key: getattr(args, key) for key in ('service', 'volumes', 'networks')}
    pending = manifest.get('remove_pending')
    if pending:
        if pending['request'] != request:
            fail(removal_retry(args.app, pending))
        selected = {filename: services[filename] for filename in pending['services']}
    elif args.service:
        _, selected = managed_services(args, project)
    else:
        selected = services

    # Removing a unit can propagate a stop to dependent services.
    kept = {name: unit_info(meta['unit']) for name, meta in services.items() if name not in selected}
    removed_units = {meta['unit'] for meta in selected.values()}
    for filename, info in kept.items():
        dependencies = {unit for key in ('Requires', 'Requisite', 'BindsTo', 'PartOf')
                        for unit in info.get(key, '').split()}
        blocked = dependencies & removed_units
        if blocked:
            fail(f'{Path(filename).stem} depends on ' + ', '.join(sorted(blocked)))

    if pending:
        resources = {tuple(key) for key in pending['resources']}
    else:
        resources = removal_resources(args, manifest, selected, kept, resource_files, event)
    if not selected and not resources:
        return 'noop'
    targets = {**selected, **{resource_files[key]: files[resource_files[key]]
                              for key in resources if key in resource_files}}
    check_removal_ownership(args.app, targets, resources)
    owned_dropins = owned_project_dropins(manifest)
    removed_dropins = {path for path in owned_dropins if path.parent.stem in selected}
    for path in removed_dropins:
        installed_dropin(path)
    for filename, meta in targets.items():
        removal_unit(filename, meta)
    owners = {}
    for meta in selected.values():
        owners.setdefault(meta['resource'], set()).add(meta['unit'])
    for name, units in owners.items():
        assert_container_owner(name, units)
    for kind, resource in resources:
        literal(resource, f'{kind} name')

    manifest['remove_pending'] = {'request': request, 'services': sorted(selected), 'resources': sorted(resources)}
    save(mp, manifest)

    for filename, meta in selected.items():
        event('remove_begin', unit=meta['unit'])
        if removal_unit(filename, meta)['LoadState'] != 'not-found':
            system('stop', meta['unit'], capture=False)
    for name, units in owners.items():
        container = assert_container_owner(name, units)
        if container:
            run('podman', 'container', 'rm', '--', require_container_id(container, name), capture=False)
    for filename in selected:
        (QUAD / filename).unlink(missing_ok=True)
    for path in sorted(removed_dropins):
        path.unlink(missing_ok=True)
        event('dropin_removed', file=str(path.relative_to(QUAD)))
    if selected:
        reload_units()
        for unit in removed_units:
            if unit_info(unit)['LoadState'] != 'not-found':
                fail(f'Removed unit still loaded: {unit}')

    # Podman rm accepts partial names for some resource types. Only pass names
    # that exist exactly, and let Podman protect resources attached to containers.
    present = {kind: set(run('podman', kind, 'ls', '--format', '{{.Name}}').stdout.splitlines())
               for kind in sorted({kind for kind, _ in resources})}
    removed_resources = set()
    removed_files = dict(selected)
    for kind, resource in sorted(resources):
        if resource in present[kind]:
            result = run('podman', kind, 'rm', '--', resource, ok=(0, 1, 2))
            if result.returncode == 2:
                event('resource_kept', kind=kind, resource=resource, reason='in use by Podman')
                continue
        filename = resource_files.get((kind, resource))
        if filename:
            meta = files[filename]
            if removal_unit(filename, meta)['LoadState'] != 'not-found':
                system('stop', meta['unit'], capture=False)
            (QUAD / filename).unlink(missing_ok=True)
            removed_files[filename] = meta
        removed_resources.add((kind, resource))
    if removed_files.keys() != selected.keys():
        reload_units()
        for filename in removed_files.keys() - selected.keys():
            unit = removed_files[filename]['unit']
            if unit_info(unit)['LoadState'] != 'not-found':
                fail(f'Removed unit still loaded: {unit}')

    state_path = project / 'state.json'
    state = load(state_path, {})
    if selected:
        save(state_path, {filename: data for filename, data in state.items() if filename not in selected})
    for filename in removed_files:
        files.pop(filename)
    manifest['dropins'] = sorted(str(path.relative_to(QUAD)) for path in owned_dropins - removed_dropins)
    manifest['named_volumes'] = [name for name in manifest['named_volumes']
                                 if ('volume', name) not in removed_resources]
    manifest.pop('remove_pending')
    if not kept:
        manifest['reload_pending'] = False
    save(mp, manifest)
    for meta in selected.values():
        event('removed', unit=meta['unit'])
    for kind, resource in sorted(removed_resources):
        event('resource_removed', kind=kind, resource=resource)
    return 'success' if selected or removed_resources else 'noop'


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
    env_help = 'Compose substitution file (default: compose.env next to compose.yml)'
    parser.add_argument('--env-file', type=Path, metavar='FILE', help=env_help)
    subs=parser.add_subparsers(dest='command',required=True)
    p=subs.add_parser('deploy',help='Incrementally deploy a project from compose.yml')
    p.add_argument('app',help=f'Project directory under APPCTL_BASE_DIR (default: {DEFAULT_BASE_DIR})')
    p.add_argument('--pull',action='store_true',help='Check registry even for cached images')
    p.add_argument('--force',action='store_true',help='Restart all desired services')
    p.add_argument('--env-file', type=Path, metavar='FILE', default=argparse.SUPPRESS, help=env_help)
    p = subs.add_parser('list', aliases=['ps'], help='List deployed services and their runtime states')
    p.set_defaults(command='list')
    p.add_argument('app', nargs='?', help='Project name (default: all deployed projects)')
    p.add_argument('service', nargs='?', help='Compose service name (default: all deployed services in the project)')
    for action, helptext in [
        ('start', 'Start deployed services'),
        ('stop', 'Temporarily stop deployed services'),
        ('restart', 'Restart deployed services'),
        ('remove', 'Remove deployed services and optionally their resources'),
        ('status', 'Show systemd and Podman runtime state'),
        ('stats', 'Show resource usage or processes for deployed containers'),
        ('inspect', 'Show ports and mounts declared in compose.yml'),
        ('logs', 'Show logs from deployed containers'),
    ]:
        p=subs.add_parser(action,help=helptext)
        p.add_argument('app',help=f'Project directory under APPCTL_BASE_DIR (default: {DEFAULT_BASE_DIR})')
        scope = 'all services in compose.yml' if action == 'inspect' else 'all owned container services'
        p.add_argument('service', nargs='?', help=f'Compose service name (default: {scope})')
        if action == 'inspect':
            p.add_argument('--env-file', type=Path, metavar='FILE', default=argparse.SUPPRESS, help=env_help)
        if action == 'remove':
            p.add_argument('--volumes', action='store_true', help='Delete unused managed named volumes and their data')
            p.add_argument('--networks', action='store_true', help='Delete unused managed Podman networks')
        if action == 'stats':
            p.add_argument('--no-stream', action='store_true', help='Show one resource usage sample')
            mode = p.add_mutually_exclusive_group()
            mode.add_argument('-i', '--interval', type=int, metavar='SECONDS',
                              help='Seconds between resource usage samples (Podman defaults to 5)')
            mode.add_argument('--processes', action='store_true', help='List individual processes in a snapshot')
        if action == 'logs':
            p.add_argument('-n', '--lines', type=int, default=100, help='Number of recent entries (default: 100)')
            p.add_argument('-f', '--follow', action='store_true', help='Follow new entries')
            p.add_argument('--since', metavar='TIME', help='Show entries since this time in the selected source format')
            p.add_argument('--source', choices=('journald', 'podman'), default='journald',
                           help='Read the journal or use Podman to read the configured driver (default: journald)')
    p=subs.add_parser('history',help='List retained deployment and operation history')
    p.add_argument('app')
    p.add_argument('--limit',type=int,default=20,help='Maximum entries to list (default: 20)')
    subs.add_parser('gc',help='Prune expired/excess history across all projects')
    for sub in dict.fromkeys(subs.choices.values()): output_flags(sub,argparse.SUPPRESS)
    args=parser.parse_args(argv)
    if args.env_file is not None and args.command not in ('deploy', 'inspect'):
        parser.error('--env-file is supported by deploy and inspect')
    if getattr(args,'limit',1)<1: parser.error('--limit must be positive')
    if args.command == 'logs' and args.lines < 0:
        parser.error('--lines must be non-negative')
    if args.command == 'stats' and args.interval is not None and args.interval < 1:
        parser.error('--interval must be positive')
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
    required = ['podman', 'systemctl'] if args.command in ('deploy', 'start', 'stop', 'restart', 'remove', 'status', 'list') else []
    if args.command in ('deploy', 'inspect'):
        required.append('podlet')
    if args.command == 'logs':
        required.append('podman' if args.source == 'podman' else 'journalctl')
    if args.command == 'stats':
        required.append('podman')
    for cmd in required:
        if not shutil.which(cmd): fail(f'Required command not found: {cmd}')
    if args.command == 'inspect':
        return show_compose_resources(args)
    if args.command == 'logs':
        return show_logs(args, ROOT / args.app)
    if args.command == 'stats':
        return show_stats(args, ROOT / args.app)
    if args.command == 'list':
        return show_service_list(args)
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
            _, project_times, service_times = deployment_times(project)
            show_deployment_times(args.app, project_times, service_times, sorted(service_times))
            OUT.phase('Operation history')
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
        option_names = ('volumes', 'networks') if args.command == 'remove' else ('pull', 'force')
        record={'version':VERSION,'app':args.app,'command':args.command,'service':getattr(args,'service',None),
                'started_at':now(),'result':'running',
                'options':{k:getattr(args,k,False) for k in option_names},'events':[]}
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
            elif action=='dropin_removed': OUT.line('REMOVE',fields['file'])
            elif action.endswith('_begin'): OUT.line(action.split('_')[0].upper(),unit)
            elif action in ('resource_kept', 'resource_removed'):
                label = 'KEEP' if action == 'resource_kept' else 'OK'
                OUT.line(label, f'{fields["kind"]} {fields["resource"]} {action.split("_")[1]}', reason)
            elif action in ('restarted','started','stopped','removed'):
                detail=short_image(image)+' @ '+fields['image_id'].removeprefix('sha256:')[:12] if image and fields.get('image_id') else None
                OUT.line('OK',f'{unit} {action}',detail)
            else: OUT.line('INFO',action+' '+unit)
        rc=0
        try:
            operation = {'deploy': deploy, 'remove': remove_deployment}.get(args.command, control)
            record['result'] = operation(args, project, event)
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
