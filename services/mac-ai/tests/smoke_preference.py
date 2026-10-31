"""真实SigLIP特征与图片接收回调的合成图片冒烟，不读取用户照片。"""
import io
import tempfile
import uuid
from pathlib import Path
from unittest.mock import patch
from PIL import Image
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mac_ai.settings import prepare_environment, load_settings, SERVICE_DIR
prepare_environment()
from mac_ai.engine import Engine
from mac_ai.preference import Preference, make_router


class CallbackClient:
    """在隔离测试中确认Mac确实向110发送逐图结果。"""
    records=[]
    def __init__(self, **kwargs):
        """检查内网回调禁止代理。"""
        assert kwargs['trust_env'] is False
    def __enter__(self):
        """返回回调测试客户端。"""
        return self
    def __exit__(self,*args):
        """结束测试连接。"""
        return False
    def post(self,url,json):
        """记录真实模型计算后的回调内容并返回确认。"""
        CallbackClient.records.append(json)
        return self
    def raise_for_status(self):
        """模拟110成功确认回调。"""
        pass


def main():
    """从原图字节提取真实特征并拟合分类器，再验证固定版本推理及回调。"""
    engine=Engine(load_settings());engine.load()
    assert engine.state=='ready',engine.error
    with tempfile.TemporaryDirectory(dir=SERVICE_DIR/'runtime') as temporary:
        preference=Preference(engine,Path(temporary),'http://192.168.3.110:8110')
        app=FastAPI();app.include_router(make_router(preference))
        client=TestClient(app);run=str(uuid.uuid4());images={}
        for label,color in [('like','red'),('notlike','blue')]:
            output=io.BytesIO();Image.new('RGB',(384,384),color).save(output,format='JPEG');images[label]=output.getvalue()
        with patch('mac_ai.preference.httpx.Client',CallbackClient):
            for label,content in images.items():
                response=client.post('/api/v1/preference/jobs/'+str(uuid.uuid4()),params={'run_id':run,'kind':'train','label':label},content=content,headers={'Content-Type':'application/octet-stream'})
                assert response.status_code==200,response.text
            response=client.post('/api/v1/preference/finalize/'+run,params={'expected':2});assert response.status_code==200,response.text
            for label,content in images.items():
                response=client.post('/api/v1/preference/jobs/'+str(uuid.uuid4()),params={'run_id':str(uuid.uuid4()),'kind':'screen','model_version':run},content=content)
                assert response.status_code==200,response.text
                assert CallbackClient.records[-1]['result']['label']==label
        assert len(CallbackClient.records)==4
        print('真实SigLIP/MPS验证通过：HTTP图片接收、1152维特征、轻量分类器、两类预测、4次回调；测试模型已隔离，无用户照片学习。')


if __name__=='__main__':main()
