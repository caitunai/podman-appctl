import json
import os
import signal
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
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
        self.started_at = {}
        self.invocations = {}
        self.jobs = set()
        self.volumes = set()
        self.networks = set()
        self.resources_in_use = set()
        self.fail_resource_removal = set()
        self.image_cached = True
        self.pulls = []
        self.calls = []
        self.preflight_sources = set()
        self.preflight_dropins = {}
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
            self.preflight_dropins = {str(path.relative_to(staged)): path.read_text()
                                       for path in staged.glob('*.container.d/*.conf')}
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
        if args[:2] == ('podman', 'pull'):
            if '--policy' in args:
                raise appctl.CommandError(args, 125, '', 'Error: unknown flag: --policy')
            self.pulls.append(args)
            self.image_cached = True
            return SimpleNamespace(stdout='sha256:test-image\n')
        if args[:2] in (('podman', 'volume'), ('podman', 'network')):
            self.calls.append(args)
            kind = args[1]
            resources = self.volumes if kind == 'volume' else self.networks
            if args[2:] == ('ls', '--format', '{{.Name}}'):
                return SimpleNamespace(stdout='\n'.join(sorted(resources)))
            if len(args) == 5 and args[2:4] == ('rm', '--'):
                name = args[4]
                self.assertIn(name, resources)
                if (kind, name) in self.fail_resource_removal:
                    raise appctl.CommandError(args, 125, '', 'resource removal failed')
                if (kind, name) in self.resources_in_use:
                    return SimpleNamespace(returncode=2)
                resources.remove(name)
                return SimpleNamespace(returncode=0)
        self.fail(f'Unexpected command {args}')

    def exists(self, kind, name):
        if kind == 'volume':
            return name in self.volumes
        if kind == 'container':
            return name in self.running.values()
        return kind == 'image' and self.image_cached

    def inspect_container(self, name):
        for unit, container in self.running.items():
            if container == name:
                return {'Image': 'sha256:test-image',
                        'State': {'Running': True, 'StartedAt': self.started_at.get(unit)},
                        'Config': {'Labels': {'PODMAN_SYSTEMD_UNIT': unit}}}
        return None

    def unit_info(self, unit):
        loaded = self.loaded.get(unit, {})
        return {**loaded, 'LoadState': 'loaded' if loaded else 'not-found',
                'ActiveState': 'active' if unit in self.running else 'inactive',
                'SubState': 'running' if unit in self.running else 'dead',
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

    def deploy(self, app='demo', force=False, pull=False):
        appdir = self.apps / app
        appdir.mkdir(exist_ok=True)
        (appdir / 'compose.yml').touch()
        project = self.state / app
        project.mkdir(exist_ok=True)
        self.old_files = {name for previous in self.state.iterdir()
                          for name in self.manifest(previous.name).get('files', {})}
        with redirect_stdout(StringIO()):
            return appctl.deploy(SimpleNamespace(app=app, force=force, pull=pull), project,
                                 lambda action, **fields: None)

    def manifest(self, app='demo'):
        return appctl.load(self.state / app / 'manifest.json', {})

    def remove(self, *args, expected=0):
        output = StringIO()
        with patch.object(appctl, 'OUT', appctl.Output()), patch.object(appctl, 'configure'), \
                patch.object(appctl.shutil, 'which', side_effect=lambda cmd: cmd if cmd != 'podlet' else None), \
                patch.object(appctl.os, 'umask'), patch.dict(os.environ), \
                redirect_stdout(output), redirect_stderr(output):
            result = appctl.main(['remove', 'demo', *args, '--no-color'])
        self.assertEqual(result, expected, output.getvalue())
        history = self.state / 'demo' / 'history'
        return output.getvalue(), appctl.load(max(history.glob('*.json')), {})

    def test_remove_one_service_preserves_resources_and_compose_by_default(self):
        self.desired = {
            'api.container': quadlet('Volume=api-data:/data\nNetwork=app.network\n'),
            'web.container': quadlet(),
            'app.network': '[Network]\nNetworkName=app-net\n',
        }
        self.deploy()
        self.networks.add('app-net')
        self.calls.clear()

        report, record = self.remove('api')

        self.assertIn('api.service removed', report)
        self.assertEqual(record['result'], 'success')
        self.assertEqual(record['options'], {'volumes': False, 'networks': False})
        self.assertEqual(self.calls, [('stop', 'api.service'), ('daemon-reload',)])
        self.assertEqual(set(self.running), {'web.service'})
        self.assertFalse((self.quad / 'api.container').exists())
        self.assertTrue((self.quad / 'app.network').is_file())
        self.assertEqual(self.volumes, {'api-data'})
        self.assertEqual(self.networks, {'app-net'})
        self.assertEqual(self.manifest()['named_volumes'], ['api-data'])
        state = appctl.load(self.state / 'demo/state.json', {})
        self.assertEqual(set(state), {'web.container'})
        self.assertTrue((self.apps / 'demo/compose.yml').is_file())
        self.assertEqual(self.deploy(), 'success')
        self.assertIn('api.service', self.running)

    def test_remove_with_flags_preserves_resources_for_other_stopped_services(self):
        bind = self.base / 'config.json'
        bind.write_text('{}')
        self.desired = {
            'api.container': quadlet(
                f'Volume={bind}:/config:ro\nVolume=shared.volume:/shared\n'
                'Mount=type=volume,source=api-data,target=/data\n'
                'Network=shared.network:alias=api\nNetwork=api.network\n'
            ),
            'web.container': quadlet('Volume=shared-data:/shared\nNetwork=shared-net\n'),
            'shared.volume': '[Volume]\nVolumeName=shared-data\n',
            'shared.network': '[Network]\nNetworkName=shared-net\n',
            'api.network': '[Network]\nNetworkName=api-net\n',
        }
        self.deploy()
        self.system('stop', 'web.service')
        self.networks.update(('api-net', 'shared-net'))
        self.calls.clear()

        report, record = self.remove('api', '--volumes', '--networks')

        self.assertEqual(record['options'], {'volumes': True, 'networks': True})
        self.assertIn('used by web', report)
        self.assertEqual(self.volumes, {'shared-data'})
        self.assertEqual(self.networks, {'shared-net'})
        self.assertFalse((self.quad / 'api.network').exists())
        self.assertTrue((self.quad / 'shared.network').is_file())
        self.assertTrue((self.quad / 'shared.volume').is_file())
        self.assertTrue((self.quad / 'web.container').is_file())
        self.assertEqual(bind.read_text(), '{}')
        self.assertIn(('podman', 'network', 'rm', '--', 'api-net'), self.calls)
        self.assertIn(('podman', 'volume', 'rm', '--', 'api-data'), self.calls)

    def test_project_remove_can_delete_retained_resources_and_skip_in_use_resources(self):
        self.desired = {
            'web.container': quadlet('Volume=data.volume:/data\nVolume=old.volume:/old\nNetwork=app.network\n'),
            'data.volume': '[Volume]\nVolumeName=app-data\n',
            'old.volume': '[Volume]\nVolumeName=old-data\n',
            'app.network': '[Network]\nNetworkName=app-net\n',
        }
        self.deploy()
        self.volumes.update(('app-data', 'old-data-unrelated'))
        self.networks.add('app-net')
        self.remove()
        self.assertEqual(set(self.manifest()['files']), {'data.volume', 'old.volume', 'app.network'})
        self.resources_in_use.add(('network', 'app-net'))

        report, record = self.remove('--volumes', '--networks')

        self.assertEqual(record['result'], 'success')
        self.assertIn('in use by Podman', report)
        self.assertEqual(self.volumes, {'old-data-unrelated'})
        self.assertEqual(self.networks, {'app-net'})
        self.assertEqual(set(self.manifest()['files']), {'app.network'})
        self.resources_in_use.clear()
        self.remove('--networks')
        self.assertFalse(self.networks)
        self.assertEqual(self.manifest()['files'], {})
        self.assertEqual(self.remove()[1]['result'], 'noop')

    def test_remove_rejects_removing_a_dependency_of_a_remaining_service(self):
        self.desired = {
            'db.container': quadlet(),
            'api.container': quadlet(unit='Requires=db.service\n'),
        }
        self.deploy()
        self.calls.clear()

        report, _ = self.remove('db', expected=1)

        self.assertIn('api depends on db.service', report)
        self.assertEqual(self.calls, [])
        self.assertEqual(set(self.running), {'api.service', 'db.service'})

    def test_remove_rejects_a_runtime_container_owned_by_another_unit(self):
        self.desired = {'web.container': quadlet()}
        self.deploy()
        self.calls.clear()
        foreign = {'Config': {'Labels': {'PODMAN_SYSTEMD_UNIT': 'other.service'}}}

        with patch.object(appctl, 'inspect_container', return_value=foreign):
            report, _ = self.remove('web', '--volumes', expected=1)

        self.assertIn('unverified owner', report)
        self.assertEqual(self.calls, [])
        self.assertTrue((self.quad / 'web.container').is_file())

    def test_remove_retries_the_original_resource_selection_after_interruption(self):
        self.desired = {'web.container': quadlet('Volume=app-data:/data\n')}
        self.deploy()
        self.fail_resource_removal.add(('volume', 'app-data'))

        self.remove('web', '--volumes', expected=1)
        self.assertFalse((self.quad / 'web.container').exists())
        self.assertEqual(self.volumes, {'app-data'})
        self.assertEqual(self.manifest()['remove_pending']['services'], ['web.container'])
        self.assertIn('Retry with appctl remove demo web --volumes', self.remove('web', expected=1)[0])
        self.fail_resource_removal.clear()

        self.remove('web', '--volumes')

        self.assertFalse(self.volumes)
        self.assertNotIn('remove_pending', self.manifest())
        self.assertEqual(self.manifest()['files'], {})

    def test_deploy_pulls_missing_and_explicitly_refreshed_images(self):
        self.desired = {'web.container': quadlet()}
        self.image_cached = False
        pull = ('podman', 'pull', '--quiet', 'docker.io/library/nginx:test')
        self.assertEqual(self.deploy(), 'success')
        self.assertEqual(self.pulls, [pull])

        self.calls.clear()
        self.assertEqual(self.deploy(), 'noop')
        self.assertEqual(self.pulls, [pull])
        self.assertEqual(self.deploy(pull=True), 'noop')
        self.assertEqual(self.pulls, [pull, pull])
        self.assertEqual(self.calls, [])

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

    def test_renaming_the_only_service_can_keep_its_container_name(self):
        definition = quadlet('ContainerName=jielong\nVolume=app_data:/data\n')
        self.desired = {'jielong.container': definition}
        self.assertEqual(self.deploy('lx_jielong'), 'success')
        self.calls.clear()
        self.desired = {'lx_jielong.container': definition}
        self.assertEqual(self.deploy('lx_jielong'), 'success')
        self.assertLess(self.calls.index(('stop', 'jielong.service')),
                        self.calls.index(('start', 'lx_jielong.service')))
        self.assertEqual(self.running, {'lx_jielong.service': 'jielong'})
        self.assertFalse((self.quad / 'jielong.container').exists())
        manifest = self.manifest('lx_jielong')
        self.assertEqual(list(manifest['files']), ['lx_jielong.container'])
        self.assertEqual(manifest['named_volumes'], ['app_data'])
        self.assertEqual(self.deploy('lx_jielong'), 'noop')

    def test_rename_can_retry_after_the_replacement_has_started(self):
        definition = quadlet('ContainerName=shared-name\n')
        self.desired = {'old.container': definition}
        self.deploy()
        self.desired = {'new.container': definition}
        with patch.object(appctl, 'verify_services', side_effect=appctl.DeployError('verification interrupted')):
            with self.assertRaisesRegex(appctl.DeployError, 'verification interrupted'):
                self.deploy()
        self.assertEqual(self.running, {'new.service': 'shared-name'})
        self.assertTrue((self.quad / 'old.container').is_file())
        self.assertTrue(self.manifest()['reload_pending'])

        self.assertEqual(self.deploy(), 'success')
        self.assertEqual(self.running, {'new.service': 'shared-name'})
        self.assertFalse((self.quad / 'old.container').exists())
        self.assertFalse(self.manifest()['reload_pending'])

    def test_rename_does_not_take_over_an_unmanaged_container(self):
        definition = quadlet('ContainerName=shared-name\n')
        self.desired = {'old.container': definition}
        self.deploy()
        self.running = {'outside.service': 'shared-name'}
        self.desired = {'new.container': definition}
        self.calls.clear()

        with self.assertRaisesRegex(appctl.DeployError, 'unverified owner: shared-name'):
            self.deploy()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.running, {'outside.service': 'shared-name'})

    def test_container_name_stays_reserved_to_its_project(self):
        definition = quadlet('ContainerName=shared-name\n')
        self.desired = {'old.container': definition}
        self.deploy('first')
        self.desired = {'new.container': definition}
        self.calls.clear()

        with self.assertRaisesRegex(appctl.DeployError, 'owned by project first'):
            self.deploy('second')
        self.assertEqual(self.calls, [])
        self.assertEqual(self.running, {'old.service': 'shared-name'})

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
        with patch.object(appctl, 'now', return_value='2026-11-21T08:00:00+00:00'):
            self.deploy()
        self.desired['z-worker.container'] = quadlet('Environment=MODE=two\n')
        self.stops_on_start['z-worker.service'] = ('a-web.service',)
        with self.assertRaisesRegex(appctl.DeployError, 'Service not active: a-web.service'):
            self.deploy()
        deployed = appctl.load(self.state / 'demo/state.json', {})
        self.assertEqual(deployed['z-worker.container'], {'deployed_at': '2026-11-21T08:00:00+00:00'})

    def test_project_deployment_time_updates_on_removal_but_not_on_noop(self):
        self.desired = {'web.container': quadlet(), 'worker.container': quadlet()}
        initial = '2026-11-20T08:00:00+00:00'
        later = '2026-11-21T08:00:00+00:00'
        with patch.object(appctl, 'now', return_value=initial):
            self.assertEqual(self.deploy(), 'success')
        with patch.object(appctl, 'now', return_value=later):
            self.assertEqual(self.deploy(), 'noop')
            self.assertEqual(self.manifest()['deployed_at'], initial)
            del self.desired['worker.container']
            self.assertEqual(self.deploy(), 'success')
        self.assertEqual(self.manifest()['deployed_at'], later)
        deployed = appctl.load(self.state / 'demo/state.json', {})
        self.assertEqual(deployed['web.container']['deployed_at'], initial)

    def test_status_inspect_and_history_show_current_project_and_service_times(self):
        self.desired = {'web.container': quadlet(), 'worker.container': quadlet()}
        with patch.object(appctl, 'now', return_value='2026-11-20T08:00:00+00:00'):
            self.deploy()
        self.started_at = {
            'web.service': '2026-11-21T07:00:00.123456789Z',
            'worker.service': '2026-11-21T15:58:00+08:00',
        }
        observed = datetime(2026, 11, 21, 8, 0, 0, tzinfo=timezone.utc).timestamp()
        for command in (['status', 'demo', 'web'], ['inspect', 'demo', 'web'], ['history', 'demo']):
            with self.subTest(command=command), redirect_stdout(StringIO()) as output, \
                    patch.object(appctl, 'OUT', appctl.Output()), patch.object(appctl, 'configure'), \
                    patch.object(appctl.shutil, 'which', return_value='/test/command'), \
                    patch.object(appctl.time, 'time', return_value=observed), patch.object(appctl.os, 'umask'), \
                    patch.dict(os.environ):
                self.assertEqual(appctl.main([*command, '--no-color']), 0)
            report = output.getvalue()
            self.assertIn('Project demo  |  Deployed (UTC) 2026-11-20 08:00:00  |  Uptime 2m', report)
            self.assertRegex(report, r'web[^\n]+2026-11-20 08:00:00\s+59m 59s')
        self.running.pop('worker.service')
        with patch.object(appctl.time, 'time', return_value=observed), \
                patch.object(appctl.shutil, 'which', return_value='/test/podman'):
            _, project_times, service_times = appctl.deployment_times(self.state / 'demo')
        self.assertEqual(project_times, ('2026-11-20 08:00:00', '-'))
        self.assertEqual(service_times['worker.container'], ('2026-11-20 08:00:00', '-'))

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

    def project_dropin(self, filename, content, app='demo'):
        source = self.apps / app / filename
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(content)
        return source

    def test_project_dropins_sync_with_the_generated_service_name(self):
        self.desired = {
            'gitea.container': quadlet('ContainerName=gitea-runtime\n'),
            'mysql.container': quadlet('ServiceName=mysql-db\n'),
        }
        gitea_name = 'gitea.container.d/10-service.conf'
        mysql_name = 'mysql.container.d/10-service.conf'
        gitea = self.project_dropin(gitea_name, '[Service]\nRestartSec=10\nTimeoutStartSec=300\n')
        mysql = self.project_dropin(mysql_name,
                                   '[Container]\nPodmanArgs=--memory=384m --memory-swap=384m\n'
                                   '[Service]\nTimeoutStopSec=120\n')
        local = self.quad / 'gitea.container.d/20-local.conf'
        local.parent.mkdir()
        local.write_text('[Service]\nMemoryHigh=400M\n')

        self.assertEqual(self.deploy(), 'success')
        self.assertEqual(self.preflight_dropins, {
            gitea_name: gitea.read_text(), mysql_name: mysql.read_text(),
            'gitea.container.d/20-local.conf': local.read_text(),
        })
        self.assertEqual(self.manifest()['dropins'], [gitea_name, mysql_name])
        self.assertEqual((self.quad / gitea_name).read_bytes(), gitea.read_bytes())
        self.assertEqual((self.quad / mysql_name).read_bytes(), mysql.read_bytes())
        self.assertEqual(self.deploy(), 'noop')

        self.calls.clear()
        gitea.write_text('[Service]\nRestartSec=15\n')
        self.assertEqual(self.deploy(), 'success')
        self.assertEqual(self.calls, [('preflight',), ('daemon-reload',), ('restart', 'gitea.service')])
        self.assertEqual((self.quad / gitea_name).read_bytes(), gitea.read_bytes())

        (self.quad / gitea_name).write_text('[Service]\nRestartSec=30\n')
        self.calls.clear()
        self.assertEqual(self.deploy(), 'success')
        self.assertEqual(self.calls, [('preflight',), ('daemon-reload',), ('restart', 'gitea.service')])
        self.assertEqual((self.quad / gitea_name).read_bytes(), gitea.read_bytes())

        mysql.unlink()
        self.calls.clear()
        self.assertEqual(self.deploy(), 'success')
        self.assertEqual(self.calls, [('preflight',), ('daemon-reload',), ('restart', 'mysql-db.service')])
        self.assertNotIn(mysql_name, self.preflight_dropins)
        self.assertFalse((self.quad / mysql_name).exists())
        self.assertEqual(self.manifest()['dropins'], [gitea_name])
        self.assertTrue(local.is_file())
        self.assertEqual(self.deploy(), 'noop')

    def test_project_dropins_refuse_existing_unmanaged_file_and_unsafe_flags(self):
        self.desired = {'web.container': quadlet()}
        name = 'web.container.d/10-service.conf'
        source = self.project_dropin(name, '[Container]\nPodmanArgs=--memory=384m --name=other\n')
        with self.assertRaisesRegex(appctl.DeployError, 'Only --memory and --memory-swap'):
            self.deploy()
        self.assertEqual(self.calls, [])
        source.write_text('[Service]\nRestartSec=10\n')
        installed = self.quad / name
        installed.parent.mkdir()
        installed.write_bytes(source.read_bytes())
        with self.assertRaisesRegex(appctl.DeployError, 'Unmanaged drop-in'):
            self.deploy()
        self.assertEqual(installed.read_bytes(), source.read_bytes())
        self.assertFalse(self.manifest())
        self.assertEqual(self.calls, [])

    def test_project_dropins_restore_previous_files_after_live_validation_failure(self):
        self.desired = {'web.container': quadlet()}
        old_name = 'web.container.d/10-service.conf'
        new_name = 'web.container.d/20-service.conf'
        original = '[Service]\nRestartSec=10\n'
        old_source = self.project_dropin(old_name, original)
        self.deploy()
        old_source.unlink()
        new_source = self.project_dropin(new_name, '[Service]\nRestartSec=12\n')
        self.live_invalid.add('web.container')
        self.calls.clear()
        with self.assertRaisesRegex(appctl.DeployError, 'Quadlet generation failed'):
            self.deploy()
        self.assertEqual(self.preflight_dropins, {new_name: new_source.read_text()})
        self.assertEqual((self.quad / old_name).read_text(), original)
        self.assertFalse((self.quad / new_name).exists())
        self.assertEqual(self.calls, [('preflight',), ('daemon-reload',), ('daemon-reload',)])

        self.live_invalid.clear()
        self.assertEqual(self.deploy(), 'success')
        self.assertEqual((self.quad / new_name).read_bytes(), new_source.read_bytes())
        self.assertFalse((self.quad / old_name).exists())
        self.assertEqual(self.deploy(), 'noop')

    def test_removing_services_cleans_up_only_their_managed_dropins(self):
        self.desired = {'old.container': quadlet(), 'web.container': quadlet()}
        old_name = 'old.container.d/10-service.conf'
        web_name = 'web.container.d/10-service.conf'
        old_source = self.project_dropin(old_name, '[Service]\nRestartSec=10\n')
        web_source = self.project_dropin(web_name, '[Service]\nRestartSec=12\n')
        self.deploy()
        local = self.quad / 'old.container.d/20-local.conf'
        local.write_text('[Service]\nMemoryHigh=400M\n')

        self.desired.pop('old.container')
        self.assertEqual(self.deploy(), 'success')
        self.assertFalse((self.quad / old_name).exists())
        self.assertTrue(local.is_file())
        self.assertTrue(old_source.is_file())
        self.assertEqual(self.manifest()['dropins'], [web_name])

        self.remove('web')
        self.assertFalse((self.quad / web_name).exists())
        self.assertTrue(web_source.is_file())
        self.assertEqual(self.manifest()['dropins'], [])


