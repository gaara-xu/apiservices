"""验证大图适配不会改变原图，保留方向且输出适合模型输入。"""
import io
import unittest
import hashlib
from PIL import Image
from mac_ai.images import read_model_image


class ImageTests(unittest.TestCase):
    def test_large_jpeg(self):
        """超过4000万像素的JPEG能够降采样，原始字节内容不变。"""
        buffer=io.BytesIO()
        with Image.new('RGB',(8000,6000),'red') as original:
            original.save(buffer,format='JPEG')
        before=hashlib.sha256(buffer.getvalue()).hexdigest()
        result=read_model_image(buffer)
        self.assertLessEqual(max(result.size),2048)
        self.assertAlmostEqual(result.width/result.height,4/3,places=2)
        self.assertEqual(result.mode,'RGB')
        self.assertEqual(hashlib.sha256(buffer.getvalue()).hexdigest(),before)

    def test_png_orientation(self):
        """非JPEG格式也会适配，EXIF旋转在缩略后仍保持正确。"""
        buffer=io.BytesIO()
        with Image.new('RGB',(3000,1000),'blue') as original:
            exif=Image.Exif();exif[274]=6
            original.save(buffer,format='PNG',exif=exif)
        result=read_model_image(buffer,resize_threshold=100)
        self.assertGreater(result.height,result.width)
        self.assertLessEqual(max(result.size),2048)


if __name__=='__main__':unittest.main()
