"""防止同一单机工作区被重复启动。锁随进程退出由系统释放。"""
from pathlib import Path
import hashlib,os


class WorkspaceLock:
    def __init__(self,root):self.root=Path(root).resolve();self.handle=None;self.file=None
    def acquire(self):
        if self.handle or self.file:return True
        if os.name=='nt':
            import ctypes
            kernel=ctypes.WinDLL('kernel32',use_last_error=True);kernel.CreateMutexW.argtypes=[ctypes.c_void_p,ctypes.c_bool,ctypes.c_wchar_p];kernel.CreateMutexW.restype=ctypes.c_void_p
            ctypes.set_last_error(0);self.kernel=kernel;self.handle=kernel.CreateMutexW(None,False,'Local\\FourFlowAnalysis_'+hashlib.sha256(str(self.root).casefold().encode()).hexdigest())
            if not self.handle:raise OSError(ctypes.get_last_error(),'工作区锁创建失败')
            if ctypes.get_last_error()==183:self.release();return False
            return True
        import fcntl
        (self.root/'runtime').mkdir(parents=True,exist_ok=True);self.file=(self.root/'runtime/instance.lock').open('a+')
        try:fcntl.flock(self.file.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB);return True
        except BlockingIOError:self.file.close();self.file=None;return False
    def release(self):
        if self.handle:
            import ctypes
            self.kernel.CloseHandle.argtypes=[ctypes.c_void_p];self.kernel.CloseHandle(self.handle);self.handle=None
        if self.file:self.file.close();self.file=None
