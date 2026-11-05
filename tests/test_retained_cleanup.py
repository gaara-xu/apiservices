"""隔离验证清理前特征持久化及无匹配特征时保留原图。"""
import hashlib
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock, patch
from app.image_preference import cleanup


class CleanupTests(unittest.TestCase):
    def test_archive_before_delete_and_skip_untrained(self):
        """已归档特征先提交再删图，未训练样本不删除，失败不清空整批。"""
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[2]/'runtime') as directory:
            root=Path(directory);path=root/'test.jpg';path.write_bytes(b'fixture');stat=path.stat()
            row={'folder':'notlike','root_path':str(root),'id':'fixture','relative_path':'test.jpg','state':'notlike','size_bytes':stat.st_size,'mtime_ns':stat.st_mtime_ns}
            feature={'label':'notlike','embedding':[0.0]*1152,'content_sha256':hashlib.sha256(b'fixture').hexdigest()}
            cur=Mock();cur.fetchall.return_value=[row];cur.fetchone.side_effect=[row,{'feature':feature},row]
            conn=Mock();conn.cursor.return_value.__enter__=Mock(return_value=cur);conn.cursor.return_value.__exit__=Mock(return_value=False)
            control=Mock();control.fetchone.return_value=None
            @contextmanager
            def control_context():
                # 隔离任务锁与数据库，不影响线上记录。
                yield Mock(),control
            @contextmanager
            def image_context():
                # 为清理提供可核验提交顺序的模拟事务。
                yield conn
            with patch('app.image_preference.ai.control_lock',control_context),patch('app.image_preference.ai.config',return_value={'mac_url':'http://test'}),patch.object(cleanup.service,'locked_connection',image_context),patch.object(cleanup.service,'folder_config',return_value={'notlike':root}),patch.object(cleanup,'audit'),patch.object(cleanup,'connect',image_context):
                conn.commit.side_effect=lambda: self.assertTrue(path.exists()) if conn.commit.call_count==1 else None
                result=cleanup.delete_image({'image_id':'fixture','root':str(root)});self.assertEqual(result['status'],'deleted');self.assertFalse(path.exists())
                path.write_bytes(b'fixture');row['mtime_ns']=path.stat().st_mtime_ns
                cur.fetchone.side_effect=[row,None,None];conn.commit.side_effect=None
                result=cleanup.delete_image({'image_id':'fixture','root':str(root)});self.assertEqual(result['status'],'skipped');self.assertTrue(path.exists())
