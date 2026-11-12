"""Real Tk widget smoke test; run under xvfb-run on Linux or a Windows desktop."""
import io
import os
from pathlib import Path
import tempfile
import tkinter as tk
import unittest
from unittest.mock import patch

from PIL import Image, ImageGrab

from splash_patcher.discovery import Installation, scan
from splash_patcher.gui import App
from test_core import create_jar, png


@unittest.skipUnless(os.name == 'nt' or os.environ.get('DISPLAY'), 'Requires a graphical display')
class GuiTests(unittest.TestCase):
    def test_browse_pair_choose_override_remove_and_filter(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            (base/'lib').mkdir()
            (base/'product-info.json').write_text('{"name":"Fixture IDE","buildNumber":"263.1","dataDirectoryName":"Fixture2026.3"}')
            create_jar(base/'lib'/'art.jar')
            image = base/'replacement.png'
            image.write_bytes(png(color='red'))
            root = tk.Tk()
            try:
                app = App(root)
                app.ide = Installation.load(base)
                app.path.set(str(base))
                app.scanned(scan(app.ide))
                root.update()
                pair_node = next(node for node, resources in app.nodes.items() if len(resources) == 2)
                app.tree.selection_set(pair_node)
                root.update()
                self.assertEqual(len(app.photos), 1)
                with patch('splash_patcher.gui.filedialog.askopenfilename', return_value=str(image)):
                    app.choose_replacement()
                root.update()
                self.assertEqual(len(app.changes), 2)
                self.assertEqual(len(app.photos), 2)
                for widget in app.controls:
                    self.assertLessEqual(widget.winfo_rooty() + widget.winfo_height(),
                                         root.winfo_rooty() + root.winfo_height())
                if os.environ.get('TK_SCREENSHOT'):
                    destination = Path(os.environ['TK_SCREENSHOT'])
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    ImageGrab.grab().save(destination)
                for (jar,entry), data in app.changes.items():
                    with Image.open(io.BytesIO(data)) as im:
                        self.assertEqual(im.size, (160,100) if '@2x' in entry else (80,50))
                child = next(node for node, resources in app.nodes.items()
                             if len(resources) == 1 and '@2x' in resources[0].name)
                app.tree.selection_set(child)
                app.remove_selected()
                self.assertEqual(len(app.changes), 1)
                app.filter_text.set('no-such-image')
                self.assertEqual(len(app.tree.get_children()), 0)
                app.filter_text.set('logo')
                self.assertGreater(len(app.tree.get_children()), 0)
                app.set_busy(True)
                self.assertTrue(all(widget.instate(['disabled']) for widget in app.controls))
                app.set_busy(False)
            finally:
                root.destroy()


if __name__ == '__main__':
    unittest.main()
