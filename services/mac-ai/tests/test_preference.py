"""隔离模型目录验证分类器、幂等样本和失败时旧模型保留。"""
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock
from mac_ai.preference import Preference
from mac_ai.settings import SERVICE_DIR


class PreferenceTests(unittest.TestCase):
    def test_training_and_pinned_model(self):
        """两类特征完整学习后可区分样本，停止的候选训练不改变正式模型。"""
        with tempfile.TemporaryDirectory(dir=SERVICE_DIR/'runtime') as temporary:
            engine = Mock()
            preference = Preference(engine, temporary, 'http://test')
            preference.embedding = Mock(side_effect=lambda content: [1.,0.] if content == b'like' else [0.,1.])
            run = str(uuid.uuid4())
            self.assertFalse(preference.status()['trained'])
            job = str(uuid.uuid4())
            preference.sample(run,job,'like',b'like')
            preference.sample(run,job,'like',b'like')
            self.assertEqual(preference.embedding.call_count,1)
            with self.assertRaises(ValueError): preference.finalize(run,2)
            with self.assertRaises(ValueError): preference.sample(run,job,'notlike',b'bad')
            preference.sample(run,str(uuid.uuid4()),'notlike',b'notlike')
            model = preference.finalize(run,2)
            self.assertEqual(model['samples'],2)
            self.assertEqual(preference.predict(run,b'like')['label'],'like')
            self.assertEqual(preference.predict(run,b'notlike')['label'],'notlike')
            self.assertEqual(preference.finalize(run,2),model)
            stopped = str(uuid.uuid4())
            preference.sample(stopped,str(uuid.uuid4()),'like',b'like')
            with self.assertRaises(ValueError): preference.finalize(stopped,1)
            self.assertEqual(preference.status()['model_version'],run)
            with self.assertRaises(ValueError): preference.identifier('../outside')


if __name__ == '__main__': unittest.main()
