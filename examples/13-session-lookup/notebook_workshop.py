"""Notebook 只委托服务生命周期；检索、展开和配对写在单元格中。"""
from demo import LiveRun


class NotebookWorkshop(LiveRun):
    def __init__(self, shell=None):
        super().__init__()
        self.shell = shell
        if shell is not None:
            shell.events.register('post_run_cell', self.after_cell)

    def after_cell(self, result):
        if result.error_before_exec is not None or result.error_in_exec is not None:
            self._at_exit()

    def close(self):
        super().close()
        if self.shell is not None:
            self.shell.events.unregister('post_run_cell', self.after_cell)
            self.shell = None
