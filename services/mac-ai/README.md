# Mac AI 图像识别服务

提供SigLIP 2本地特征提取和候选标签分类，默认端口8111，模型仅在Mac运行。

1. 首次运行 `安装环境.command`。
2. 运行 `启动.command`，等待模型就绪。
3. 使用 `关闭.command`、`重启.command`、`状态.command` 管理服务。

接口、参数、错误码、调用示例和共享目录配置见 [API.md](API.md)。本机入口位于“图像识别/MacAI”，对应本仓库services/mac-ai目录。

个人喜好分类器和标注训练尚未实现。模型通过PyTorch直接加载，不依赖Ollama；服务运行强制使用本地权重，禁止在线下载。

`scripts/download_model.py` 是已完成的模型准备工具，日常启动无需运行。当前权重存放在 `/Users/gaara/hades/AILocal/ollama-models/siglip2-so400m-patch14-384`。
