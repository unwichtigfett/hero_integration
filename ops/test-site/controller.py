#!/usr/bin/env python3
"""Cold snapshot controller for the four local containers in compose.yaml (Linux root)."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import re
import stat
import subprocess
import tempfile
import time
from uuid import UUID, uuid4

ROLES = ('backend', 'worker', 'redis', 'db')
DATA = ('db', 'sites', 'redis')
BENCH = '/home/frappe/frappe-bench'
RECEIPT_DEST = '/var/lib/hero-test-mutation-receipts'
DOCKER = ('docker', '--host', 'unix:///var/run/docker.sock')


def command(*args, **options):
    return subprocess.check_output(args, **options).decode().strip()


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def write_json(path, value, mode=0o600):
    with tempfile.NamedTemporaryFile('w', dir=path.parent, delete=False) as out:
        json.dump(value, out, sort_keys=True)
        out.flush()
        os.fchmod(out.fileno(), mode)
        os.fsync(out.fileno())
    os.replace(out.name, path)
    fd = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def bounded_request(path):
    fd = os.open(path, os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as f:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and 0 < info.st_size < 4096, 'Invalid reset request file')
        return json.loads(f.read(4096))


class Sandbox:
    def __init__(self, env, state, *, dev=None):
        self.env = UUID(hex=env).hex
        require(env == self.env, 'Use the canonical 32-character environment UUID')
        self.site = f'hero-test-{env}.localhost'
        self.database = 'ht_' + env[:20]
        self.project = f'hero-test-{env}'
        self.dev = dev is not None
        self.apps = ('backend', 'worker')
        self.stores = ('db', 'redis')
        self.data = {r:r for r in DATA}
        self.consumers = ('worker',)
        if dev:
            self.project, self.site, self.database = dev
            require(re.fullmatch(r'[a-z0-9][a-z0-9_-]*', self.project)
                    and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]*', self.site)
                    and re.fullmatch(r'[A-Za-z0-9_]+', self.database), 'Invalid Dev target')
            self.consumers = ('queue-short', 'queue-long')
            self.apps = (*self.consumers, 'backend', 'websocket', 'frontend', 'scheduler')
            self.stores = ('db', 'redis-cache', 'redis-queue')
            self.data = {'db':'db-data', 'sites':'sites', 'redis-cache':'redis-cache-data', 'redis-queue':'redis-queue-data', 'logs':'logs'}
        self.state = state.resolve(strict=True)
        require(os.geteuid() == 0 and self.state.stat().st_uid == 0 and
                not self.state.stat().st_mode & 0o022, 'Controller state requires a trusted root-owned directory')
        self.public = self.state / 'public'
        require(not self.public.is_symlink() and self.public.stat().st_uid == 0 and
                not self.public.stat().st_mode & 0o022, 'Untrusted control directory')
        self.marker = self.public / 'sandbox.json'
        self.baseline = self.state / 'snapshot'
        self.journal = self.state / 'reset.json'

    def check_receipt_mounts(self, container, sender):
        """Only the shared Dev queue workers may mount the campaign receipt directory."""
        source = self.state / 'mutation-receipts'
        admitted = False
        for mount in container.get('Mounts', []):
            dest = Path(mount['Destination'])
            host_path = Path(mount['Source']).resolve() if mount.get('Source') else None
            overlaps_source = host_path and (host_path.is_relative_to(source) or source.is_relative_to(host_path))
            if not overlaps_source and not dest.is_relative_to(RECEIPT_DEST):
                continue
            require(sender and dest == Path(RECEIPT_DEST) and mount['Type'] == 'bind'
                    and mount['RW'] and mount.get('Source') == str(source)
                    and source.resolve(strict=True) == source and source.is_dir(),
                    'Invalid Dev mutation receipt mount')
            admitted = True
        return admitted

    def topology(self):
        """Verify exclusive local volumes, Docker project, mounts and isolated network before every mutation."""
        project = self.project
        network = project + ('_private' if self.dev else '_isolated')
        self.network = network
        net = json.loads(command(*DOCKER, 'network', 'inspect', network))[0]
        require(net['Internal'] and net['Labels'].get('com.docker.compose.project') == project,
                'Network is not a dedicated internal Compose network')
        names = command(*DOCKER, 'ps', '-aq', '--no-trunc').splitlines()
        containers = json.loads(command(*DOCKER, 'inspect', *names))
        self.units = {}
        self.roots = {}
        self.volumes = {k: project + '_' + v for k,v in (self.data | {'inbox':'inbox'}).items()}
        rootdir = Path(command(*DOCKER, 'info', '-f', '{{.DockerRootDir}}')).resolve()
        for role, name in self.volumes.items():
            vol = json.loads(command(*DOCKER, 'volume', 'inspect', name))[0]
            path = Path(vol['Mountpoint'])
            require(vol['Driver'] == 'local' and not vol['Options'] and path.resolve() == path
                    and path == rootdir / 'volumes' / name / '_data'
                    and vol['Labels'].get('com.docker.compose.project') == project
                    and vol['Labels'].get('com.docker.compose.volume') == self.data.get(role, role), 'Unsafe volume ' + role)
            self.roots[role] = path
        for root in self.roots.values():
            require(not (self.state == root or self.state.is_relative_to(root) or root.is_relative_to(self.state)),
                    'Controller storage overlaps sandbox data')
            for entry in Path('/proc/self/mountinfo').read_text().splitlines():
                target = Path(re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), entry.split()[4]))
                require(not target.is_relative_to(root), 'Nested mount inside sandbox storage')
        mounts = {'db': {'/var/lib/mysql': 'db'}, 'redis': {'/data': 'redis'}}
        for role in ('backend', 'worker'):
            mounts[role] = {BENCH+'/sites': 'sites', '/run/hero-test-requests': 'inbox',
                            '/run/hero-test-state': 'marker'}
        if self.dev:
            app_mounts = mounts['backend']
            mounts = {r: {('/var/lib/mysql' if r == 'db' else '/data'): r} for r in self.stores}
            mounts.update({r: app_mounts for r in ('backend', *self.consumers)})
            mounts.update({r:{BENCH+'/sites':'sites'} for r in ('frontend', 'websocket', 'scheduler')})
            for r in ('backend','websocket','scheduler',*self.consumers):
                mounts[r] = mounts[r] | {BENCH+'/logs':'logs'}
        for container in containers:
            labels = container['Config']['Labels'] or {}
            receipt_mount = False
            if self.dev:
                sender = (labels.get('com.docker.compose.project') == project
                          and labels.get('com.docker.compose.service') in self.consumers)
                receipt_mount = self.check_receipt_mounts(container, sender)
            if labels.get('com.docker.compose.project') != project:
                for m in container.get('Mounts', []):
                    p = Path(m['Source']).resolve()
                    require(not any(p == v or p.is_relative_to(v) or v.is_relative_to(p)
                                    for v in self.roots.values()), 'Another container shares sandbox storage')
                require(network not in container['NetworkSettings']['Networks'], 'Network has another member')
                continue
            role = labels.get('com.docker.compose.service')
            if self.dev and role not in (*self.apps, *self.stores):
                # Configurator and one-off tools may mount site data only while stopped.
                # Network relays do not mount site data and run no jobs.
                if role in ('hero-egress', 'webde-egress'):
                    require(not any(Path(m['Source']).is_relative_to(root) or root.is_relative_to(m['Source'])
                                    for m in container['Mounts'] for root in self.roots.values()),
                            'Relay has an application data mount')
                else:
                    require(not container['State']['Running'], 'Stop auxiliary Dev service before test window: '+str(role))
                continue
            require(role in (*self.apps, *self.stores) and role not in self.units and container['Name'] == f'/{project}-{role}-1',
                    'Unexpected sandbox service')
            self.units[role] = container
            host = container['HostConfig']
            ports = host.get('PortBindings') or {}
            if self.dev:
                require(network in container['NetworkSettings']['Networks'], 'Dev service outside its project network')
            else:
                require(host['NetworkMode'] == network and set(container['NetworkSettings']['Networks']) == {network}
                    and host['ReadonlyRootfs'] and not host['Privileged'] and not host.get('Devices')
                    and not host['PidMode'] and not host['CapAdd'] and host['RestartPolicy']['Name'] == 'no'
                    and (not ports or role == 'backend' and set(ports) == {'8000/tcp'} and all(
                        p['HostIp'] == '127.0.0.1' for p in ports['8000/tcp'])), 'Unsafe service isolation')
            if not self.dev and role in ('backend', 'worker'):
                require(host['ReadonlyRootfs'] and container['Config']['User'] == 'frappe', 'Unsafe application filesystem')
            actual = {m['Destination']: m for m in container['Mounts'] if m['Type'] != 'tmpfs'}
            if self.dev:
                # Dev also mounts operational logs, TLS configuration and a DB root secret.
                # Never allow another mount to shadow part of a snapshotted tree.
                extras = set(actual) - set(mounts[role])
                if receipt_mount:
                    extras.remove(RECEIPT_DEST)
                require(all(not actual[p]['RW'] for p in extras)
                        and not any(Path(p).is_relative_to(dest) for p in extras for dest in mounts[role]),
                        'Unsupported writable or nested mount on '+role)
            else:
                require(set(actual) == set(mounts[role]), 'Unexpected mounts on '+role)
            for dest, key in mounts[role].items():
                m = actual[dest]
                if key == 'marker':
                    require(m['Type'] == 'bind' and not m['RW'] and m['Source'] == str(self.public), 'Unsafe state mount')
                else:
                    require(m['Type'] == 'volume' and m['Name'] == self.volumes[key], 'Unexpected volume on '+role)
        require(set(self.units) == set(self.apps + self.stores), 'Incomplete sandbox')
        require(all(self.units[r]['Image'] == self.units['backend']['Image'] for r in self.consumers), 'App image mismatch')
        if self.dev:
            scheduler = self.units['scheduler']
            require(scheduler['Image'] == self.units['backend']['Image']
                    and scheduler['Config']['Cmd'] == ['bench', 'schedule'], 'Unexpected Dev scheduler')
        self.inbox = self.roots['inbox']
        self.pending = self.inbox / 'pending.json'

    def run(self, action, *roles):
        # RQ handles SIGTERM as a warm shutdown. Never kill a running migration on a timeout.
        timeout = ('--time', '-1') if action == 'stop' and set(roles).intersection(self.consumers) else ()
        command(*DOCKER, action, *timeout, *(self.units[r]['Id'] for r in roles))

    def quiesce(self):
        producers = [r for r in reversed(self.apps) if r not in self.consumers]
        self.run('stop', *producers)
        self.run('stop', *self.consumers)

    def stopped(self):
        for role in self.apps + self.stores:
            state = command(*DOCKER, 'inspect', '-f', '{{.State.Running}}', self.units[role]['Id'])
            require(state == 'false', 'A sandbox writer is still running')

    def check_site(self):
        root = self.roots['sites']
        locations = list(root.glob('*/site_config.json'))
        require(locations == [root / self.site / 'site_config.json'] and not locations[0].is_symlink()
                and locations[0].resolve() == locations[0], 'Expected exactly one dedicated local site')
        conf = json.loads(locations[0].read_text())
        common = json.loads((root/'common_site_config.json').read_text())
        merged = {**common, **conf}
        require(isinstance(conf.get('encryption_key'), str) and bool(conf['encryption_key']), 'Site key missing')
        if self.dev:
            require(merged.get('hero_exclusive_test_environment') == self.env
                    and merged.get('pause_scheduler') in (1, '1') and merged.get('mute_emails') in (1, '1'),
                    'Explicit Dev window, paused scheduler and muted mail required')
        cache = 'redis://redis-cache:6379' if self.dev else 'redis://redis:6379/0'
        queue = 'redis://redis-queue:6379' if self.dev else 'redis://redis:6379/1'
        require(merged['db_name'] == self.database and merged['db_host'] == 'db'
                and merged['redis_queue'] == queue and merged['redis_cache'] == cache, 'Site points outside its storage')

    def verify_sql(self, *, clean=False):
        """No web, worker or scheduler can write during this probe."""
        for _ in range(60):
            if subprocess.run([*DOCKER, 'exec', self.units['db']['Id'], 'healthcheck.sh', '--connect'],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
                break
            time.sleep(1)
        schemas = command(*DOCKER, 'exec', self.units['db']['Id'], 'sh', '-ec',
                         'MYSQL_PWD="${MARIADB_ROOT_PASSWORD:-$(cat "$MARIADB_ROOT_PASSWORD_FILE")}" mariadb -uroot -NBe "SHOW DATABASES"').splitlines()
        require(set(schemas) - {'mysql','information_schema','performance_schema','sys'} == {self.database},
                'Unexpected databases in the sandbox volume')
        data = command(*DOCKER, 'run', '--rm', '--network', self.network, '--read-only', '--user', 'frappe',
                       '--tmpfs', '/tmp', '--tmpfs', BENCH+'/logs:uid=1000,gid=1000',
                       '-v', self.volumes['sites']+':'+BENCH+'/sites',
                       '--entrypoint', '/usr/local/bin/bench', self.units['backend']['Image'],
                       '--site', self.site, 'execute', 'hero_integration.services.test_environment.baseline_inventory')
        counts = json.loads(data.splitlines()[-1])
        require(counts and all(type(v) is int and v >= 0 for v in counts.values()), 'Invalid site inventory')
        require(not any(counts[k] for k in ('active_or_queued','running_jobs','waiting_jobs')),
                'Wait for migrations and queue jobs to finish')
        if clean and not self.dev:
            require(not any(counts.values()), 'Dedicated baseline contains test data')
            booked = command(*DOCKER, 'exec', self.units['redis']['Id'], 'redis-cli', '-n', '1',
                             '--scan', '--pattern', 'rq:job:*')
            require(not booked, 'The dedicated baseline contains retained jobs')
        return counts

    def identity(self):
        return {r: [self.units[r]['Id'], self.units[r]['Image']] for r in self.apps + self.stores}

    @staticmethod
    def archive_args(root):
        return ['tar', '--format=gnu', '--numeric-owner', '--sort=name', '--one-file-system', '-C', str(root), '-cf']

    def seal(self):
        if self.baseline.exists() and not self.marker.exists() and not self.journal.exists():
            manifest = json.loads((self.baseline/'manifest.json').read_text())
            require(manifest['units'] == self.identity() and manifest['environment'] == self.env and
                    all(digest(self.baseline/(r+'.tar')) == manifest['hashes'][r] for r in self.data),
                    'Cannot recover incomplete snapshot activation')
            # Never trust the intervening live volumes after an interrupted first activation.
            job = dict(generation=uuid4().hex, old_generation=uuid4().hex, baseline=digest(self.baseline/'manifest.json'))
            write_json(self.journal, job)
            return self.restore()
        require(not self.baseline.exists() and not self.marker.exists() and not self.journal.exists(),
                'A sealed baseline or unfinished reset already exists')
        self.check_site()
        self.run('start', *self.stores)
        self.verify_sql(clean=True)
        self.quiesce()
        counts = self.verify_sql(clean=True)
        self.run('stop', *reversed(self.stores))
        self.stopped()
        incoming = Path(tempfile.mkdtemp(dir=self.state, prefix='seal-'))
        hashes = {}
        for role in self.data:
            path = incoming/(role+'.tar')
            subprocess.run([*self.archive_args(self.roots[role]), str(path), '.'], check=True)
            os.chmod(path, 0o600)
            with path.open('rb') as archive:
                os.fsync(archive.fileno())
            hashes[role] = digest(path)
        manifest = {'environment': self.env, 'site': self.site, 'database': self.database,
                    'units': self.identity(), 'volumes': self.volumes, 'hashes': hashes, 'inventory': counts}
        write_json(incoming/'manifest.json', manifest)
        os.rename(incoming, self.baseline)
        directory = os.open(self.state, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        os.chown(self.inbox, 1000, 1000)
        os.chmod(self.inbox, 0o700)
        self.activate(uuid4().hex)
        return hashes

    def activate(self, generation, *, before_ready=None):
        self.run('start', *self.stores)
        counts = self.verify_sql()
        expected = json.loads((self.baseline/'manifest.json').read_text())['inventory']
        require(counts == expected, 'Post-restore inventory differs from baseline')
        self.run('start', *self.apps)
        for role in self.apps:
            require(command(*DOCKER,'inspect','-f','{{.State.Running}}',self.units[role]['Id']) == 'true',
                    'Sandbox process did not start: '+role)
        if before_ready:
            before_ready(counts)
        write_json(self.marker, self.marker_data('ready', generation), 0o644)
        return counts

    def marker_data(self, phase, generation):
        return dict(environment=self.env, database=self.database, site=self.site, phase=phase,
                    generation=generation, baseline=digest(self.baseline/'manifest.json'))

    def restore(self):
        if self.journal.exists():
            job = json.loads(self.journal.read_text())
        else:
            if not self.pending.exists():
                return {'status': 'idle'}
            request = bounded_request(self.pending)
            marker = json.loads(self.marker.read_text())
            require(marker == self.marker_data('ready', marker['generation']) and
                    request == {k: marker[k] for k in ('environment','generation','baseline','site')} | {'actor':'Administrator'},
                    'Invalid or stale reset request')
            self.check_site()
            self.verify_sql()
            # Close ingress and drain workers before declaring that a rollback may start.
            # On failure the volumes are still intact; resume only after operator review.
            self.quiesce()
            self.verify_sql()
            job = {'old_generation': marker['generation'], 'generation': uuid4().hex, 'baseline': marker['baseline']}
            write_json(self.journal, job)
        manifest = json.loads((self.baseline/'manifest.json').read_text())
        require(job['baseline'] == digest(self.baseline/'manifest.json') and manifest['environment'] == self.env
                and manifest['site'] == self.site and manifest['database'] == self.database
                and manifest['units'] == self.identity() and manifest['volumes'] == self.volumes, 'Snapshot identity changed')
        # A crash after publishing a new generation must never rewind that generation a second time.
        receipt = self.state/'last-reset.json'
        if receipt.exists() and self.marker.exists():
            last = json.loads(receipt.read_text())
            if (last.get('generation') == job['generation'] and
                    json.loads(self.marker.read_text()) == self.marker_data('ready', job['generation'])):
                self.journal.unlink()
                return last
        write_json(self.marker, self.marker_data('restoring', job['old_generation']), 0o644)
        self.quiesce()
        self.run('stop', *reversed(self.stores))
        self.stopped()
        for role in self.data:
            require(digest(self.baseline/(role+'.tar')) == manifest['hashes'][role], 'Damaged snapshot: '+role)
        verified = {}
        for role in self.data:
            root = self.roots[role]
            for path in root.iterdir():
                if path.is_symlink() or not path.is_dir():
                    path.unlink()
                else:
                    shutil.rmtree(path)
            subprocess.run(['tar','--numeric-owner','-C',str(root),'-xf',str(self.baseline/(role+'.tar'))],check=True)
            with subprocess.Popen([*self.archive_args(root), '-', '.'], stdout=subprocess.PIPE) as proc:
                verified[role] = hashlib.file_digest(proc.stdout, 'sha256').hexdigest()
                require(proc.wait() == 0 and verified[role] == manifest['hashes'][role], 'Cold restore mismatch: '+role)
        self.check_site()
        # No producer is running. Remove unpublished request temporaries left by a killed backend as well.
        for item in self.inbox.iterdir():
            require(item.is_symlink() or not item.is_dir(), 'Unexpected directory in reset inbox')
            item.unlink()
        os.chown(self.inbox, 1000, 1000)
        os.chmod(self.inbox, 0o700)
        def persist_receipt(counts):
            result = dict(status='restored', generation=job['generation'], baseline=job['baseline'],
                          volumes=verified, inventory=counts)
            write_json(receipt, result)
        self.activate(job['generation'], before_ready=persist_receipt)
        result = json.loads(receipt.read_text())
        self.journal.unlink()
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('seal','process','serve'))
    parser.add_argument('--environment', required=True)
    parser.add_argument('--state', required=True, type=Path)
    parser.add_argument('--dev', nargs=3, metavar=('PROJECT','SITE','DATABASE'),
                        help='Use the named exclusive VPS1 Dev compose project and site')
    args = parser.parse_args()
    site = Sandbox(args.environment, args.state, dev=args.dev)
    with (site.state/'controller.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        site.topology()
        if args.action == 'seal':
            print(json.dumps(site.seal()))
        else:
            while True:
                if site.journal.exists() or site.pending.exists():
                    site.topology()
                    print(json.dumps(site.restore()), flush=True)
                elif args.action == 'process':
                    print(json.dumps({'status':'idle'}))
                if args.action == 'process':
                    return
                time.sleep(1)


if __name__ == '__main__':
    main()
