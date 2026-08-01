import asyncio
import unittest
from pathlib import Path
from uuid import uuid4

import app


class AppDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.original_db_path = app.DB_PATH
        self.db_path = Path(__file__).parent / f".test-telescope-{uuid4().hex}.db"
        app.DB_PATH = self.db_path
        app.initialize_db()

    def tearDown(self):
        app.DB_PATH = self.original_db_path
        self.db_path.unlink(missing_ok=True)

    def test_legacy_demo_cleanup_only_removes_known_samples(self):
        with app.database() as db:
            db.execute("INSERT INTO channels(id,title,username,kind) VALUES(1001,'产品观察','productnotes','channel')")
            db.execute("INSERT INTO channels(id,title,username,kind) VALUES(1004,'真实频道','realchannel','channel')")
        app.remove_legacy_demo_data()
        with app.database() as db:
            channel_ids = [row[0] for row in db.execute("SELECT id FROM channels ORDER BY id")]
        self.assertEqual(channel_ids, [1004])

    def test_background_sync_uses_selected_scanned_channels(self):
        with app.database() as db:
            db.execute("INSERT INTO scanned_dialogs(id,title,username,kind,scanned_at) VALUES(2001,'频道 A','channel_a','channel',?)", (app.utc_iso(),))
        original_sync = app.telegram.sync

        async def fake_sync(dialogs, limit, job):
            self.assertEqual([item["id"] for item in dialogs], [2001])
            self.assertEqual(limit, 50)
            job.results.append({"channel_id": 2001, "title": "频道 A", "state": "completed", "processed": 3, "stored_total": 3, "error": None})
            job.channels_completed = 1
            job.messages_processed = 3

        app.telegram.sync = fake_sync
        try:
            job = app.SyncJob(job_id="job", channel_ids=[2001], limit_per_channel=50)
            asyncio.run(app.run_sync_job(job))
        finally:
            app.telegram.sync = original_sync
        self.assertEqual(job.state, "completed")
        self.assertEqual(app.snapshot(job)["messages_processed"], 3)

    def test_telegram_links_cover_public_and_private_channels(self):
        self.assertEqual(app.telegram_permalink("public_channel", 123, 45), "https://t.me/public_channel/45")
        self.assertEqual(app.telegram_permalink(None, 123, 45), "https://t.me/c/123/45")

    def test_delete_channel_index_cascades_and_preserves_other_data(self):
        with app.database() as db:
            db.execute("INSERT INTO channels(id,title,kind,message_count) VALUES(3001,'删除目标','channel',1)")
            db.execute("INSERT INTO channels(id,title,kind,message_count) VALUES(3002,'保留频道','channel',1)")
            db.execute("INSERT INTO messages(channel_id,telegram_id,sent_at,text,media_type,tags) VALUES(3001,1,?,'delete_token','text','')", (app.utc_iso(),))
            deleted_message_id = db.execute("SELECT id FROM messages WHERE channel_id=3001").fetchone()[0]
            db.execute("INSERT INTO messages(channel_id,telegram_id,sent_at,text,media_type,tags) VALUES(3002,1,?,'keep_token','text','')", (app.utc_iso(),))
            db.execute("INSERT INTO bookmarks(message_id,created_at) VALUES(?,?)", (deleted_message_id, app.utc_iso()))
            db.execute("INSERT INTO scanned_dialogs(id,title,username,kind,scanned_at) VALUES(3001,'删除目标',NULL,'channel',?)", (app.utc_iso(),))
        result = app.delete_channel_index(3001)
        self.assertEqual(result, {"deleted": True, "channel_id": 3001, "title": "删除目标", "messages_deleted": 1, "bookmarks_deleted": 1})
        with app.database() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM channels WHERE id=3001").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM messages WHERE channel_id=3001").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM bookmarks").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM message_search WHERE message_search MATCH 'delete_token'").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM message_search WHERE message_search MATCH 'keep_token'").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM channels WHERE id=3002").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM scanned_dialogs WHERE id=3001").fetchone()[0], 1)

    def test_delete_missing_channel_returns_404(self):
        with self.assertRaises(app.HTTPException) as error:
            app.delete_channel_index(9999)
        self.assertEqual(error.exception.status_code, 404)

    def test_delete_endpoint_blocks_while_sync_is_running(self):
        with app.database() as db:
            db.execute("INSERT INTO channels(id,title,kind,message_count) VALUES(3001,'删除目标','channel',1)")
            db.execute("INSERT INTO messages(channel_id,telegram_id,sent_at,text,media_type,tags) VALUES(3001,1,?,'preserve_token','text','')", (app.utc_iso(),))

        class RunningTask:
            def done(self):
                return False

        original_task = app.sync_task
        app.sync_task = RunningTask()
        try:
            with self.assertRaises(app.HTTPException) as error:
                asyncio.run(app.delete_channel(3001))
        finally:
            app.sync_task = original_task
        self.assertEqual(error.exception.status_code, 409)
        with app.database() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM messages WHERE channel_id=3001").fetchone()[0], 1)

    def test_search_returns_one_card_per_telegram_album(self):
        with app.database() as db:
            db.execute("INSERT INTO channels(id,title,kind,message_count) VALUES(4001,'相册频道','channel',4)")
            for telegram_id, text in ((1, ''), (2, ''), (3, 'album_caption_token')):
                db.execute("""INSERT INTO messages(channel_id,telegram_id,sent_at,text,media_type,tags,grouped_id,album_text)
                              VALUES(4001,?,?,?,'image','', 'album-1', 'album_caption_token')""", (telegram_id, app.utc_iso(), text))
            db.execute("INSERT INTO messages(channel_id,telegram_id,sent_at,text,media_type,tags) VALUES(4001,4,?,'standalone_token','text','')", (app.utc_iso(),))
        result = app.search(app.SearchIn(limit=10))
        self.assertEqual(result["total"], 2)
        self.assertEqual({item["telegram_id"] for item in result["items"]}, {3, 4})
        caption_search = app.search(app.SearchIn(query="album_caption_token", limit=10))
        self.assertEqual(caption_search["total"], 1)
        self.assertEqual(caption_search["items"][0]["telegram_id"], 3)


if __name__ == "__main__":
    unittest.main()
