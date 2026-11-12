"""Explicit card selection for pending recordings; tkinter and stdlib only.

All network work runs outside the Tk event loop. No selection is made by default.
This module can be imported on Linux without loading Windows APIs or tkinter.
"""
import queue
import threading
from pathlib import Path
import velcro_win as v

_window_lock = threading.Lock()


def upload_selected(client, pending, item, lead):
    """The single upload entry point: an empty selection can never send a file."""
    if not lead.strip():
        raise v.crm.CRMError('올릴 카드를 먼저 선택하거나 카드 주소를 붙여 넣으세요.')
    result = client.send(item['path'], lead.strip())
    pending.finish(item)
    return result


def show():
    if not _window_lock.acquire(blocking=False):
        return
    try:
        _show()
    except ImportError:
        # Bundled builds include tkinter; source runs may not have it installed.
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, 'CRM 창에는 tkinter가 필요합니다.', 'velcro', 0x40)
    finally:
        _window_lock.release()


def _show():
    import tkinter as tk
    from tkinter import ttk

    root = tk.Tk()
    root.title('velcro · CRM 에 붙이기')
    root.geometry('900x560')
    frame = ttk.Frame(root, padding=16)
    frame.pack(fill='both', expand=True)
    ttk.Label(frame, text='CRM 에 붙이기', font=('', 16, 'bold')).pack(anchor='w')
    ttk.Label(frame, text='카드를 직접 선택한 뒤 올리기를 누르세요. 자동으로 올리지 않습니다.').pack(anchor='w', pady=(4, 12))
    settings = ttk.Frame(frame)
    settings.pack(fill='x')
    ttk.Label(settings, text='CRM 주소').pack(side='left')
    url = tk.StringVar(value=v.get(v.read_settings(), 'crm.url'))
    entry = ttk.Entry(settings, textvariable=url, width=44)
    entry.pack(side='left', padx=8)
    status = tk.StringVar(value='주소 예: https://api.example.com')
    messages = queue.Queue()
    controls, lead_labels = [], {}
    busy = [False]

    def context():
        address = v.crm.server_url(url.get().strip())
        return v.crm.Client(address, state=v.state_dir()), v.crm.Pending(address, state=v.state_dir())

    def work(action, done=lambda result: None):
        if busy[0]:
            return
        busy[0] = True
        for widget in controls:
            widget.configure(state='disabled')
        status.set('처리 중…')
        def run():
            try:
                messages.put((True, action(), done))
            except v.crm.CRMError as e:
                messages.put((False, str(e), done))
            except Exception:
                messages.put((False, 'CRM 작업 실패. 설정과 연결을 확인하세요.', done))
        threading.Thread(target=run, daemon=True).start()

    def drain():
        try:
            ok, result, done = messages.get_nowait()
            if ok == 'notice':
                status.set(result)
                root.after(100, drain)
                return
            busy[0] = False
            for widget in controls:
                widget.configure(state='normal')
            if ok:
                status.set('완료')
                done(result)
            else:
                status.set(result)
        except queue.Empty:
            pass
        root.after(100, drain)

    def save():
        client, _ = context()
        s = v.read_settings()
        s['crm.url'] = [client.url]
        v.write_settings(s)
        url.set(client.url)
        return client

    def connect():
        try:
            client = save()
            work(lambda: client.connect(output=lambda text: messages.put(('notice', text, None))), lambda _: refresh())
        except v.crm.CRMError as e:
            status.set(str(e))
        except OSError:
            status.set('CRM 주소를 저장하지 못했습니다.')

    def disconnect():
        try:
            client, _ = context()
            work(client.disconnect, lambda text: status.set(text))
        except v.crm.CRMError as e:
            status.set(str(e))

    for label, command in [('주소 저장', lambda: safe_save()), ('CRM 연결', connect), ('끊기', disconnect)]:
        button = ttk.Button(settings, text=label, command=command)
        button.pack(side='left', padx=2)
        controls.append(button)
    controls.append(entry)

    canvas = tk.Canvas(frame, highlightthickness=0)
    scrollbar = ttk.Scrollbar(frame, orient='vertical', command=canvas.yview)
    scrollbar.pack(side='right', fill='y', pady=12)
    canvas.pack(fill='both', expand=True, pady=12)
    rows = ttk.Frame(canvas)
    window = canvas.create_window((0, 0), window=rows, anchor='nw')
    canvas.configure(yscrollcommand=scrollbar.set)
    rows.bind('<Configure>', lambda _: canvas.configure(scrollregion=canvas.bbox('all')))
    canvas.bind('<Configure>', lambda event: canvas.itemconfigure(window, width=event.width))
    ttk.Label(frame, textvariable=status, wraplength=850).pack(anchor='w')

    def safe_save():
        try:
            save()
            refresh()
        except v.crm.CRMError as e:
            status.set(str(e))
        except OSError:
            status.set('CRM 주소를 저장하지 못했습니다.')

    def render():
        for widget in rows.winfo_children():
            widget.destroy()
        del controls[4:]
        try:
            client, pending = context()
            items = pending.items()
        except v.crm.CRMError as e:
            status.set(str(e))
            return
        root.title(f'velcro · CRM 에 붙이기 ({len(items)})')
        if not items:
            ttk.Label(rows, text='CRM 에 붙일 녹음이 없습니다. 주소를 설정한 뒤 가져온 녹음만 표시합니다.').pack(anchor='w')
        for item in items:
            row = ttk.Frame(rows, padding=(0, 8))
            row.pack(fill='x')
            duration = item.get('duration_s')
            secs = f'{duration:g}초' if isinstance(duration, (int, float)) else '길이 알 수 없음'
            ttk.Label(row, text=f"{Path(item['path']).name} · {item.get('recorded_at', item['at'])} · {secs}").pack(anchor='w')
            actions = ttk.Frame(row)
            actions.pack(fill='x', pady=4)
            choice = ttk.Combobox(actions, values=list(lead_labels), width=63)
            choice.set('')
            choice.pack(side='left', fill='x', expand=True)
            controls.append(choice)
            def upload(item=item, choice=choice):
                selected = choice.get().strip()
                if not selected:
                    status.set('올릴 카드를 먼저 선택하거나 카드 주소를 붙여 넣으세요.')
                    return
                if url.get().strip().rstrip('/') != client.url:
                    status.set('변경한 CRM 주소를 먼저 저장하세요.')
                    return
                lead = lead_labels.get(selected, selected)
                work(lambda: upload_selected(client, pending, item, lead), lambda _: render())
            def skip(item=item):
                work(lambda: pending.finish(item, skipped=True), lambda _: render())
            for label, command in [('올리기', upload), ('건너뛰기', skip)]:
                button = ttk.Button(actions, text=label, command=command)
                button.pack(side='left', padx=(6, 0))
                controls.append(button)

    def refresh():
        lead_labels.clear()
        render()
        try:
            client, _ = context()
            def loaded(leads):
                lead_labels.clear()
                lead_labels.update({f"{x['name']} · {x['stage']} · {x['id']}": x['id'] for x in leads})
                render()
            work(client.leads, loaded)
        except v.crm.CRMError as e:
            status.set(str(e))

    root.protocol('WM_DELETE_WINDOW', lambda: None if busy[0] else root.destroy())
    refresh()
    root.after(100, drain)
    root.mainloop()
