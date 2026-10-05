class RolloutError(RuntimeError):
    """An unusable rollout, optionally carrying evidence collected before failure."""
    def __init__(self, message, result=None):
        super().__init__(message)
        self.result = result
