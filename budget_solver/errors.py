"""领域错误类型。整次修订拒绝等场景携带结构化错误明细。"""


class BudgetError(Exception):
    """所有可预期业务错误的基类。"""


class ValidationError(BudgetError):
    """模型 JSON 非法（结构、有理数、项形状等）。"""

    def __init__(self, message, errors=None):
        super().__init__(message)
        self.errors = errors or []


class ReferenceRejectedError(BudgetError):
    """引用非法或会悬挂，整次修订必须拒绝。"""

    def __init__(self, message, errors=None):
        super().__init__(message)
        self.errors = errors or []


class CompileError(BudgetError):
    """编译期引用核对失败（理论上快照内不应发生，防御性抛出）。"""


class NotFoundError(BudgetError):
    """模型 / 快照 / 编译产物 / 报告不存在。"""


class VerifyError(BudgetError):
    """独立验证发现产物与目录事实不符。"""


class AllocationValidationError(BudgetError):
    """代拨场次/委托/拨款单负载结构或数值非法。"""

    def __init__(self, message, errors=None):
        super().__init__(message)
        self.errors = errors or []


class LedgerNotSettledError(BudgetError):
    """去向册存在不能结清的受困来源，不得进入点名/批签。"""

    def __init__(self, message, errors=None):
        super().__init__(message)
        self.errors = errors or []


class OrderRejectedError(BudgetError):
    """正式拨款单被拒：同号异文、突破证额/底线/余额或对已支余额重复下账。"""

    def __init__(self, message, errors=None):
        super().__init__(message)
        self.errors = errors or []


class AllocationInfeasibleError(BudgetError):
    """获批项目无法同时足额兑现（原资助证额、留款底线或自有款不足）。"""

    def __init__(self, message, errors=None):
        super().__init__(message)
        self.errors = errors or []
