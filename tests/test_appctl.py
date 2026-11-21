import os
import signal
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import appctl


def quadlet(container='', unit=''):
    dependencies = '[Unit]\n' + unit if unit else ''
    return dependencies + '[Container]\nImage=docker.io/library/nginx:test\nPull=never\n' + container


class DeployTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.config = self.base / 'config'
        self.quad = self.config / 'containers/systemd'
        self.state = self.base / 'state'
        self.apps = self.base / 'apps'
        for directory in (self.quad, self.state, self.apps):
            directory.mkdir(parents=True)
        self.desired = {}
        self.loaded = {}
        self.running = {}
        self.invocations = {}
        self.jobs = set()
        self.volumes = set()
        self.calls = []
        self.preflight_sources = set()
        self.preflight_invalid = set()
        self.live_invalid = set()
        self.cannot_start = set()
        self.stops_on_start = {}
        stack = patch.multiple(
            appctl, BASE=self.apps, ROOT=self.state, CONFIG=self.config, QUAD=self.quad,
            ADMIN_QUAD=self.base / 'admin', run=self.run_command, system=self.system,
            inspect_container=self.inspect_container, exists=self.exists, unit_info=self.unit_info,
            generator_command=lambda: 'quadlet-generator', runtime_quadlet_dir=lambda: None,
        )
        stack.start()
        self.addCleanup(stack.stop)

    def run_command(self, *args, env=None, **kwargs):
        if args[0] == 'podlet':
            output = Path(args[args.index('--file') + 1])
            for name, content in self.desired.items():
                (output / name).write_text(content)
            return SimpleNamespace(stdout='', stderr='', returncode=0)
        if args == ('quadlet-generator', '--user', '--dryrun'):
            self.calls.append(('preflight',))
            staged = Path(env['QUADLET_UNIT_DIRS'].split(os.pathsep)[0])
            self.preflight_sources = {path.name for path in staged.iterdir()}
            generated = []
            for path in staged.iterdir():
                if path.name not in self.desired or path.name in self.preflight_invalid:
                    continue
                unit = appctl.metadata(path)['unit']
                generated.append(f'---{unit}---\n[Unit]\nSourcePath={path}\n')
            code = int(bool(self.preflight_invalid))
            return SimpleNamespace(stdout='\n'.join(generated), stderr='bad generator input', returncode=code)
        if args[:4] == ('podman', 'image', 'inspect', '--format'):
            return SimpleNamespace(stdout='sha256:test-image\n')
        self.fail(f'Unexpected command {args}')

    def exists(self, kind, name):
        if kind == 'volume':
            return name in self.volumes
        if kind == 'container':
            return name in self.running.values()
        return kind == 'image'

    def inspect_container(self, name):
        for unit, container in self.running.items():
            if container == name:
                return {'Image': 'sha256:test-image', 'State': {'Running': True},
                        'Config': {'Labels': {'PODMAN_SYSTEMD_UNIT': unit}}}
        return None

    def unit_info(self, unit):
        loaded = self.loaded.get(unit, {})
        return {**loaded, 'LoadState': 'loaded' if loaded else 'not-found',
                'ActiveState': 'active' if unit in self.running else 'inactive',
                'NeedDaemonReload': 'no', 'InvocationID': self.invocations.get(unit, ''),
                'Job': '1' if unit in self.jobs else ''}

    def start(self, unit):
        if unit in self.cannot_start:
            appctl.fail(f'Start failed: {unit}')
        self.jobs.discard(unit)
        definition = self.loaded[unit]
        self.running[unit] = definition['Resource']
        self.invocations[unit] = str(int(self.invocations.get(unit, '0')) + 1)
        self.volumes.update(definition['Mounts'])
        for stopped in self.stops_on_start.get(unit, ()):
            self.running.pop(stopped, None)

    def system(self, action, *units, **kwargs):
        self.calls.append((action, *units))
        if action == 'daemon-reload':
            self.loaded = {}
            managed = self.desired.keys() | self.old_files
            for path in self.quad.iterdir():
                if path.name in managed and path.name not in self.live_invalid:
                    meta = appctl.metadata(path)
                    sections = appctl.parse(path)
                    dependencies = {key: ' '.join(values) for key, values in sections.get('Unit', {}).items()}
                    self.loaded[meta['unit']] = {
                        **dependencies, 'SourcePath': str(path), 'Resource': meta['resource'],
                        'Mounts': appctl.named_volumes(sections.get('Container', {})),
                    }
            return
        for unit in units:
            if action == 'stop':
                self.running.pop(unit, None)
            elif action == 'restart':
                self.start(unit)
                for dependent in self.running.keys() - {unit}:
                    if unit in self.loaded[dependent].get('Requires', '').split():
                        self.jobs.add(dependent)
            elif action == 'start' and (unit not in self.running or unit in self.jobs):
                self.start(unit)

    def deploy(self, app='demo', force=False):
        appdir = self.apps / app
        appdir.mkdir(exist_ok=True)
        (appdir / 'compose.yml').touch()
        project = self.state / app
        project.mkdir(exist_ok=True)
        self.old_files = {name for previous in self.state.iterdir()
                          for name in self.manifest(previous.name).get('files', {})}
        with redirect_stdout(StringIO()):
            return appctl.deploy(SimpleNamespace(app=app, force=force, pull=False), project,
                                 lambda action, **fields: None)

    def manifest(self, app='demo'):
        return appctl.load(self.state / app / 'manifest.json', {})

    def inspect(self, *selectors):
        compose = self.apps / 'demo/compose.yml'
        compose.parent.mkdir(exist_ok=True)
        compose.touch()
        unused_state = self.base / 'unused-state'
        report = StringIO()
        with patch.object(appctl, 'ROOT', unused_state), patch.object(appctl, 'OUT', appctl.Output()), \
                patch.object(appctl, 'configure'), patch.object(appctl.shutil, 'which', return_value='/test/podlet') as which, \
                redirect_stdout(report):
            self.assertEqual(appctl.main(['inspect', 'demo', *selectors, '--no-color']), 0)
        which.assert_called_once_with('podlet')
        self.assertFalse(unused_state.exists())
        self.assertEqual(self.calls, [])
        return report.getvalue()

    def test_inspect_reports_declared_ports_and_mounts_for_all_services(self):
        self.desired = {
            'api.container': (
                '[Container]\nImage=docker.io/library/nginx:test\n'
                'PublishPort=::1:8053:53/udp\n'
                'PublishPort=[::]:8443:443\n'
                'PublishPort=127.0.0.1:8000-8002:80-82\n'
                'PublishPort=443\nExposeHostPort=8080/tcp\n'
                'Volume=db.volume:/data:rw\nVolume=/scratch\n'
                'Mount=type=bind,"source=/srv/config,prod.yaml",dst=/etc/api.yaml,ro\n'
                'Tmpfs=/tmp:rw,size=64m\n'
                'Secret=cert,target=tls.pem,mode=0440\nSecret=env-only,type=env\n'
            ),
            'db.volume': '[Volume]\nVolumeName=app-db\n',
            'idle.container': quadlet(),
            'worker.container': quadlet('Network=host\nVolume=/srv/worker:/work:ro,Z\n'),
        }
        report = self.inspect()
        self.assertRegex(report, r'api\s+published\s+\[::1\]:8053:53/udp')
        self.assertRegex(report, r'api\s+published\s+\[::\]:8443:443')
        self.assertRegex(report, r'api\s+published\s+127\.0\.0\.1:8000-8002:80-82')
        self.assertRegex(report, r'api\s+published\s+443')
        self.assertRegex(report, r'api\s+exposed\s+8080/tcp')
        self.assertRegex(report, r'api\s+volume\s+db\.volume \(app-db\)\s+/data\s+rw')
        self.assertRegex(report, r'api\s+volume\s+\(anonymous\)\s+/scratch')
        self.assertRegex(report, r'api\s+bind\s+/srv/config,prod.yaml\s+/etc/api.yaml\s+ro')
        self.assertRegex(report, r'api\s+tmpfs\s+-\s+/tmp\s+rw,size=64m')
        self.assertRegex(report, r'api\s+secret\s+cert\s+/run/secrets/tls.pem\s+mode=0440')
        self.assertNotIn('env-only', report)
        self.assertRegex(report, r'worker\s+host network\s+Uses host network')
        self.assertRegex(report, r'worker\s+bind\s+/srv/worker\s+/work\s+ro,Z')
        self.assertRegex(report, r'idle\s+none')

    def test_inspect_selects_one_service_and_lists_available_names_on_error(self):
        self.desired = {
            'api.container': quadlet('PublishPort=8080:80\n'),
            'worker.container': quadlet('Volume=jobs:/jobs\n'),
        }
        report = self.inspect('worker')
        self.assertRegex(report, r'worker\s+volume\s+jobs\s+/jobs')
        self.assertNotIn('8080:80', report)
        with self.assertRaisesRegex(appctl.DeployError, 'Unknown service missing in demo. Available: api, worker'):
            self.inspect('missing')

    def test_direct_named_volume_blocks_other_projects_and_survives_removal(self):
        self.desired = {'web.container': quadlet('Volume=app_data:/data\n')}
        self.assertEqual(self.deploy(), 'success')
        self.desired = {'web.container': quadlet()}
        self.assertEqual(self.deploy(), 'success')
        self.assertEqual(self.manifest()['named_volumes'], ['app_data'])
        self.desired = {'other.container': quadlet('Mount=type=volume,src=app_data,dst=/data\n')}
        with self.assertRaisesRegex(appctl.DeployError, 'owned by project demo'):
            self.deploy('other')

    def test_existing_unmanaged_volume_is_rejected(self):
        self.volumes.add('app_data')
        self.desired = {'web.container': quadlet('Volume=app_data:/data\n')}
        with self.assertRaisesRegex(appctl.DeployError, 'Existing unmanaged volume: app_data'):
            self.deploy()
        self.assertEqual(self.calls, [])

    def test_services_can_share_a_registered_volume_and_redeploy_without_changes(self):
        self.desired = {
            'web.container': quadlet('Volume=app_data:/data\n'),
            'worker.container': quadlet('Mount=type=volume,src=app_data,dst=/data\n'),
        }
        (self.state / 'failed-project/history').mkdir(parents=True)
        self.assertEqual(self.deploy(), 'success')
        self.assertEqual(self.manifest()['named_volumes'], ['app_data'])
        args = SimpleNamespace(app='demo', command='restart', service=None)
        self.assertEqual(appctl.selected_units(args, self.state / 'demo').keys(), self.desired.keys())
        self.calls.clear()
        with patch.object(appctl, 'save', wraps=appctl.save) as save:
            self.assertEqual(self.deploy(), 'noop')
        save.assert_not_called()
        self.assertEqual(self.calls, [])

    def test_invalid_candidate_does_not_stop_or_replace_old_unit(self):
        self.desired = {'old.container': quadlet()}
        self.deploy()
        self.calls.clear()
        self.desired = {'new.container': quadlet()}
        self.preflight_invalid.add('new.container')
        with self.assertRaises(appctl.CommandError):
            self.deploy()
        self.assertEqual(self.calls, [('preflight',)])
        self.assertNotIn('old.container', self.preflight_sources)
        self.assertTrue((self.quad / 'old.container').is_file())
        self.assertIn('old.service', self.running)

    def test_live_load_failure_restores_definitions_without_stopping_old_unit(self):
        self.desired = {'old.container': quadlet(), 'keep.container': quadlet()}
        self.deploy()
        self.calls.clear()
        self.desired = {'new.container': quadlet(), 'keep.container': quadlet('Environment=MODE=two\n')}
        self.live_invalid.add('new.container')
        with self.assertRaisesRegex(appctl.DeployError, 'generation failed'):
            self.deploy()
        self.assertNotIn(('stop', 'old.service'), self.calls)
        self.assertIn('old.service', self.running)
        self.assertFalse((self.quad / 'new.container').exists())
        self.assertEqual((self.quad / 'keep.container').read_text(), quadlet())
        self.live_invalid.clear()
        self.assertEqual(self.deploy(), 'success')
        self.assertFalse((self.quad / 'old.container').exists())

    def test_startup_failure_keeps_old_definitions_for_retry(self):
        self.desired = {'old.container': quadlet()}
        self.deploy()
        self.desired = {'new.container': quadlet()}
        self.cannot_start.add('new.service')
        with self.assertRaisesRegex(appctl.DeployError, 'Start failed'):
            self.deploy()
        self.assertTrue((self.quad / 'old.container').is_file())
        self.assertIn('old.container', self.manifest()['files'])
        self.cannot_start.clear()
        self.assertEqual(self.deploy(), 'success')
        self.assertFalse((self.quad / 'old.container').exists())

    def test_required_removed_service_is_rejected_before_stop(self):
        self.desired = {'old.container': quadlet(), 'web.container': quadlet()}
        self.deploy()
        self.calls.clear()
        self.desired = {'web.container': quadlet(unit='Requires=old.service\nAfter=old.service\n')}
        with self.assertRaisesRegex(appctl.DeployError, 'requires removed unit'):
            self.deploy()
        self.assertNotIn(('stop', 'old.service'), self.calls)
        self.assertIn('old.service', self.running)
        self.assertEqual((self.quad / 'web.container').read_text(), quadlet())

    def test_dependency_restarts_before_consumer_and_queued_restart_is_joined(self):
        self.desired = {
            'a-web.container': quadlet(unit='Requires=z-db.service\nAfter=z-db.service\n'),
            'z-db.container': quadlet(),
        }
        self.deploy()
        self.calls.clear()
        self.assertEqual(self.deploy(force=True), 'success')
        self.assertEqual(self.calls, [('restart', 'z-db.service'), ('start', 'a-web.service')])
        self.assertEqual(self.invocations, {'z-db.service': '2', 'a-web.service': '2'})

    def test_end_verification_checks_services_previously_skipped(self):
        self.desired = {'a-web.container': quadlet(), 'z-worker.container': quadlet()}
        self.deploy()
        self.desired['z-worker.container'] = quadlet('Environment=MODE=two\n')
        self.stops_on_start['z-worker.service'] = ('a-web.service',)
        with self.assertRaisesRegex(appctl.DeployError, 'Service not active: a-web.service'):
            self.deploy()
        deployed = appctl.load(self.state / 'demo/state.json', {})
        self.assertNotIn('z-worker.container', deployed)

    def test_anonymous_volume_and_unrelated_multiline_quadlet_are_accepted(self):
        continuation = '\\' + '\n'
        (self.quad / 'unrelated.container').write_text(
            '[Container]\nImage=docker.io/library/nginx:test\n'
            f'Environment=ONE=1 {continuation}# comment\n TWO=2\n'
        )
        self.desired = {'web.container': quadlet('Volume=/appctl-container-data\n')}
        self.assertEqual(self.deploy(), 'success')
        self.assertEqual(self.manifest()['named_volumes'], [])
        fields = appctl.parse(self.quad / 'unrelated.container')['Container']
        self.assertEqual(fields['Environment'][0].split(), ['ONE=1', 'TWO=2'])

    def test_xdg_and_dash_prefix_dropins_are_fingerprinted(self):
        self.desired = {'demo-web.container': quadlet('ServiceName=service-web\n')}
        self.deploy()
        root = self.config / 'systemd/user'
        prefix = root / 'service-.service.d/10-prefix.conf'
        specific = root / 'service-web.service.d/20-specific.conf'
        for path in (prefix, specific):
            path.parent.mkdir(parents=True)
            path.write_text('[Service]\nRestartSec=10s\n')
        self.calls.clear()
        self.assertEqual(self.deploy(), 'success')
        self.assertEqual(self.calls, [('preflight',), ('daemon-reload',), ('restart', 'service-web.service')])
        prefix.unlink()
        self.assertEqual(self.deploy(), 'success')
        self.assertEqual(self.deploy(), 'noop')


class LogsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        appctl.save(self.root / 'demo/manifest.json', {
            'files': {
                'worker.container': {'kind': 'container', 'unit': 'worker.service', 'resource': 'systemd-worker'},
                'api.container': {'kind': 'container', 'unit': 'api-custom.service', 'resource': 'api-container'},
                'cache.volume': {'kind': 'volume', 'unit': 'cache-volume.service', 'resource': 'systemd-cache'},
            },
            'named_volumes': [],
            'reload_pending': False,
        })

    def logs(self, *args, source=None, child_status=0):
        source_args = ['--source', source] if source else []
        executable = 'podman' if source == 'podman' else 'journalctl'
        with patch.object(appctl, 'ROOT', self.root), patch.object(appctl, 'OUT', appctl.Output()), \
                patch.object(appctl, 'configure'), patch.object(appctl.shutil, 'which', return_value=f'/test/{executable}') as which, \
                patch.object(appctl.subprocess, 'run', return_value=SimpleNamespace(returncode=child_status)) as child:
            status = appctl.main(['logs', 'demo', *args, *source_args, '--no-color'])
        which.assert_called_once_with(executable)
        child.assert_called_once()
        self.assertFalse((self.root / 'deploy.lock').exists())
        self.assertFalse((self.root / 'demo/history').exists())
        return status, child.call_args

    @staticmethod
    def container(container_id, unit, driver):
        return {'Id': container_id, 'Config': {'Labels': {'PODMAN_SYSTEMD_UNIT': unit}},
                'HostConfig': {'LogConfig': {'Type': driver}}}

    def test_project_logs_match_each_unit_and_container_as_a_pair(self):
        status, journal = self.logs()
        uid = f'_UID={os.getuid()}'
        self.assertEqual(status, 0)
        self.assertEqual(journal.args, ([
            'journalctl', '--no-pager', '--output=with-unit', '--lines=100', '--',
            uid, '_SYSTEMD_USER_UNIT=api-custom.service', 'CONTAINER_NAME=api-container',
            '+', uid, '_SYSTEMD_USER_UNIT=worker.service', 'CONTAINER_NAME=systemd-worker',
        ],))
        self.assertEqual(set(journal.kwargs), {'env'})
        self.assertEqual(journal.kwargs['env']['SYSTEMD_COLORS'], '0')

    def test_single_service_follow_uses_line_count_and_time_range(self):
        status, journal = self.logs('worker', '-f', '-n', '0', '--since', '1 hour ago')
        self.assertEqual(status, 0)
        self.assertEqual(journal.args, ([
            'journalctl', '--no-pager', '--output=with-unit', '--lines=0', '--follow',
            '--since=1 hour ago', '--', f'_UID={os.getuid()}',
            '_SYSTEMD_USER_UNIT=worker.service', 'CONTAINER_NAME=systemd-worker',
        ],))

    def test_journal_errors_and_interrupt_have_appctl_exit_codes(self):
        self.assertEqual(self.logs(child_status=2)[0], 1)
        self.assertEqual(self.logs('-f', child_status=-signal.SIGINT)[0], 130)

    def test_podman_logs_use_verified_ids_and_native_options_for_mixed_drivers(self):
        ids = ['a' * 64, 'b' * 64]
        containers = {
            'api-container': self.container(ids[0], 'api-custom.service', 'k8s-file'),
            'systemd-worker': self.container(ids[1], 'worker.service', 'journald'),
        }
        with patch.object(appctl, 'inspect_container', side_effect=containers.get):
            status, child = self.logs('-f', '-n', '0', '--since', '30m', source='podman')
        self.assertEqual(status, 0)
        self.assertEqual(child.args, ([
            'podman', 'logs', '--names', '--timestamps', '--tail=0', '--follow', '--since=30m', '--', *ids,
        ],))

    def test_podman_logs_skip_services_with_no_stored_output(self):
        containers = {
            'api-container': self.container('a' * 64, 'api-custom.service', 'k8s-file'),
            'systemd-worker': self.container('b' * 64, 'worker.service', 'none'),
        }
        with patch.object(appctl, 'inspect_container', side_effect=containers.get), redirect_stderr(StringIO()) as stderr:
            status, child = self.logs(source='podman')
        self.assertEqual(status, 0)
        self.assertEqual(child.args[0][-2:], ['--', 'a' * 64])
        self.assertIn('Skipping worker: the none log driver stores no output', stderr.getvalue())

    def test_podman_logs_require_a_container_owned_by_the_selected_service(self):
        with patch.object(appctl, 'inspect_container', return_value=None):
            with self.assertRaisesRegex(appctl.DeployError, 'container api-container no longer exists'):
                self.logs('api', source='podman')
        unrelated = self.container('a' * 64, 'other.service', 'k8s-file')
        with patch.object(appctl, 'inspect_container', return_value=unrelated):
            with self.assertRaisesRegex(appctl.DeployError, 'unverified owner: api-container'):
                self.logs('api', source='podman')

    def test_logs_reject_negative_line_counts(self):
        with redirect_stderr(StringIO()) as stderr, self.assertRaises(SystemExit) as error:
            appctl.parse_args(['logs', 'demo', '--lines', '-1'])
        self.assertEqual(error.exception.code, 2)
        self.assertIn('--lines must be non-negative', stderr.getvalue())


if __name__ == '__main__':
    unittest.main()
