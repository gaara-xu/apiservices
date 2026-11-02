"""真实服务验收：保留 deployment-check 模块记录作为部署证据。"""
import os
import httpx


def main():
    """验证健康、业务配置、任务状态、服务隔离和审计完整性。"""
    base = os.getenv('API_BASE', 'http://192.168.3.110:8110')
    prefix = '/api/v1/services/deployment-check'
    with httpx.Client(base_url=base, timeout=15, trust_env=False) as client:
        assert client.get('/health/ready').status_code == 200
        assert client.get('/openapi.json').status_code == 200
        response = client.put(prefix+'/settings/validation', json={'value': {'version': '0.5.1', 'purpose': 'deployment verification'}})
        assert response.status_code == 200, response.text
        assert client.get(prefix+'/settings/validation').json()['value']['version'] == '0.5.1'
        response = client.post(prefix+'/tasks', json={'kind':'self-check','payload':{'no_ai_execution':True}})
        assert response.status_code == 201, response.text
        task_id = response.json()['id']
        assert client.get('/api/v1/services/another-service/tasks/'+task_id).status_code == 404
        for status in ('running','succeeded'):
            response=client.patch(prefix+'/tasks/'+task_id,json={'status':status,'result':{'verified':True} if status=='succeeded' else None})
            assert response.status_code == 200, response.text
        assert client.patch(prefix+'/tasks/'+task_id,json={'status':'running'}).status_code == 409
        task=client.get(prefix+'/tasks/'+task_id).json()
        assert task['result']['verified'] is True
        events=client.get(prefix+'/audit').json()
        assert len([row for row in events if row['entity_id']==task_id]) == 3
        print('验收通过：健康、配置读写、任务状态、服务隔离、结果及审计。任务编号：'+task_id)


if __name__=='__main__':
    main()
