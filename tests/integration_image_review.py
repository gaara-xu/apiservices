"""使用真实MySQL及项目临时图片验证完整人工分类流程。"""
import json
import tempfile
import uuid
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from PIL import Image
from app.main import app
from app.db import connect, audit
from app.image_preference import service


def main():
    """用隔离目录测试扫描、原图、预览、两种选择、幂等、重名和中断恢复。"""
    runtime=Path(__file__).resolve().parents[1]/'runtime'
    runtime.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=runtime) as temporary:
        roots={key:Path(temporary)/key for key in service.STATES}
        for root in roots.values(): root.mkdir()
        for name,color in [('a.jpg','red'),('b.jpg','blue'),('c.jpg','green')]:
            Image.new('RGB',(48,32),color).save(roots['all']/name)
        Image.new('RGB',(48,32),'white').save(roots['good']/'reference.jpg')
        Image.new('RGB',(48,32),'black').save(roots['like']/'a.jpg')
        reference=(roots['good']/'reference.jpg').read_bytes()
        collision=(roots['like']/'a.jpg').read_bytes()
        try:
            with patch.object(service,'folder_config',return_value=roots), TestClient(app) as client:
                prefix='/api/v1/image-preference'
                response=client.post(prefix+'/scan');assert response.status_code==200,response.text
                assert client.get(prefix+'/stats').json()['pending']==3
                with connect() as conn,conn.cursor() as cur:
                    cur.execute("SELECT id,relative_path FROM image_items WHERE root_path=%s AND folder='all'",(str(roots['all']),))
                    ids={row['relative_path']:row['id'] for row in cur.fetchall()}
                for endpoint in ('content','preview'):
                    response=client.get(prefix+'/images/'+ids['a.jpg']+'/'+endpoint)
                    assert response.status_code==200 and response.content
                response=client.post(prefix+'/images/'+ids['a.jpg']+'/decision',json={'decision':'like'})
                assert response.status_code==200,response.text
                assert not (roots['all']/'a.jpg').exists()
                assert (roots['like']/'a.jpg').read_bytes()==collision
                assert len(list(roots['like'].glob('a__*.jpg')))==1
                response=client.post(prefix+'/images/'+ids['a.jpg']+'/decision',json={'decision':'like'})
                assert response.json()['already_applied'] is True
                assert client.post(prefix+'/images/'+ids['a.jpg']+'/decision',json={'decision':'notlike'}).status_code==409
                assert client.post(prefix+'/images/'+ids['b.jpg']+'/decision',json={'decision':'notlike'}).status_code==200
                assert (roots['notlike']/'b.jpg').exists()
                assert client.post(prefix+'/images/'+ids['c.jpg']+'/decision',json={'decision':'invalid'}).status_code==422
                with patch.object(service,'finalize_move',side_effect=RuntimeError('simulate interrupted database finalization')):
                    try: service.decide(ids['c.jpg'],'like')
                    except RuntimeError: pass
                    else: raise AssertionError('fault injection did not run')
                assert client.get(prefix+'/next').json()['image'] is None
                assert (roots['good']/'reference.jpg').read_bytes()==reference
                with connect() as conn,conn.cursor() as cur:
                    cur.execute('SELECT state FROM image_moves WHERE image_id IN (%s,%s,%s)',tuple(ids.values()))
                    assert all(row['state']=='completed' for row in cur.fetchall())
                print('真实MySQL验收通过：扫描、图片传输、喜欢/不喜欢移动、重名保护、重复提交、状态冲突、中断恢复；good目录未改动。')
        finally:
            with connect() as conn,conn.cursor() as cur:
                cur.execute('DELETE image_moves FROM image_moves JOIN image_items ON image_moves.image_id=image_items.id WHERE image_items.root_path IN (%s,%s,%s,%s)',tuple(str(path) for path in roots.values()))
                cur.execute('DELETE FROM image_items WHERE root_path IN (%s,%s,%s,%s)',tuple(str(path) for path in roots.values()))
                audit(cur,'image-preference','verification',str(uuid.uuid4()),'cleanup',None,{'temporary_roots':[str(path) for path in roots.values()]})
                conn.commit()


if __name__=='__main__': main()
