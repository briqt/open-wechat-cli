"""结构化错误：每个错误都带稳定的 code、可执行的 repair 和退出码。

约定（与项目质量要求一致）：
* code 稳定可判断，不随文案变化；
* repair 必须是"下一步能真的做"的动作，不能是"请检查环境"；
* details 只放可安全输出的信息，不放 key、正文、完整路径里的用户名以外的东西。
"""

from __future__ import annotations

from typing import Any


class OwcError(Exception):
    """所有可预期错误的基类。"""

    code = "ERROR"
    exit_code = 1

    def __init__(
        self,
        message: str,
        *,
        repair: str | None = None,
        details: dict[str, Any] | None = None,
        code: str | None = None,
        exit_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.repair = repair
        self.details = details or {}
        if code is not None:
            self.code = code
        if exit_code is not None:
            self.exit_code = exit_code

    def to_dict(self) -> dict[str, Any]:
        err: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.repair:
            err["repair"] = self.repair
        if self.details:
            err["details"] = self.details
        return err


class UsageError(OwcError):
    """参数不合法。"""

    code = "USAGE"
    exit_code = 2


class PreconditionError(OwcError):
    """前置条件不满足（客户端未运行、账号未登录、平台不支持等）。"""

    code = "PRECONDITION"
    exit_code = 3


class KeyNotFoundError(OwcError):
    """内存中找不到能通过校验的 key。"""

    code = "KEY_NOT_FOUND"
    exit_code = 4


class FormatError(OwcError):
    """落盘格式与当前 codec 假设不符（页大小、page1 结构、WAL 头等）。"""

    code = "FORMAT_MISMATCH"
    exit_code = 5


class SnapshotError(OwcError):
    """快照不一致或读取被拒绝。"""

    code = "SNAPSHOT"
    exit_code = 6


class DataError(OwcError):
    """解密后的库不可用（quick_check 失败、表/列缺失等）。"""

    code = "DATA"
    exit_code = 7


class DependencyError(OwcError):
    """缺少运行时依赖。"""

    code = "DEPENDENCY"
    exit_code = 8
