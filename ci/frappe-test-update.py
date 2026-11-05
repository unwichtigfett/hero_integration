"""Host-side updater for the dedicated Woodpecker test site."""
import datetime
import fcntl
import gzip
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time

ROOT = Path('/srv/frappe-test')
SITE = ROOT / 'sites/frappe-test'
ENVFILE = ROOT / '.env'
APPSFILE = ROOT / 'sites/apps.txt'
CONFIG = SITE / 'site_config.json'
COMMON = ROOT / 'sites/common_site_config.json'
REPO = 'https://github.com/unwichtigfett/hero_integration.git'
REF = 'refs/heads/hero_int_dev'
SERVICES = ['frontend', 'websocket', 'scheduler', 'queue-short', 'queue-long']
FAILURE_STATE = Path('/run/frappe-test-auto-update-failure.json')
LOCKFILE = Path('/run/lock/frappe-test.lock')
ENV = os.environ.copy()
ENV.pop('FRAPPE_TEST_IMAGE', None)
ENV.pop('INSTALL_APPS', None)
COMPOSE = ['docker', 'compose', '-p', 'frappe-test', '-f', '/srv/frappe-test-ctl/compose.yaml', '--env-file', str(ENVFILE)]
log = None
backup = None
maintenance = False
stopped = False
switched = False
migration_started = False
old_env = None


def update_request(command):
    """Only the CI command requests a reset; the timer remains incremental."""
    if command == '':
        return False, None
    # Existing deployed pipelines use this forced SSH command. Keep them on the
    # same blank-site policy while the revision-pinned workflow is rolled out.
    if command == 'update':
        return True, None
    match = re.fullmatch(r'update:blank:([a-f0-9]{40})', command)
    if not match:
        raise RuntimeError('invalid test-site update request')
    return True, match[1]


def reset_database():
    values = dict(line.split('=', 1) for line in ENVFILE.read_text().splitlines() if '=' in line)
    # Feed credentials through stdin, never command arguments or operation logs.
    source = Path(__file__).with_name('reset-test-site.py').read_text()
    request = json.dumps({'site': 'frappe-test', 'db_root_password': values['DB_ROOT_PASSWORD']})
    # Workers are stopped. This Redis belongs exclusively to the test stack;
    # clear queued and deferred jobs so old work cannot refill the new database.
    run(['docker', 'exec', 'frappe-test-redis-queue-1', 'redis-cli', 'FLUSHDB'])
    run(['docker', 'exec', '-i', '-w', '/home/frappe/frappe-bench/sites', 'frappe-test-backend-1',
         '/home/frappe/frappe-bench/env/bin/python', '-c', source], timeout=1800, input_text=request)


def say(message):
    print(message, flush=True)


def run(args, timeout=120, capture=False, input_text=None):
    result = subprocess.run(args, env=ENV, input=input_text, stdout=subprocess.PIPE if capture else log,
                            stderr=subprocess.PIPE if capture else subprocess.STDOUT, timeout=timeout,
                            text=capture or input_text is not None)
    if result.returncode:
        raise RuntimeError(f'{args[0]} failed with exit {result.returncode}')
    return result.stdout if capture else None


def dc(*args, **kwargs):
    return run(COMPOSE + list(args), **kwargs)


def bench(*args, **kwargs):
    return run(['docker', 'exec', '-w', '/home/frappe/frappe-bench', 'frappe-test-backend-1',
                'bench', '--site', 'frappe-test', *args], **kwargs)


def head():
    output = run(['git', 'ls-remote', '--exit-code', REPO, REF], timeout=45, capture=True)
    fields = output.split()
    if len(fields) != 2 or fields[1] != REF or not re.fullmatch('[a-f0-9]{40}', fields[0]):
        raise RuntimeError('invalid upstream branch response')
    return fields[0]


def atomic_write(path, data):
    st = path.stat()
    fd, name = tempfile.mkstemp(prefix='.hero-update-', dir=path.parent)
    try:
        os.fchmod(fd, st.st_mode & 0o7777)
        os.fchown(fd, st.st_uid, st.st_gid)
        with os.fdopen(fd, 'wb') as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def save(src, dest):
    with src.open('rb') as source, dest.open('xb') as target:
        shutil.copyfileobj(source, target)
    os.chmod(dest, 0o600)


def check_image_revision(image, revision):
    actual = run(['docker', 'run', '--rm', '--network', 'none', '--memory', '128m', '--cpus', '0.25',
                  '--entrypoint', 'git', image, '-C', '/home/frappe/frappe-bench/apps/hero_integration',
                  'rev-parse', 'HEAD'], timeout=60, capture=True).strip()
    if actual != revision:
        raise RuntimeError('built image revision mismatch')


