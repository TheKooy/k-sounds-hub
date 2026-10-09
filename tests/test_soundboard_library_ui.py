"""Real Qt event-loop tests, with all network/audio processing isolated."""
from __future__ import annotations
import copy
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
try:
    from PySide6.QtWidgets import QApplication
    from ksound_hub.ui import soundboard_library_dialog as ui
    from ksound_hub.soundboard_library import SoundEntry
except ImportError:
    ui = None


@unittest.skipIf(ui is None, 'PySide6 not installed')
class LibraryUiTests(unittest.TestCase):
    def test_import_stays_responsive_and_preserves_edits_made_during_download(self):
        app = QApplication.instance() or QApplication([])
        main_thread = threading.get_ident()
        class Service:
            document = {'slots': [], 'folders': ['Alerts'], 'global_volume': 80}
            def read_document(self): return copy.deepcopy(self.document)
            def write_document(self, data):
                assert threading.get_ident() == main_thread
                self.document = data
        service = Service()
        refreshed, started, release = [], threading.Event(), threading.Event()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            sound = root/'a.wav'
            sound.write_bytes(b'fixture')
            entry = SoundEntry('A', path=str(sound))
            dialog = ui.SoundboardLibraryDialog(None, service, root, 'Alerts', lambda: refreshed.append(True))
            dialog._show_entries([entry], True)
            self.assertTrue(dialog.add_button.isEnabled())
            def prepare(entries, config, cancelled, progress):
                assert threading.get_ident() != main_thread
                started.set()
                release.wait(3)
                return {'ready': [{'entry': {
                    'title': 'A', 'url': '', 'path': str(sound), 'creator': '', 'license': '', 'source': ''},
                    'path': str(sound), 'background_path': ''}], 'cancelled': False, 'errors': []}
            with patch.object(ui, 'prepare_import', side_effect=prepare):
                dialog._import_selected()
                deadline = time.monotonic() + 3
                while not started.is_set() and time.monotonic() < deadline:
                    app.processEvents()
                    time.sleep(.005)
                self.assertTrue(started.is_set())
                service.document['global_volume'] = 43
                app.processEvents()
                self.assertTrue(dialog.cancel.isEnabled())
                release.set()
                while dialog.worker is not None and time.monotonic() < deadline:
                    app.processEvents()
                    time.sleep(.005)
            self.assertIsNone(dialog.worker)
            self.assertEqual(service.document['global_volume'], 43)
            self.assertEqual(len(service.document['slots']), 1)
            self.assertEqual(service.document['slots'][0]['folder'], 'Alerts')
            self.assertEqual(refreshed, [True])
            self.assertIn('Added 1 sounds', dialog.status.text())
            dialog.list.selectAll()
            self.assertFalse(dialog.add_button.isEnabled(), 'already imported items must not be selectable')
            dialog.reject()
            dialog.deleteLater()
            app.processEvents()


if __name__ == '__main__':
    unittest.main()
