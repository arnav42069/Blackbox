import shutil
from pathlib import Path
from .db import now


def prune(settings, db):
    """Retention is applied on startup/settings changes; never delete an active session."""
    cutoff = now() - settings.prefs.retention_days * 86400
    for row in db.rows("SELECT id FROM sessions WHERE updated_at<? AND status!='running'", (cutoff,)):
        sid = row['id']
        for attachment in db.rows('SELECT path FROM attachments WHERE session_id=?', (sid,)):
            Path(attachment['path']).unlink(missing_ok=True)
        shutil.rmtree(settings.data_dir/'artifacts'/sid, ignore_errors=True)
        db.execute('DELETE FROM sessions WHERE id=?', (sid,))
    # Remove interrupted/failed-upload orphans, preserving only committed attachments.
    referenced = {r['path'] for r in db.rows('SELECT path FROM attachments')}
    for path in (settings.data_dir/'attachments').iterdir():
        if path.is_file() and str(path) not in referenced:
            path.unlink()
