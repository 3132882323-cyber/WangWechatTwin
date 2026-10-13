"""Single local supervisor. Restarts crashes; never submits or sends messages."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def supervise(command, root, *, launch=subprocess.Popen, wait=time.sleep, max_runs=None,
              healthy=None, terminate=None, watchdog_check=None, watchdog_checks=None):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    stop = root / 'STOP_SUPERVISOR'
    runs, delay = 0, 10
    checks = dict(watchdog_checks or {})
    if watchdog_check is not None:
        checks['deepseek'] = watchdog_check
    if set(checks) - {'deepseek', 'glm'}:
        raise ValueError('unsupported_watchdog')

    def check_watchdog():
        for name, callback in checks.items():
            try:
                callback()
            except Exception:
                # One provider's monitor failure cannot stop another monitor or
                # restart the backend. Logs contain fixed codes only.
                code = name + '_watchdog_check_failed'
                event = {'at': time.time(), 'event': code}
                try:
                    with (root / 'startup.jsonl').open('a', encoding='utf-8') as log:
                        log.write(json.dumps(event) + '\n')
                except OSError:
                    print(code, file=sys.stderr, flush=True)

    while not stop.exists() and (max_runs is None or runs < max_runs):
        from app.resource_health import available_commit_bytes, under_pressure
        if under_pressure(available_commit_bytes()):
            wait(30)
            continue
        check_watchdog()
        started = time.monotonic()
        stamp = time.strftime('%Y%m%d-%H%M%S')
        with (root / ('backend-' + stamp + '.log')).open('ab') as log:
            child = launch(command, stdout=log, stderr=subprocess.STDOUT,
                           env={**os.environ, 'PYTHONIOENCODING': 'utf-8'},
                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            (root / 'supervisor.json').write_text(json.dumps({'pid': os.getpid(), 'child_pid': child.pid, 'started': time.time()}), encoding='utf-8')
            if healthy is None and not checks:
                code = child.wait()
            else:
                while True:
                    try:
                        code = child.wait(timeout=30)
                        break
                    except subprocess.TimeoutExpired:
                        check_watchdog()
                        if healthy is not None and time.monotonic() - started > 600 and not healthy(child.pid):
                            (terminate or (lambda process: process.terminate()))(child)
                            try:
                                child.wait(timeout=30)
                            except subprocess.TimeoutExpired:
                                child.kill()
                                child.wait()
                            code = 3
                            break
        runs += 1
        with (root / 'restarts.jsonl').open('a', encoding='utf-8') as log:
            log.write(json.dumps({'at': time.time(), 'exit_code': code, 'child_pid': child.pid}) + '\n')
        # A normal user stop remains stopped. API pause is never changed here.
        if code == 0 or stop.exists():
            break
        if time.monotonic() - started > 300:
            delay = 10
        wait(delay)
        delay = min(delay * 2, 120)
    return runs


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='config.http-api.yaml')
    args = parser.parse_args()
    from app.config import load_config
    from app.instance_lock import SingleInstanceLock
    config = load_config(args.config)
    root = config.resolve('.runtime/startup')
    lock = SingleInstanceLock(root / 'supervisor.lock')
    if not lock.acquire():
        reason = getattr(lock, 'failure_reason', None) or 'unknown'
        diagnostic = getattr(lock, 'diagnostic', None) or reason
        event = {'at': time.time(), 'pid': os.getpid(),
                 'event': 'supervisor_lock_rejected', 'reason': reason,
                 'diagnostic': str(diagnostic)}
        print(f"supervisor lock rejected ({reason}): {diagnostic}", file=sys.stderr, flush=True)
        try:
            root.mkdir(parents=True, exist_ok=True)
            with (root / 'startup.jsonl').open('a', encoding='utf-8') as log:
                log.write(json.dumps(event, ensure_ascii=False) + '\n')
        except OSError as exc:
            print(f"supervisor startup log unavailable: {exc}", file=sys.stderr, flush=True)
        return 0 if reason in {'locked', 'legacy_owner_active'} else 2
    try:
        def healthy(pid):
            import sqlite3
            try:
                with sqlite3.connect('file:' + str(config.resolve(config.paths.database)) + '?mode=ro', uri=True, timeout=2) as connection:
                    row = connection.execute("SELECT value FROM state WHERE key='runtime_heartbeat'").fetchone()
                pulse = json.loads(row[0]) if row else {}
                return owns_process(pid, pulse.get('pid')) and time.time() - pulse.get('seen_at', 0) < 600
            except (OSError, ValueError, sqlite3.Error):
                # A transient DB lock alone must never terminate the backend.
                return True
        supervise([sys.executable, '-u', '-m', 'app', '--config', args.config, 'run'], root,
                  healthy=healthy, terminate=terminate_owned, watchdog_checks=provider_watchdog_checks(config, root))
        return 0
    finally:
        lock.release()


def provider_watchdog_checks(config, root: Path, *, platform_name=None):
    """Enable only configured Python monitors; Doubao startup is never added."""
    if (platform_name or os.name) != 'nt' or config.openai.provider != 'hybrid_web':
        return {}
    from app.local_proxy_watchdog import ensure_deepseek_watchdog, ensure_glm_watchdog
    checks = {}
    root = Path(root)
    disabled = config.openai.disabled_lanes
    deepseek = config.openai.local_proxies.get('deepseek')
    ds_script = root / 'deepseek_watchdog.py'
    if (deepseek is not None and deepseek.enabled and 'deepseek' not in disabled and ds_script.is_file()
            and not (root / 'STOP_DEEPSEEK_PROXY').exists()):
        checks['deepseek'] = lambda: ensure_deepseek_watchdog(ds_script)
    glm = config.openai.local_proxies.get('glm')
    glm_script = root.parents[1] / 'scripts' / 'glm_watchdog.py'
    if (glm is not None and glm.enabled and 'glm' not in disabled and glm_script.is_file()
            and not (root / 'STOP_GLM_PROXY').exists()):
        checks['glm'] = lambda: ensure_glm_watchdog(glm_script, root.parent / 'glm_proxy')
    return checks


def owns_process(launcher_pid, runtime_pid):
    if runtime_pid == launcher_pid:
        return True
    if not isinstance(runtime_pid, int):
        return False
    import psutil
    try:
        return any(parent.pid == launcher_pid for parent in psutil.Process(runtime_pid).parents())
    except psutil.Error:
        return False


def terminate_owned(child):
    # Windows venv launchers have a separate Python child. Stop this owned tree only.
    import psutil
    try:
        descendants = psutil.Process(child.pid).children(recursive=True)
        for process in reversed(descendants):
            try:
                process.terminate()
            except psutil.NoSuchProcess:
                pass
    except psutil.NoSuchProcess:
        pass
    child.terminate()


if __name__ == '__main__':
    sys.exit(main())