class RuntimeReadTests(unittest.TestCase):
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

    def list_services(self, args, containers, units, *, root=None):
        root = self.root if root is None else root
        with patch.object(appctl, 'ROOT', root), patch.object(appctl, 'QUAD', root / 'quadlet'), \
                patch.object(appctl, 'OUT', appctl.Output()), patch.object(appctl, 'configure'), \
                patch.object(appctl.shutil, 'which', side_effect=lambda name: name), \
                patch.object(appctl, 'run', return_value=SimpleNamespace(stdout=json.dumps(containers))) as run, \
                patch.object(appctl, 'unit_info', side_effect=units.__getitem__) as unit_info, \
                redirect_stdout(StringIO()) as stdout:
            self.assertEqual(appctl.main([*args, '--no-color']), 0)
        self.assertFalse((root / 'deploy.lock').exists())
        self.assertEqual(list(root.glob('*/history/*')), [])
        if units:
            run.assert_called_once_with('podman', 'ps', '--all', '--format', 'json')
        else:
            run.assert_not_called()
        return stdout.getvalue(), [call.args[0] for call in unit_info.call_args_list]

    def test_list_reports_all_managed_services_including_stopped_and_failed(self):
        appctl.save(self.root / 'alpha/manifest.json', {
            'files': {
                'web.container': {'kind': 'container', 'unit': 'web.service', 'resource': 'web-runtime'},
            },
            'named_volumes': [],
        })
        containers = [
            {'Names': ['web-runtime'], 'Labels': {'PODMAN_SYSTEMD_UNIT': 'web.service'}, 'State': 'exited'},
            {'Names': ['api-container'], 'Labels': {'PODMAN_SYSTEMD_UNIT': 'api-custom.service'}, 'State': 'running',
             'Id': 'hidden-id', 'Image': 'hidden-image', 'Command': ['hidden-command']},
            {'Names': ['unmanaged'], 'Labels': None, 'State': 'running'},
        ]
        units = {
            'web.service': {'LoadState': 'loaded', 'SourcePath': str(self.root / 'quadlet/web.container'),
                            'ActiveState': 'failed', 'SubState': 'failed'},
            'api-custom.service': {'LoadState': 'loaded', 'SourcePath': str(self.root / 'quadlet/api.container'),
                                   'ActiveState': 'active', 'SubState': 'running'},
            'worker.service': {'LoadState': 'loaded', 'SourcePath': str(self.root / 'quadlet/worker.container'),
                               'ActiveState': 'inactive', 'SubState': 'dead'},
        }

        report, queried = self.list_services(['list'], containers, units)

        self.assertEqual(queried, ['web.service', 'api-custom.service', 'worker.service'])
        self.assertEqual([line.split() for line in report.splitlines() if '/' in line], [
            ['PROJECT/SERVICE', 'SYSTEMD', 'CONTAINER'],
            ['alpha/web', 'failed/failed', 'exited'],
            ['demo/api', 'active/running', 'running'],
            ['demo/worker', 'inactive/dead', 'absent'],
        ])
        for value in ('hidden-id', 'hidden-image', 'hidden-command', 'unmanaged', 'cache.volume'):
            self.assertNotIn(value, report)

    def test_list_and_ps_support_project_and_service_filters(self):
        units = {
            'api-custom.service': {'LoadState': 'not-found'},
            'worker.service': {'LoadState': 'not-found'},
        }
        report, queried = self.list_services(['list', 'demo'], [], units)
        self.assertEqual(queried, ['api-custom.service', 'worker.service'])
        self.assertRegex(report, r'demo/worker\s+not-found\s+absent')
        report, queried = self.list_services(['ps', 'demo', 'api'], [], units)
        self.assertEqual(queried, ['api-custom.service'])
        self.assertRegex(report, r'demo/api\s+not-found\s+absent')
        self.assertNotIn('demo/worker', report)
        self.assertEqual(self.list_services(['list', 'demo', 'api'], [], units)[0], report)

    def test_list_checks_container_and_unit_ownership(self):
        containers = [{'Names': ['api-container'], 'Labels': None, 'State': 'running'}]
        units = {
            'api-custom.service': {'LoadState': 'loaded', 'SourcePath': str(self.root / 'foreign.container'),
                                   'ActiveState': 'active', 'SubState': 'running'},
        }
        report, _ = self.list_services(['ps', 'demo', 'api'], containers, units)
        self.assertRegex(report, r'demo/api\s+conflict\s+conflict')

    def test_list_with_no_state_does_not_create_it(self):
        missing = self.root / 'missing'
        report, queried = self.list_services(['ps'], [], {}, root=missing)
        self.assertIn('No deployed services', report)
        self.assertEqual(queried, [])
        self.assertFalse(missing.exists())

    def logs(self, *args, source=None, child_status=0, podman_available=False):
        source_args = ['--source', source] if source else []
        executable = 'podman' if source == 'podman' else 'journalctl'
        binaries = {executable}
        if podman_available:
            binaries.add('podman')
        history = self.root / 'demo/history'
        had_history = history.exists()
        with patch.object(appctl, 'ROOT', self.root), patch.object(appctl, 'OUT', appctl.Output()), \
                patch.object(appctl, 'configure'), patch.object(appctl.shutil, 'which', side_effect=lambda name: name if name in binaries else None) as which, \
                patch.object(appctl.subprocess, 'run', return_value=SimpleNamespace(returncode=child_status)) as child, \
                redirect_stdout(StringIO()) as stdout, redirect_stderr(StringIO()) as stderr:
            status = appctl.main(['logs', 'demo', *args, *source_args, '--no-color'])
        self.assertEqual(which.call_args_list[0].args, (executable,))
        child.assert_called_once()
        self.assertFalse((self.root / 'deploy.lock').exists())
        self.assertEqual(history.exists(), had_history)
        return status, child.call_args, stdout.getvalue(), stderr.getvalue()

    @staticmethod
    def container(container_id, unit, driver):
        return {'Id': container_id, 'Config': {'Labels': {'PODMAN_SYSTEMD_UNIT': unit}},
                'HostConfig': {'LogConfig': {'Type': driver}}}

    def test_project_logs_match_each_unit_and_container_as_a_pair(self):
        status, journal, *_ = self.logs()
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
        status, journal, *_ = self.logs('worker', '-f', '-n', '0', '--since', '1 hour ago')
        self.assertEqual(status, 0)
        self.assertEqual(journal.args, ([
            'journalctl', '--no-pager', '--output=with-unit', '--lines=0', '--follow',
            '--since=1 hour ago', '--', f'_UID={os.getuid()}',
            '_SYSTEMD_USER_UNIT=worker.service', 'CONTAINER_NAME=systemd-worker',
        ],))

    def test_journal_errors_and_interrupt_have_appctl_exit_codes(self):
        self.assertEqual(self.logs(child_status=2)[0], 1)
        self.assertEqual(self.logs('-f', child_status=-signal.SIGINT)[0], 130)

    def test_logs_show_existing_deploy_times_on_stderr_and_keep_stdout_for_logs(self):
        project = self.root / 'demo'
        appctl.save(project / 'state.json', {
            'api.container': {'deployed_at': '2026-11-20T08:00:00+00:00'},
            'worker.container': {'deployed_at': '2026-11-20T07:00:00+00:00'},
        })
        appctl.save(project / 'history/001.json', {
            'command': 'deploy', 'result': 'success', 'finished_at': '2026-11-20T09:00:00+00:00',
        })
        appctl.save(project / 'history/002.json', {
            'command': 'deploy', 'result': 'noop', 'finished_at': '2026-11-21T08:00:00+00:00',
        })
        api = self.container('a' * 64, 'api-custom.service', 'journald')
        worker = self.container('b' * 64, 'worker.service', 'journald')
        api['State'] = {'Running': True, 'StartedAt': '2026-11-21T07:50:00Z'}
        worker['State'] = {'Running': True, 'StartedAt': '2026-11-21T07:55:00Z'}
        containers = {'api-container': api, 'systemd-worker': worker}
        observed = datetime(2026, 11, 21, 8, 0, 0, tzinfo=timezone.utc).timestamp()
        with patch.object(appctl, 'inspect_container', side_effect=containers.get), \
                patch.object(appctl.time, 'time', return_value=observed):
            status, _, stdout, stderr = self.logs('api', podman_available=True)
        self.assertEqual(status, 0)
        self.assertEqual(stdout, '')
        self.assertIn('Project demo  |  Deployed (UTC) 2026-11-20 09:00:00  |  Uptime 5m', stderr)
        self.assertRegex(stderr, r'api\s+2026-11-20 08:00:00\s+10m')
        self.assertNotIn('worker', stderr)
        _, _, _, stderr = self.logs('api')
        self.assertIn('Project demo  |  Deployed (UTC) 2026-11-20 09:00:00  |  Uptime -', stderr)

    def test_podman_logs_use_verified_ids_and_native_options_for_mixed_drivers(self):
        ids = ['a' * 64, 'b' * 64]
        containers = {
            'api-container': self.container(ids[0], 'api-custom.service', 'k8s-file'),
            'systemd-worker': self.container(ids[1], 'worker.service', 'journald'),
        }
        with patch.object(appctl, 'inspect_container', side_effect=containers.get):
            status, child, *_ = self.logs('-f', '-n', '0', '--since', '30m', source='podman')
        self.assertEqual(status, 0)
        self.assertEqual(child.args, ([
            'podman', 'logs', '--names', '--timestamps', '--tail=0', '--follow', '--since=30m', '--', *ids,
        ],))

    def test_podman_logs_skip_services_with_no_stored_output(self):
        containers = {
            'api-container': self.container('a' * 64, 'api-custom.service', 'k8s-file'),
            'systemd-worker': self.container('b' * 64, 'worker.service', 'none'),
        }
        with patch.object(appctl, 'inspect_container', side_effect=containers.get):
            status, child, _, stderr = self.logs(source='podman')
        self.assertEqual(status, 0)
        self.assertEqual(child.args[0][-2:], ['--', 'a' * 64])
        self.assertIn('Skipping worker: the none log driver stores no output', stderr)

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

    def stats(self, containers, *args, child_status=0):
        with patch.object(appctl, 'ROOT', self.root), patch.object(appctl, 'OUT', appctl.Output()), \
                patch.object(appctl, 'configure'), patch.object(appctl.shutil, 'which', return_value='/test/podman') as which, \
                patch.object(appctl, 'inspect_container', side_effect=containers.get), \
                patch.object(appctl.subprocess, 'run', return_value=SimpleNamespace(returncode=child_status)) as child, \
                redirect_stdout(StringIO()) as stdout, redirect_stderr(StringIO()) as stderr:
            status = appctl.main(['stats', 'demo', *args, '--no-color'])
        which.assert_called_once_with('podman')
        self.assertFalse((self.root / 'deploy.lock').exists())
        self.assertFalse((self.root / 'demo/history').exists())
        return status, [call.args[0] for call in child.call_args_list], stdout.getvalue(), stderr.getvalue()

    def running_containers(self):
        api = self.container('a' * 64, 'api-custom.service', 'journald')
        worker = self.container('b' * 64, 'worker.service', 'journald')
        for container in (api, worker):
            container['State'] = {'Running': True, 'Status': 'running'}
        return {'api-container': api, 'systemd-worker': worker}

    def test_project_stats_snapshot_targets_only_the_projects_running_containers(self):
        status, commands, _, _ = self.stats(self.running_containers(), '--no-stream')
        self.assertEqual(status, 0)
        self.assertEqual(commands, [['podman', 'stats', '--no-stream', '--no-reset', '--', 'a' * 64, 'b' * 64]])

    def test_single_service_stream_respects_interval_and_interrupt(self):
        status, commands, _, _ = self.stats(self.running_containers(), 'api', '-i', '2',
                                             child_status=-signal.SIGINT)
        self.assertEqual(status, 130)
        self.assertEqual(commands, [['podman', 'stats', '--no-reset', '--interval=2', '--', 'a' * 64]])

    def test_process_snapshots_show_states_and_group_rows_by_service(self):
        status, commands, report, _ = self.stats(self.running_containers(), '--processes')
        details = ['user', 'pid', 'ppid', 'state', 'pcpu', 'etime', 'args']
        self.assertEqual(status, 0)
        self.assertEqual(commands, [['podman', 'top', '--', container_id, *details]
                                    for container_id in ('a' * 64, 'b' * 64)])
        self.assertEqual(report.split(), ['api', 'worker'])

    def test_stats_reports_stopped_services_and_fails_when_none_remain(self):
        containers = self.running_containers()
        containers['api-container']['State'] = {'Running': False, 'Status': 'exited'}
        status, commands, _, warnings = self.stats(containers, '--no-stream')
        self.assertEqual(status, 0)
        self.assertEqual(commands, [['podman', 'stats', '--no-stream', '--no-reset', '--', 'b' * 64]])
        self.assertIn('Skipping api: container is exited', warnings)
        with self.assertRaisesRegex(appctl.DeployError, 'No running containers in demo'):
            self.stats(containers, 'api')


if __name__ == '__main__':
    unittest.main()
