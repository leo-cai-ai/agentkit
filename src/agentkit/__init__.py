"""agentkit：通用企业级 LLM Agent 框架。"""

import warnings

from langchain_core._api.deprecation import LangChainPendingDeprecationWarning

# LangGraph 0.2/0.3 在导入 Checkpoint 模块时会由其内部全局 Reviver 触发该告警，
# 应用层无法向这个内部构造函数传参；只屏蔽这一条已知告警，其他弃用告警继续可见。
warnings.filterwarnings(
    "ignore",
    message=r"The default value of `allowed_objects` will change in a future version.*",
    category=LangChainPendingDeprecationWarning,
)
