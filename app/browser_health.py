"""Bound failure pauses without undoing a pause requested by the owner."""
import math
import os
import threading
import time
import uuid

AUTO_RESUME_COOLDOWN_SECONDS = 300
_PAUSE_PREFIX = 'browser-failure-pause:'
_STATE_LOCK = threading.RLock()


def _file_signature(stat):
    # A manual touch, replacement, or content edit revokes our ownership, even
    # when a debugger leaves the original text in the pause file.
    # Python 3.12 on Windows reports different ctime semantics for stat/fstat;
    # inode, size, and last-write time identify the same unchanged file.
    return {key: getattr(stat, key) for key in (
        'st_dev', 'st_ino', 'st_size', 'st_mtime_ns')}


def _matches_pause(target, marker):
    if not isinstance(marker, dict) or not isinstance(marker.get('token'), str):
        return False
    if not marker['token'] or not isinstance(marker.get('file_signature'), dict):
        return False
    expected = _PAUSE_PREFIX + marker['token'] + '\n'
    try:
        with target.open('r', encoding='utf-8') as pause:
            if _file_signature(os.fstat(pause.fileno())) != marker['file_signature']:
                return False
            if pause.read(len(expected) + 1) != expected:
                return False
            return _file_signature(target.stat()) == marker['file_signature']
    except (OSError, UnicodeError):
        return False


def _owned_marker(target, db):
    marker = db.get_state('auto_pause') or {}
    if not isinstance(marker, dict):
        return None
    at = marker.get('at')
    if isinstance(at, bool) or not isinstance(at, (int, float)) or not math.isfinite(at):
        return None
    return marker if _matches_pause(target, marker) else None


def _pause_for_failure(target, db, now, stage):
    target.parent.mkdir(parents=True, exist_ok=True)
    marker = {'at': now, 'stage': stage, 'token': uuid.uuid4().hex}
    try:
        # Never overwrite or take ownership of an existing human/debug pause.
        with target.open('x', encoding='utf-8', newline='\n') as pause:
            pause.write(_PAUSE_PREFIX + marker['token'] + '\n')
            pause.flush()
        # Windows finalizes the last-write timestamp when the writer closes.
        marker['file_signature'] = _file_signature(target.stat())
    except FileExistsError:
        # More failures must not extend an existing automatic cooldown.
        return _owned_marker(target, db) is not None
    try:
        db.set_state('auto_pause', marker)
    except Exception:
        # A pause without persisted ownership cannot ever heal by itself.
        if _matches_pause(target, marker):
            target.unlink(missing_ok=True)
        raise
    return True


def failed(config, db, stage):
    with _STATE_LOCK:
        previous = db.get_state('browser_failures') or {}
        now = time.time()
        count = previous.get('consecutive', 0) + 1 if now - previous.get('last_at', 0) < 600 else 1
        stage = stage if stage in {'load', 'setup', 'reply', 'complete', 'queue'} else 'unknown'
        state = {'consecutive': count, 'last_at': now, 'stage': stage}
        paused = count >= 3
        isolated = paused and config.openai.provider == 'hybrid_web'
        if isolated:
            state.update({'blocked_until': now + 60, 'lane': 'chatgpt'})
        db.set_state('browser_failures', state)
        if isolated:
            detail = 'GPT 通道连续失败，暂缓该通道；其他模型继续'
        elif paused:
            automatic = _pause_for_failure(config.resolve(config.paths.pause_file), db, now, stage)
            detail = (f'连续三次失败，已暂停，{int(AUTO_RESUME_COOLDOWN_SECONDS // 60)} 分钟后自动恢复'
                      if automatic else '连续三次失败；保留已有的人工或调试暂停')
        else:
            detail = '本条留待审核，其他联系人继续处理'
        db.add_event('llm_error', f'网页阶段 {stage} 失败；{detail}；未切回 Codex')
        return paused


def succeeded(db):
    with _STATE_LOCK:
        db.set_state('browser_failures', {'consecutive': 0, 'last_at': time.time()})
        # A late success can arrive while paused. Keep ownership until the
        # cooldown removes the corresponding file, or it would stay forever.


def auto_resume(config, db, cooldown=AUTO_RESUME_COOLDOWN_SECONDS):
    """Resume an unchanged, owned failure pause after its cooldown."""
    with _STATE_LOCK:
        target = config.resolve(config.paths.pause_file)
        if not target.exists():
            if db.get_state('auto_pause'):
                db.set_state('auto_pause', {})
            return False
        marker = _owned_marker(target, db)
        if marker is None:
            return False
        now = time.time()
        if now - marker['at'] < cooldown:
            return False
        try:
            target.unlink()
        except OSError:
            return False
        db.set_state('auto_pause', {})
        db.set_state('browser_failures', {'consecutive': 0, 'last_at': now})
        db.add_event('auto_resume', f'网页连续失败导致的暂停已满 {int(cooldown // 60)} 分钟，已自动恢复处理；未切回 Codex')
        return True


def pending_resume_seconds(config, db, cooldown=AUTO_RESUME_COOLDOWN_SECONDS):
    """Seconds until an unchanged automatic pause resumes; None otherwise."""
    with _STATE_LOCK:
        marker = _owned_marker(config.resolve(config.paths.pause_file), db)
        if marker is None:
            return None
        return max(0.0, cooldown - (time.time() - marker['at']))
