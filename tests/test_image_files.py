"""图片迁移与碰撞保护的本地文件系统测试。"""
import os
from pathlib import Path
import tempfile
import unittest
from app.image_preference.files import choose_destination, perform_move, safe_path

ROOT=Path(__file__).resolve().parents[1]


class FileTests(unittest.TestCase):
    def setUp(self):
        """创建仅位于项目内的独立测试目录。"""
        self.tmp=tempfile.TemporaryDirectory(dir=ROOT/'runtime')
        self.root=Path(self.tmp.name)
        self.source=self.root/'source.jpg'
        self.source.write_bytes(b'fixture')
        self.target=self.root/'dest.jpg'
        stat=self.source.stat()
        self.operation={'source_path':str(self.source),'destination_path':str(self.target),'source_device':stat.st_dev,'source_inode':stat.st_ino,'source_size':stat.st_size,'source_mtime_ns':stat.st_mtime_ns}

    def tearDown(self):
        """清理测试生成的文件，不涉及真实图片。"""
        self.tmp.cleanup()

    def test_move_and_retry(self):
        """移动保持内容并删除源路径，重复恢复不影响结果。"""
        perform_move(self.operation)
        perform_move(self.operation)
        self.assertFalse(self.source.exists())
        self.assertEqual(self.target.read_bytes(),b'fixture')

    def test_recover_after_link(self):
        """硬链接已创建但源未删除的中断可恢复。"""
        os.link(self.source,self.target)
        perform_move(self.operation)
        self.assertFalse(self.source.exists())
        self.assertTrue(self.target.exists())

    def test_collision_and_changed_source(self):
        """同名不同文件或源内容变化时拒绝操作，保持文件不丢失。"""
        self.target.write_bytes(b'existing')
        with self.assertRaises(FileExistsError): perform_move(self.operation)
        self.assertEqual(self.source.read_bytes(),b'fixture')
        self.assertEqual(self.target.read_bytes(),b'existing')
        self.target.unlink()
        self.source.write_bytes(b'changed source')
        with self.assertRaises(ValueError): perform_move(self.operation)
        self.assertTrue(self.source.exists())

    def test_choose_and_path(self):
        """目标同名追加标识，路径越界和软链接被拒绝。"""
        self.target.write_bytes(b'existing')
        result=choose_destination(self.root,'dest.jpg','id123')
        self.assertEqual(result.name,'dest__id123.jpg')
        with self.assertRaises(ValueError): safe_path(self.root,'../other.jpg')
        (self.root/'alias.jpg').symlink_to(self.source)
        with self.assertRaises(ValueError): safe_path(self.root,'alias.jpg')


if __name__=='__main__': unittest.main()
