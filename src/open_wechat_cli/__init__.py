"""open-wechat-cli：微信 / 企业微信本地历史的只读读取层。

当前覆盖 Windows 企业微信（WXWork 5.x）的本地库：
内存取 key → 一致性快照 → 页面解密 → 只读查询与导出。
"""

__version__ = "0.1.0a1"

__all__ = ["__version__"]
