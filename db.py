import os, sqlite3, time


class DB:
    def __init__(self, path=None):
        path = path or os.getenv("DB_PATH", "data.db")
        self.c = sqlite3.connect(path, check_same_thread=False)
        self.c.row_factory = sqlite3.Row
        self.c.executescript("""
        CREATE TABLE IF NOT EXISTS streamers(
            slug TEXT PRIMARY KEY, region TEXT, channel_id INTEGER, role_id INTEGER);
        CREATE TABLE IF NOT EXISTS clips(
            id TEXT PRIMARY KEY, slug TEXT, path TEXT, message_id INTEGER, created REAL);
        CREATE TABLE IF NOT EXISTS trials(
            user_id INTEGER PRIMARY KEY, slug TEXT, expires REAL, done INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS paid(user_id INTEGER PRIMARY KEY, expires REAL);
        CREATE TABLE IF NOT EXISTS cfg(key TEXT PRIMARY KEY, value TEXT);
        """)
        self.c.commit()

    def q(self, sql, *a):
        cur = self.c.execute(sql, a)
        self.c.commit()
        return cur

    # settings (channel ids the bot created for itself)
    def get_cfg(self, key):
        r = self.q("SELECT value FROM cfg WHERE key=?", key).fetchone()
        return r["value"] if r else None

    def set_cfg(self, key, value):
        self.q("INSERT OR REPLACE INTO cfg VALUES(?,?)", key, value)

    # streamers
    def add_streamer(self, slug, region, channel_id, role_id):
        self.q("INSERT OR REPLACE INTO streamers VALUES(?,?,?,?)", slug, region, channel_id, role_id)

    def get_streamer(self, slug):
        return self.q("SELECT * FROM streamers WHERE slug=?", slug).fetchone()

    def list_streamers(self):
        return self.q("SELECT * FROM streamers").fetchall()

    def remove_streamer(self, slug):
        self.q("DELETE FROM streamers WHERE slug=?", slug)

    # clips
    def add_clip(self, clip_id, slug, path, message_id):
        self.q("INSERT OR REPLACE INTO clips VALUES(?,?,?,?,?)", clip_id, slug, path, message_id, time.time())

    def clip_by_message(self, message_id):
        return self.q("SELECT * FROM clips WHERE message_id=?", message_id).fetchone()

    def old_clips(self, before):
        return self.q("SELECT * FROM clips WHERE created<?", before).fetchall()

    def delete_clip(self, clip_id):
        self.q("DELETE FROM clips WHERE id=?", clip_id)

    # trials: one per user, ever
    def has_used_trial(self, user_id):
        return self.q("SELECT 1 FROM trials WHERE user_id=?", user_id).fetchone() is not None

    def start_trial(self, user_id, slug, expires):
        self.q("INSERT INTO trials(user_id,slug,expires) VALUES(?,?,?)", user_id, slug, expires)

    def expired_trials(self, now):
        return self.q("SELECT * FROM trials WHERE done=0 AND expires<?", now).fetchall()

    def active_trial(self, user_id):
        return self.q("SELECT 1 FROM trials WHERE user_id=? AND done=0 AND expires>?",
                      user_id, time.time()).fetchone() is not None

    def finish_trial(self, user_id):
        self.q("UPDATE trials SET done=1 WHERE user_id=?", user_id)

    # paid
    def grant_paid(self, user_id, expires):
        self.q("INSERT OR REPLACE INTO paid VALUES(?,?)", user_id, expires)

    def revoke_paid(self, user_id):
        self.q("DELETE FROM paid WHERE user_id=?", user_id)

    def expired_paid(self, now):
        return self.q("SELECT * FROM paid WHERE expires<?", now).fetchall()
