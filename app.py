"""四流关联分析系统的桌面入口。"""
import ctypes
import logging
from pathlib import Path

ROOT=Path(__file__).resolve().parent


def main(workspace=None):
    root=Path(workspace or ROOT).resolve()
    (root/'runtime').mkdir(parents=True,exist_ok=True)
    (root/'exports').mkdir(exist_ok=True)
    logging.basicConfig(filename=root/'runtime/application.log',level=logging.INFO,encoding='utf-8',format='%(asctime)s %(levelname)s %(message)s')
    try:ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError,OSError):pass
    from fourflow.workspace_lock import WorkspaceLock
    lock=WorkspaceLock(root)
    if not lock.acquire():
        from tkinter import messagebox
        messagebox.showinfo('工作区已打开','这个工作区已在运行，请返回现有窗口。');return
    from fourflow.ui import Application
    app=Application(root)
    if root!=ROOT:app.title('反电诈四流辅助侦查系统 — '+root.name)
    def handle_exception(exc_type,value,traceback):
        logging.error("UI exception",exc_info=(exc_type,value,traceback))
        from tkinter import messagebox
        messagebox.showerror("操作未完成",str(value))
    app.report_callback_exception=handle_exception
    try:app.mainloop()
    finally:lock.release()


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(description='四流单机辅助研判')
    parser.add_argument('--workspace',type=Path,help='另一个独立工作区，默认使用项目目录')
    main(parser.parse_args().workspace)
