"""Single-window Tk desktop application. All installation work runs off the UI thread."""
from __future__ import annotations

import io
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageTk

from .discovery import Installation, Resource, make_replacement, read_png, scan
from .installer import run_helper
from .transactions import Manager, clear_cache


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title('JetBrains IDE Splash Patcher')
        root.geometry('1200x820')
        root.minsize(960, 700)
        self.ide = None
        self.resources = []
        self.nodes = {}
        self.changes = {}
        self.photos = []
        self.busy = False
        self.events = queue.Queue()
        self.path = tk.StringVar()
        self.workspace = tk.StringVar(value=str(Path.cwd() / 'splash-workspace'))
        self.cache_path = tk.StringVar()
        self.all_pngs = tk.BooleanVar()
        self.mode = tk.StringVar(value='Crop')
        self.filter_text = tk.StringVar()
        self.status = tk.StringVar(value='Choose your IDE installation to browse its artwork.')
        self.selection_text = tk.StringVar(value='Select an image or a standard / HiDPI pair.')
        self.count = tk.StringVar(value='0 replacements queued')
        self.controls = []
        outer = ttk.Frame(root, padding=16)
        outer.pack(fill='both', expand=True)
        ttk.Label(outer, text='JetBrains IDE Splash Patcher', font=('Segoe UI', 19, 'bold')).pack(anchor='w')
        ttk.Label(outer, text='Browse artwork • Preview replacements • Back up, apply, and restore').pack(anchor='w', pady=(2, 14))
        self.path_row(outer, 'IDE installation', self.path, self.choose_ide)
        self.path_row(outer, 'Backup workspace', self.workspace, self.choose_workspace)
        self.path_row(outer, 'Custom splash cache (optional)', self.cache_path, self.choose_cache)
        tools = ttk.Frame(outer)
        tools.pack(fill='x', pady=(6, 12))
        self.button(tools, 'Scan artwork', self.start_scan).pack(side='left')
        check = ttk.Checkbutton(tools, text='Scan all PNGs', variable=self.all_pngs)
        check.pack(side='left', padx=12)
        self.controls.append(check)
        ttk.Label(tools, text='Filter').pack(side='left', padx=(20, 5))
        search = ttk.Entry(tools, textvariable=self.filter_text)
        search.pack(side='left', fill='x', expand=True)
        self.controls.append(search)
        self.filter_text.trace_add('write', lambda *_: self.populate())
        panes = ttk.Panedwindow(outer, orient='horizontal')
        panes.pack(fill='both', expand=True)
        left = ttk.Frame(panes)
        right = ttk.Frame(panes, padding=(16, 0, 0, 0))
        panes.add(left, weight=2)
        panes.add(right, weight=3)
        self.tree = ttk.Treeview(left, columns=('size', 'queued'), selectmode='browse')
        self.tree.heading('#0', text='JAR / image resource')
        self.tree.heading('size', text='Dimensions')
        self.tree.heading('queued', text='Queued')
        self.tree.column('#0', width=290, minwidth=120)
        self.tree.column('size', width=110, stretch=False)
        self.tree.column('queued', width=60, stretch=False)
        scrollbar = ttk.Scrollbar(left, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side='left', fill='both', expand=True)
        scrollbar.pack(side='right', fill='y')
        self.tree.bind('<<TreeviewSelect>>', lambda _: self.preview())
        ttk.Label(right, textvariable=self.selection_text, wraplength=520).pack(anchor='w', pady=(0, 8))
        previews = ttk.Frame(right)
        previews.pack(fill='both', expand=True)
        previews.columnconfigure((0, 1), weight=1)
        previews.rowconfigure(1, weight=1)
        ttk.Label(previews, text='Currently installed', font=('Segoe UI', 11, 'bold')).grid(row=0, column=0, sticky='w')
        ttk.Label(previews, text='Queued replacement', font=('Segoe UI', 11, 'bold')).grid(row=0, column=1, sticky='w')
        self.current_canvas = tk.Canvas(previews, background='#20252b', highlightthickness=0, width=260, height=260)
        self.replacement_canvas = tk.Canvas(previews, background='#20252b', highlightthickness=0, width=260, height=260)
        self.current_canvas.grid(row=1, column=0, sticky='nsew', padx=(0, 5), pady=8)
        self.replacement_canvas.grid(row=1, column=1, sticky='nsew', padx=(5, 0), pady=8)
        for canvas in (self.current_canvas, self.replacement_canvas):
            canvas.bind('<Configure>', lambda _: self.preview())
        options = ttk.Frame(right)
        options.pack(fill='x', pady=(6, 4))
        ttk.Label(options, text='Resize').pack(side='left')
        mode = ttk.Combobox(options, textvariable=self.mode, values=('Crop', 'Fit'), state='readonly', width=8)
        mode.pack(side='left', padx=8)
        self.controls.append(mode)
        self.button(options, 'Choose replacement…', self.choose_replacement).pack(side='left')
        self.button(right, 'Remove selected from queue', self.remove_selected).pack(anchor='w', pady=4)
        ttk.Label(right, text='Select a pair to replace both sizes, or a child image to replace one.\nCrop fills the frame; Fit adds black margins. Resize applies when choosing an image.',
                  wraplength=520).pack(anchor='w', pady=8)
        ttk.Label(right, textvariable=self.count, font=('Segoe UI', 11, 'bold')).pack(anchor='w', pady=8)
        actions = ttk.Frame(outer)
        actions.pack(fill='x', pady=(12, 8))
        self.button(actions, 'Restore originals', self.restore).pack(side='left')
        self.button(actions, 'Recover interrupted operation', self.recover).pack(side='left', padx=8)
        self.button(actions, 'Clear splash cache', self.cache_only).pack(side='left')
        self.button(actions, 'Review & apply…', self.apply).pack(side='right')
        self.log = tk.Text(outer, height=5, wrap='word', state='disabled', font=('Consolas', 9))
        self.log.pack(fill='x')
        ttk.Label(outer, textvariable=self.status, wraplength=1100).pack(anchor='w', pady=(7, 0))
        root.protocol('WM_DELETE_WINDOW', self.close)
        root.after(80, self.poll)

    def button(self, parent, text, command):
        widget = ttk.Button(parent, text=text, command=command)
        self.controls.append(widget)
        return widget

    def path_row(self, parent, label, variable, command):
        row = ttk.Frame(parent)
        row.pack(fill='x', pady=3)
        ttk.Label(row, text=label, width=29).pack(side='left')
        entry = ttk.Entry(row, textvariable=variable, state='readonly')
        entry.pack(side='left', fill='x', expand=True, padx=(0, 8))
        self.button(row, 'Browse…', command).pack(side='right')

    def append_log(self, text):
        self.log.configure(state='normal')
        self.log.insert('end', text + '\n')
        self.log.see('end')
        self.log.configure(state='disabled')

    def poll(self):
        try:
            while True:
                event, value = self.events.get_nowait()
                if event == 'status':
                    self.status.set(value)
                elif event == 'log':
                    self.append_log(value)
                elif event == 'done':
                    callback, result = value
                    self.set_busy(False)
                    callback(result)
                elif event == 'error':
                    self.set_busy(False)
                    self.status.set('Operation stopped. See the log for details.')
                    self.append_log(value)
                    messagebox.showerror('Operation stopped', value, parent=self.root)
        except queue.Empty:
            pass
        self.root.after(80, self.poll)

    def set_busy(self, value):
        self.busy = value
        for widget in self.controls:
            widget.state(['disabled'] if value else ['!disabled'])

    def work(self, function, callback):
        if self.busy:
            return
        self.set_busy(True)
        def worker():
            try:
                self.events.put(('done', (callback, function())))
            except Exception as exc:
                self.events.put(('error', str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def choose_ide(self):
        if self.changes and not messagebox.askyesno('Discard queued images?', 'Switching installations discards the replacement queue.', parent=self.root):
            return
        folder = filedialog.askdirectory(title='Choose JetBrains IDE installation')
        if folder:
            try:
                ide = Installation.load(Path(folder))
            except Exception as exc:
                messagebox.showerror('Invalid installation', str(exc), parent=self.root)
                return
            self.ide = ide
            self.path.set(str(ide.root))
            self.changes.clear()
            self.resources.clear()
            self.cache_path.set('')
            self.start_scan()

    def choose_workspace(self):
        folder = filedialog.askdirectory(title='Choose persistent backup workspace')
        if folder:
            self.workspace.set(folder)

    def choose_cache(self):
        folder = filedialog.askdirectory(title='Optional: select the actual splash cache folder (named splash)')
        if folder:
            if Path(folder).name.lower() != 'splash':
                messagebox.showerror('Invalid cache', 'Select the directory named splash.', parent=self.root)
                return
            self.cache_path.set(folder)

    def start_scan(self):
        if not self.ide:
            self.choose_ide()
            return
        ide, all_pngs = self.ide, self.all_pngs.get()
        self.status.set('Scanning artwork…')
        self.work(lambda: scan(ide, all_pngs, lambda message: self.events.put(('status', message))), self.scanned)

    def scanned(self, result):
        self.resources, warnings, jars = result
        self.populate()
        text = f'{self.ide.name} · build {self.ide.build} · {jars} JARs · {len(self.resources)} PNG resources'
        self.status.set(text)
        self.append_log(text)
        for warning in warnings:
            self.append_log('Skipped: ' + warning)

    def populate(self):
        if not hasattr(self, 'tree'):
            return
        self.tree.delete(*self.tree.get_children())
        self.nodes.clear()
        groups = {}
        term = self.filter_text.get().lower()
        for resource in self.resources:
            if term and term not in (resource.jar + '/' + resource.name).lower():
                continue
            groups.setdefault(resource.jar, {}).setdefault(resource.pair_key, []).append(resource)
        for jar, pairs in groups.items():
            jar_node = self.tree.insert('', 'end', text=jar, open=True)
            for pair, resources in pairs.items():
                resources.sort(key=lambda r: r.width)
                if len(resources) == 1:
                    self.insert_resource(jar_node, resources[0])
                else:
                    queued = sum((r.jar, r.name) in self.changes for r in resources)
                    node = self.tree.insert(jar_node, 'end', text=pair + ' (pair)', open=False,
                                            values=('', str(queued) if queued else ''))
                    self.nodes[node] = resources
                    for resource in resources:
                        self.insert_resource(node, resource)
        self.count.set(f'{len(self.changes)} replacements queued across {len({j for j, _ in self.changes})} JARs')
        self.preview()

    def insert_resource(self, parent, resource):
        node = self.tree.insert(parent, 'end', text=resource.name,
                                values=(f'{resource.width} × {resource.height}',
                                        'Yes' if (resource.jar, resource.name) in self.changes else ''))
        self.nodes[node] = [resource]

    def selected(self):
        selection = self.tree.selection()
        return self.nodes.get(selection[0], []) if selection else []

    def preview(self):
        if not hasattr(self, 'current_canvas'):
            return
        self.photos.clear()
        for canvas in (self.current_canvas, self.replacement_canvas):
            canvas.delete('all')
        resources = self.selected()
        if not resources:
            self.selection_text.set('Select an image or a standard / HiDPI pair.')
            return
        resource = resources[0]
        self.selection_text.set(resource.jar + '\n' + '\n'.join(f'{r.name} — {r.width} × {r.height}' for r in resources))
        try:
            self.draw(self.current_canvas, read_png(self.ide.root, resource))
            replacement = self.changes.get((resource.jar, resource.name))
            if replacement:
                self.draw(self.replacement_canvas, replacement)
            else:
                self.replacement_canvas.create_text(130, 130, text='No replacement selected', fill='#c0c9d4')
        except Exception as exc:
            self.status.set(str(exc))

    def draw(self, canvas, data):
        with Image.open(io.BytesIO(data)) as im:
            image = im.convert('RGBA')
        size = (max(40, canvas.winfo_width() - 16), max(40, canvas.winfo_height() - 16))
        image.thumbnail(size, Image.Resampling.LANCZOS)
        photo = ImageTk.PhotoImage(image)
        self.photos.append(photo)
        canvas.create_image(canvas.winfo_width() // 2, canvas.winfo_height() // 2, image=photo)

    def choose_replacement(self):
        resources = self.selected()
        if not resources:
            messagebox.showinfo('Select artwork', 'Select an image or a pair first.', parent=self.root)
            return
        path = filedialog.askopenfilename(title='Choose replacement artwork', filetypes=[('Artwork', '*.png *.jpg *.jpeg *.webp')])
        if not path:
            return
        try:
            prepared = {(r.jar, r.name): make_replacement(Path(path), (r.width, r.height), self.mode.get()) for r in resources}
        except Exception as exc:
            messagebox.showerror('Cannot load artwork', str(exc), parent=self.root)
            return
        self.changes.update(prepared)
        self.refresh_selection(resources)

    def refresh_selection(self, resources):
        self.populate()
        for node, value in self.nodes.items():
            if value == resources:
                self.tree.selection_set(node)
                self.tree.see(node)
                self.preview()
                break

    def remove_selected(self):
        resources = self.selected()
        for resource in resources:
            self.changes.pop((resource.jar, resource.name), None)
        self.refresh_selection(resources)

    def manager(self):
        if not self.ide:
            raise ValueError('Select an IDE installation first')
        return Manager(self.ide, Path(self.workspace.get()))

    def apply(self):
        if not self.changes:
            messagebox.showinfo('Nothing queued', 'Choose replacement artwork first.', parent=self.root)
            return
        changes = {}
        for (jar, entry), png in self.changes.items():
            changes.setdefault(jar, {})[entry] = png
        try:
            manager = self.manager()
            state = manager.state()
        except Exception as exc:
            messagebox.showerror('Cannot prepare', str(exc), parent=self.root)
            return
        originals = {}
        unknown = [jar for jar in changes if jar not in state['jars']]
        confirmed = False
        if unknown:
            answer = messagebox.askyesnocancel('Identify original JARs',
                'Are the selected installed JARs untouched originals?\n\n'
                'Yes: back up the currently installed JARs as originals.\n'
                'No: choose your original backup for each new JAR.\n'
                'Cancel: return without changing anything.\n\n'
                'If you already patched PyCharm, choose No. The app cannot prove that a JAR is an original JetBrains file.', parent=self.root)
            if answer is None:
                return
            confirmed = answer
            if not answer:
                for jar in unknown:
                    path = filedialog.askopenfilename(title=f'Choose original backup for {jar}', filetypes=[('JAR or backup', '*.jar *.bak'), ('All files', '*')])
                    if not path:
                        return
                    originals[jar] = Path(path)
        summary = '\n'.join(jar + '\n' + '\n'.join('    ' + entry for entry in images) for jar, images in changes.items())
        if not self.review(summary):
            return
        cache = Path(self.cache_path.get()) if self.cache_path.get() else None
        self.status.set('Backing up and validating staged JARs…')
        def operation():
            plan = manager.prepare(changes, originals, confirmed)
            self.events.put(('status', 'Installing validated JARs… approve the Windows administrator prompt if shown.'))
            run_helper(plan)
            return self.cache_result(cache)
        self.work(operation, self.applied)

    def review(self, summary):
        dialog = tk.Toplevel(self.root)
        dialog.title('Review changes')
        dialog.geometry('700x470')
        dialog.transient(self.root)
        dialog.grab_set()
        ttk.Label(dialog, text='These resources will be replaced after original JARs are backed up.', padding=12).pack(anchor='w')
        text = tk.Text(dialog, wrap='word', height=15)
        text.pack(fill='both', expand=True, padx=12)
        text.insert('1.0', summary)
        text.configure(state='disabled')
        closed = tk.BooleanVar()
        result = [False]
        ttk.Checkbutton(dialog, text='I have closed this IDE completely.', variable=closed).pack(anchor='w', padx=12, pady=10)
        def accept():
            if closed.get():
                result[0] = True
                dialog.destroy()
            else:
                messagebox.showinfo('Close the IDE', 'Close the IDE and check the confirmation box.', parent=dialog)
        buttons = ttk.Frame(dialog, padding=12)
        buttons.pack(fill='x')
        ttk.Button(buttons, text='Cancel', command=dialog.destroy).pack(side='left')
        ttk.Button(buttons, text='Back up & apply', command=accept).pack(side='right')
        self.root.wait_window(dialog)
        return result[0]

    def cache_result(self, cache):
        try:
            return clear_cache(self.ide, cache)
        except Exception as exc:
            return f'JAR operation succeeded, but splash cache could not be renamed: {exc}. Close the IDE and use Clear splash cache before launching.'

    def applied(self, result):
        self.changes.clear()
        self.populate()
        self.append_log(result)
        self.status.set('JAR operation completed. ' + result)
        messagebox.showinfo('Completed', result, parent=self.root)

    def restore(self):
        try:
            manager = self.manager()
            jars = manager.state()['jars']
            if not jars:
                raise ValueError('No original backups exist in this workspace for this installation')
        except Exception as exc:
            messagebox.showerror('Cannot restore', str(exc), parent=self.root)
            return
        if not messagebox.askyesno('Restore original JARs?', 'Close the IDE first. Restore all backed-up JARs?\n\n' + '\n'.join(jars), parent=self.root):
            return
        cache = Path(self.cache_path.get()) if self.cache_path.get() else None
        def operation():
            run_helper(manager.prepare(None))
            return self.cache_result(cache)
        self.status.set('Verifying backups and preparing restoration…')
        self.work(operation, self.applied)

    def recover(self):
        try:
            plan = self.manager().recover()
        except Exception as exc:
            messagebox.showerror('Cannot recover', str(exc), parent=self.root)
            return
        if not messagebox.askyesno('Recover interrupted operation?', 'Close the IDE first. Return every affected JAR to its state before the pending operation?', parent=self.root):
            return
        cache = Path(self.cache_path.get()) if self.cache_path.get() else None
        def operation():
            run_helper(plan, recover=True)
            return self.cache_result(cache)
        self.work(operation, self.applied)

    def cache_only(self):
        if not self.ide:
            return
        from .transactions import ensure_closed
        cache = Path(self.cache_path.get()) if self.cache_path.get() else None
        def operation():
            ensure_closed(self.ide)
            return clear_cache(self.ide, cache)
        self.work(operation, lambda result: (self.append_log(result), self.status.set(result)))

    def close(self):
        if self.busy:
            messagebox.showinfo('Operation running', 'Wait for the current operation to finish before closing.', parent=self.root)
            return
        if self.changes and not messagebox.askyesno('Discard queued images?', 'Close without applying the queued replacements?', parent=self.root):
            return
        self.root.destroy()


def main():
    root = tk.Tk()
    style = ttk.Style(root)
    if 'vista' in style.theme_names():
        style.theme_use('vista')
    App(root)
    root.mainloop()
