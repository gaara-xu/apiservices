# Mac AI 图像识别接口

服务版本：0.1.0。默认监听Mac的8111端口；与110业务API的8110、Ollama的11434独立。

## 运行与文件

双击 `安装环境.command` 按requirements.lock安装已验证的环境（首次需要下载Python依赖）；以后双击 `启动.command`、`关闭.command`、`重启.command`，可用 `状态.command` 查询状态。命令也可从终端运行。启动脚本将进程置于后台，模型就绪后退出；关闭终端不影响服务。

`config.json` 设置host、port、模型路径、允许读取图片的目录、device（auto/mps/cpu）、批量上限和启动等待时间。修改后重启。默认auto优先MPS。只运行一个Uvicorn worker，模型只加载一份，请求串行计算。

模型位于 `/Users/gaara/hades/AILocal/ollama-models/siglip2-so400m-patch14-384`。服务代码在 `apiservices/services/mac-ai`。依赖环境 `.venv`、缓存及临时文件 `runtime`、日志 `runtime/service.log` 均位于服务目录。业务输入和输出不在Mac建库，返回给调用方保存至110的apiserver数据库。本服务没有图片上传、图片下载或目录扫描接口。

## 访问方式

本机：`http://127.0.0.1:8111`。110调用时将127.0.0.1替换为Mac实际内网IP；HTTP客户端应设置 `trust_env=False`，不能经VPN代理。共享目录须事先在Mac可访问，并配置为允许的目录；当前默认允许AILocal路径，其他位置在用户授权后再配置。

请求里的路径必须是Mac可读的绝对路径，不是110容器内路径。接口不传输图片文件，只传路径、标签和数值结果。

### GET /health/live

返回 `service`、`version`、`pid`、`instance`。只表示进程在线。

### GET /health/ready

模型就绪返回200：`{"status":"ready","device":"mps"}`。加载中或失败返回503，detail中包含state和error。

### GET /api/v1/model

返回模型名称、loading/ready/error状态、设备、错误、批量上限、标签上限和 `personal_preference_trained:false`。

### POST /api/v1/images/embeddings

请求：

```json
{"paths":["/Users/gaara/hades/AILocal/共享图片/example.jpg"]}
```

响应示意（实际embedding包含完整1152维向量）：

```json
{"dimension":1152,"normalized":true,"items":[{"path":"/Users/gaara/hades/AILocal/共享图片/example.jpg","embedding":[0.01,-0.02]}]}
```

图片按EXIF方向修正、转RGB，使用模型配套预处理。每张图片返回一条L2归一化特征。调用方应同时记录模型版本和特征处理规则，不能把不同模型产生的向量混用。

### POST /api/v1/images/classify

请求：

```json
{"paths":["/Users/gaara/hades/AILocal/共享图片/example.jpg"],"labels":["a photo of a cat","a photo of a dog"]}
```

响应示意：

```json
{"score_type":"sigmoid_similarity","items":[{"path":"/Users/gaara/hades/AILocal/共享图片/example.jpg","predictions":[{"label":"a photo of a cat","score":0.8},{"label":"a photo of a dog","score":0.1}]}]}
```

候选标签2至32项，非空且唯一，每项最多200字符；模型文本长度还有token限制，超长文本会截断。每张图片的标签按匹配分数降序返回。这是候选文本与图片的匹配分数，彼此独立、不保证和为1，也不是经过校准的真实概率。模型支持的语言和具体准确率应使用实际样本评估。

这不是自由问答、OCR或自动生成图片描述接口；也尚未训练个人喜好，不能通过简单输入“喜欢/不喜欢”代替个人标注训练。

### GET /openapi.json

由服务生成完整OpenAPI规范，可直接供后续开发使用。没有依赖外部CDN的文档页面。

## 错误和调用限制

- 422：路径不存在、未挂载、路径超出允许目录、图片损坏/过大、批量或标签参数无效。
- 429：模型正在计算，Retry-After为2秒，调用方稍后重试。
- 503：模型未就绪或运行时计算失败；查询model接口和日志定位。
- 默认单次1至4张图片，单图不超过4000万像素。建议客户端先从单张请求开始，超时设为120秒，首次MPS计算可能包含初始化耗时。

## Python调用示例

```python
import httpx

with httpx.Client(base_url="http://Mac内网IP:8111", trust_env=False, timeout=120) as client:
    response = client.post("/api/v1/images/classify", json={
        "paths": ["/Users/gaara/hades/AILocal/共享图片/example.jpg"],
        "labels": ["a photo of a cat", "a photo of a dog"]
    })
    response.raise_for_status()
    print(response.json())
```

## 生命周期说明

启动脚本使用项目内锁避免重复操作，通过PID和实例令牌确认服务身份。端口被占用会报错，不杀死其他服务。模型加载失败时保留状态接口和日志，修复配置后运行重启。关闭时等待正在执行的请求完成，超时报告错误，不强制杀进程。

仓库内同时提供 `openapi.json` 静态规范，接口变化时必须同步更新；实时规范以运行服务返回为准。