def build_image(base, revision, image):
    dockerfile = '''FROM {base}\nUSER frappe\nWORKDIR /home/frappe/frappe-bench\nARG HERO_REV\nRUN rm -rf apps/hero_integration && \\\n    bench get-app --branch hero_int_dev https://github.com/unwichtigfett/hero_integration.git --skip-assets && \\\n    test "$(git -C apps/hero_integration rev-parse HEAD)" = "$HERO_REV" && \\\n    bench build --app hero_integration\n'''.format(base=base)
    run(['docker', 'build', '--resource', 'memory=3g', '--resource', 'cpu-quota=50000',
         '--tag', image, '--build-arg', 'HERO_REV=' + revision, '-'], timeout=1800, input_text=dockerfile)
    check_image_revision(image, revision)
    run(['docker', 'run', '--rm', '--network', 'none', '--memory', '512m', '--cpus', '0.5',
         '--entrypoint', '/home/frappe/frappe-bench/env/bin/python', image,
         '-B', '-m', 'unittest', 'hero_integration.tests.test_app_references'], timeout=180)


def backup_site(db):
    global backup, log
    backups = Path('/root/backups')
    if backups.is_symlink() or backups.resolve() != backups:
        raise RuntimeError('unexpected backup root')
    backup = backups / ('frappe-test-auto-update-' + datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    backup.mkdir(mode=0o700)
    os.chmod(backup, 0o700)
    log = (backup / 'operation.log').open('x')
    os.chmod(backup / 'operation.log', 0o600)
    for src, name in ((ENVFILE, 'stack.env'), (APPSFILE, 'apps.txt'), (CONFIG, 'site_config.json'), (COMMON, 'common_site_config.json')):
        save(src, backup / name)
    say('Backup: ' + str(backup))


def backup_data(db):
    path = backup / 'database.sql.gz'
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as target, gzip.GzipFile(fileobj=target, mode='wb', compresslevel=4) as gz:
        process = subprocess.Popen(['docker', 'exec', 'frappe-test-db-1', 'sh', '-c',
            'export MYSQL_PWD="$MARIADB_ROOT_PASSWORD"; exec mariadb-dump -u root --single-transaction --quick --skip-lock-tables -- "$1"',
            'backup', db], stdout=subprocess.PIPE, stderr=log)
        try:
            shutil.copyfileobj(process.stdout, gz)
            process.stdout.close()
            rc = process.wait(timeout=300)
        except BaseException:
            process.kill()
            process.wait()
            raise
        if rc:
            raise RuntimeError('database dump failed')
    if path.stat().st_size < 512:
        raise RuntimeError('database backup unexpectedly small')
    with gzip.open(path, 'rb') as source:
        while source.read(1024 * 1024):
            pass
    archive = backup / 'site-files.tar'
    fd = os.open(archive, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    run(['tar', '-C', str(SITE), '-cf', str(archive), '.'], timeout=300)
    with tarfile.open(archive) as tar:
        if not any(x.name in ('./site_config.json', 'site_config.json') for x in tar):
            raise RuntimeError('site backup lacks config')


def main():
    global old_env, maintenance, stopped, switched, migration_started, log
    revision = None
    lockfd = os.open(LOCKFILE, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lockfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        blank, expected_revision = update_request(ENV.get('SSH_ORIGINAL_COMMAND', ''))
        for path in (ROOT, SITE, ENVFILE, APPSFILE, CONFIG, COMMON):
            if path.is_symlink() or path.resolve() != path:
                raise RuntimeError('unexpected site path')
        if subprocess.run(['mountpoint', '-q', str(ROOT)]).returncode:
            raise RuntimeError('test filesystem is not mounted')
        cfg = json.loads(CONFIG.read_text())
        common = json.loads(COMMON.read_text())
        db = cfg.get('db_name', '')
        if not re.fullmatch('[A-Za-z0-9_]+', db) or cfg.get('maintenance_mode') or common.get('maintenance_mode'):
            raise RuntimeError('invalid database name or existing maintenance mode')
        apps = APPSFILE.read_text().splitlines()
        if 'hero_integration' not in apps or 'erpnext' not in apps:
            raise RuntimeError('HERO and ERPNext must be installed')
        listing = bench('list-apps', timeout=60, capture=True)
        for app in ('frappe', 'erpnext', 'hero_integration'):
            if not re.search(r'^\s*' + app + r'\s', listing, re.M):
                raise RuntimeError('required app is not installed on the site')
        old_env = ENVFILE.read_bytes()
        lines = old_env.decode().splitlines()
        image_lines = [x for x in lines if x.startswith('FRAPPE_TEST_IMAGE=')]
        if len(image_lines) != 1 or not re.fullmatch('FRAPPE_TEST_IMAGE=frappe-test-apps:[a-f0-9]{20}', image_lines[0]):
            raise RuntimeError('unexpected saved image')
        old_image = image_lines[0].split('=', 1)[1]
        saved_id = run(['docker', 'image', 'inspect', old_image, '--format', '{{.Id}}'], capture=True).strip()
        runtime_id = run(['docker', 'inspect', 'frappe-test-backend-1', '--format', '{{.Image}}'], capture=True).strip()
        if saved_id != runtime_id:
            raise RuntimeError('running backend does not use saved image')
        old_revision = run(['docker', 'exec', '-w', '/home/frappe/frappe-bench/apps/hero_integration',
                            'frappe-test-backend-1', 'git', 'rev-parse', 'HEAD'], capture=True).strip()
        revision = head()
        if expected_revision and expected_revision != revision:
            raise RuntimeError('requested pipeline revision is no longer the development head')
        if old_revision == revision and not blank:
            say('HERO is current: ' + revision)
            return
        if FAILURE_STATE.is_symlink():
            raise RuntimeError('unsafe retry state path')
        if FAILURE_STATE.exists():
            retry = json.loads(FAILURE_STATE.read_text())
            if not blank and retry.get('revision') == revision and retry.get('retry_after', 0) > time.time():
                say('Previous update failed; retry deferred for this revision')
                return
        if old_revision != revision and shutil.disk_usage('/').free < 15 * 1024**3:
            raise RuntimeError('less than 15 GiB free for image build')
        if not re.fullmatch('[a-f0-9]{40}', old_revision):
            raise RuntimeError('invalid installed revision')
        image = old_image
        if old_revision != revision:
            image = 'frappe-test-apps:' + revision[:20]
            say('Building HERO revision: ' + revision)
            build_image(old_image, revision, image)
        if head() != revision:
            raise RuntimeError('upstream advanced during build; retry on next timer')
        bench('ready-for-migration', timeout=120)
        if blank:
            say('Pipeline reset: no database, file or Docker backup will be created')
        else:
            backup_site(db)
        maintenance = True
        bench('set-maintenance-mode', 'on')
        stopped = True
        dc('stop', *SERVICES, timeout=180)
        if not blank:
            backup_data(db)
            say('Backup verified; switching test site image')
        if ENVFILE.read_bytes() != old_env:
            raise RuntimeError('saved environment changed during backup')
        if head() != revision:
            raise RuntimeError('upstream advanced during backup; retry on next timer')
        new = '\n'.join(('FRAPPE_TEST_IMAGE=' + image) if x.startswith('FRAPPE_TEST_IMAGE=') else x for x in lines) + '\n'
        switched = True
        atomic_write(ENVFILE, new.encode())
        dc('up', '-d', '--no-deps', '--pull', 'never', 'backend', timeout=180)
        installed = run(['docker', 'exec', '-w', '/home/frappe/frappe-bench/apps/hero_integration',
                         'frappe-test-backend-1', 'git', 'rev-parse', 'HEAD'], capture=True).strip()
        if installed != revision:
            raise RuntimeError('running backend revision mismatch')
        migration_started = True
        if blank:
            say('Reinstalling blank test database, preserving users')
            reset_database()
        else:
            say('Migrating test site')
            bench('migrate', timeout=1200)
        result = json.loads(bench('execute', 'hero_integration.app_setup.verify_install', timeout=120, capture=True))
        if result.get('missing') or result.get('unsafe_defaults'):
            raise RuntimeError('HERO verification failed after migration')
        dc('up', '-d', '--no-deps', '--pull', 'never', *SERVICES, timeout=180)
        stopped = False
        bench('set-maintenance-mode', 'off')
        maintenance = False
        run(['curl', '--fail', '--silent', '--show-error', '--retry', '15', '--retry-delay', '2', '--retry-connrefused',
             '--max-time', '5', '-H', 'Host: frappe-test', 'http://127.0.0.1:8090/api/method/ping'], timeout=120)
        say('HERO update successful: ' + revision)
    except Exception as exc:
        say('HERO update failed: ' + str(exc))
        if backup:
            say('Backup retained: ' + str(backup))
        if revision and not migration_started:
            try:
                if FAILURE_STATE.is_symlink():
                    raise RuntimeError('unsafe retry state path')
                fd, name = tempfile.mkstemp(prefix='.frappe-test-auto-update-', dir='/run')
                with os.fdopen(fd, 'w') as state:
                    json.dump({'revision': revision, 'retry_after': time.time() + 3600}, state)
                os.chmod(name, 0o600)
                os.replace(name, FAILURE_STATE)
            except Exception:
                say('Could not write retry backoff state')
        if migration_started:
            # Database migrations may be partial. Do not restart workers or silently restore.
            if not maintenance:
                try:
                    bench('set-maintenance-mode', 'on')
                    maintenance = True
                except Exception:
                    say('Could not re-enable maintenance; inspect site immediately')
            if not stopped:
                try:
                    dc('stop', *SERVICES, timeout=180)
                    stopped = True
                except Exception:
                    say('Could not stop all services; inspect site immediately')
            say('Migration may have changed data; maintenance retained for manual recovery')
        else:
            try:
                if switched:
                    atomic_write(ENVFILE, old_env)
                    dc('up', '-d', '--no-deps', '--pull', 'never', 'backend', timeout=180)
                if stopped or switched:
                    dc('up', '-d', '--no-deps', '--pull', 'never', *SERVICES, timeout=180)
                if maintenance:
                    bench('set-maintenance-mode', 'off')
                say('Pre-migration rollback completed')
            except Exception as recovery:
                say('Pre-migration rollback failed: ' + str(recovery))
        sys.exit(1)
    finally:
        if log:
            log.close()
        os.close(lockfd)


if __name__ == '__main__':
    main()
